"""Live execution engine — TRIPLE-GUARDED, disabled by default.

A real order is only ever sent when ALL of the following hold:
  * ``config.execution.live_enabled`` is true, AND
  * ``config.mode`` == ``live``, AND
  * env ``CONFIRM_LIVE_TRADING`` == ``YES``.

Any call to :meth:`execute` while not armed raises immediately.

Flow: pre-trade balance check -> place the combo + hedge-leg orders (IOC, so each
fills immediately or cancels) -> reconcile real fills/fees from /portfolio/fills.
If the hedged set only *partially* fills, unwind the filled remainder back to flat
(legging protection) so we are never left with a naked leg or combo.

SCHEMA NOTE: price is sent as integer cents (yes_price/no_price). These MVE combo
markets use a deci-cent tick, so CONFIRM the exact price field/units against the
Kalshi DEMO environment before any real order and adjust :meth:`_to_kalshi_order`
if demo rejects cents.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from combo_arb.config import AppConfig
from combo_arb.execution.base import ExecutionEngine
from combo_arb.kalshi.client import KalshiClient
from combo_arb.models import Fill, LegPrice, Order, OrderStatus, Side
from combo_arb.pricing.fees import taker_fee

log = logging.getLogger(__name__)

_UNWIND_SLIPPAGE = 0.05  # cross this far through to close on an emergency unwind


class LiveTradingNotArmed(RuntimeError):
    pass


class InsufficientBalance(RuntimeError):
    pass


def _cents(v) -> Optional[float]:
    try:
        return float(v) / 100.0
    except (TypeError, ValueError):
        return None


def _clamp_price(p: float) -> float:
    return min(0.99, max(0.01, p))


class LiveExecutionEngine(ExecutionEngine):
    def __init__(self, cfg: AppConfig, client: KalshiClient):
        self.cfg = cfg
        self.client = client
        if not cfg.live_trading_armed():
            log.warning(
                "LiveExecutionEngine constructed but NOT armed "
                "(live_enabled=%s, mode=%s, confirm=%s). Orders will be refused.",
                cfg.execution.live_enabled, cfg.mode.value, cfg.secrets.live_confirmed,
            )
        else:
            log.warning("*** LIVE TRADING ARMED — real orders will be sent to Kalshi ***")

    # -- payload ----------------------------------------------------------
    def _to_kalshi_order(self, order: Order, client_order_id: str) -> dict:
        payload = {
            "ticker": order.instrument,
            "client_order_id": client_order_id,   # idempotency
            "action": order.action,                # buy | sell
            "side": order.side.value,              # yes | no
            "count": order.qty,
            "type": "limit",
            "time_in_force": self.cfg.execution.time_in_force,
        }
        price_cents = int(round(order.price * 100))
        payload["yes_price" if order.side == Side.YES else "no_price"] = price_cents
        return payload

    # -- pre-trade balance ------------------------------------------------
    def _check_balance(self, orders: list[Order]) -> None:
        if not self.cfg.execution.require_balance_check:
            return
        needed = sum(o.qty * o.price for o in orders if o.action == "buy")
        try:
            available = (self.client.get_balance().get("balance") or 0) / 100.0
        except RuntimeError as exc:
            raise InsufficientBalance(f"could not read balance: {exc}") from exc
        if needed > available:
            raise InsufficientBalance(
                f"insufficient balance: need ~${needed:.2f}, have ${available:.2f}"
            )

    # -- placement + reconciliation --------------------------------------
    def _place(self, order: Order) -> None:
        payload = self._to_kalshi_order(order, uuid.uuid4().hex)
        resp = self.client.create_order(payload)
        order.kalshi_order_id = resp.get("order", {}).get("order_id")

    def _reconcile(self, order: Order) -> list[Fill]:
        """Real fills for one order (price/qty/fee); fee computed if not returned."""
        try:
            raw = self.client.get_fills(order_id=order.kalshi_order_id) if order.kalshi_order_id else []
        except RuntimeError as exc:
            log.warning("fill reconciliation failed for %s: %s", order.instrument, exc)
            raw = []
        fills: list[Fill] = []
        for f in raw:
            price = _cents(f.get("yes_price") if order.side == Side.YES else f.get("no_price"))
            price = price if price is not None else order.price
            qty = int(f.get("count", 0))
            if qty <= 0:
                continue
            fee = _cents(f.get("fee"))
            if fee is None:
                fee = taker_fee(price, qty, self.cfg.fees)
            fills.append(Fill(
                order_id=order.kalshi_order_id or order.instrument,
                instrument=order.instrument,
                instrument_type=order.instrument_type,
                side=order.side,
                action=order.action,
                price=price,
                qty=qty,
                fee=fee,
            ))
        return fills

    def execute(self, orders: list[Order], leg_prices: dict[str, LegPrice]) -> list[Fill]:
        if not self.cfg.live_trading_armed():
            raise LiveTradingNotArmed(
                "Live trading is not armed. Set execution.live_enabled=true, mode=live, "
                "and env CONFIRM_LIVE_TRADING=YES to enable real order entry."
            )
        self._check_balance(orders)

        # Place combo + legs (IOC), as near-simultaneous as the rate limiter allows.
        for order in orders:
            try:
                self._place(order)
                order.status = OrderStatus.NEW
            except RuntimeError as exc:
                log.error("order placement failed for %s: %s", order.instrument, exc)
                order.status = OrderStatus.REJECTED

        time.sleep(min(self.cfg.execution.fill_poll_timeout_s, 1.0))  # let IOC fills settle

        fills: list[Fill] = []
        filled: dict[int, int] = {}
        for order in orders:
            of = self._reconcile(order)
            fills.extend(of)
            fq = sum(f.qty for f in of)
            filled[id(order)] = fq
            order.status = (
                OrderStatus.FILLED if fq >= order.qty
                else OrderStatus.PARTIAL if fq > 0
                else order.status
            )

        # Legging protection: if any placed order did not fully fill, the hedge is
        # incomplete — unwind every filled quantity back to flat.
        incomplete = any(
            filled.get(id(o), 0) < o.qty for o in orders if o.status != OrderStatus.REJECTED
        )
        if incomplete and self.cfg.execution.unwind_on_partial:
            log.error("partial hedged fill — unwinding filled legs back to flat")
            fills.extend(self._unwind(orders, filled))
        return fills

    def _unwind(self, orders: list[Order], filled: dict[int, int]) -> list[Fill]:
        unwind_fills: list[Fill] = []
        for order in orders:
            fq = filled.get(id(order), 0)
            if fq <= 0:
                continue
            closing = order.action == "buy"  # bought -> sell to close, and vice versa
            price = _clamp_price(
                order.price - _UNWIND_SLIPPAGE if closing else order.price + _UNWIND_SLIPPAGE
            )
            opp = Order(
                instrument=order.instrument,
                instrument_type=order.instrument_type,
                side=order.side,
                action="sell" if closing else "buy",
                price=price,
                qty=fq,
                mode="live",
                signal_ref=order.signal_ref,
            )
            try:
                self._place(opp)
                time.sleep(min(self.cfg.execution.fill_poll_timeout_s, 1.0))
                closed = self._reconcile(opp)
                unwind_fills.extend(closed)
                if sum(f.qty for f in closed) < fq:
                    log.critical(
                        "UNWIND INCOMPLETE for %s: %d filled, %d closed — MANUAL "
                        "INTERVENTION NEEDED (naked exposure remains)",
                        order.instrument, fq, sum(f.qty for f in closed),
                    )
            except RuntimeError as exc:
                log.critical(
                    "UNWIND ORDER FAILED for %s (%d) — MANUAL INTERVENTION NEEDED: %s",
                    order.instrument, fq, exc,
                )
        return unwind_fills
