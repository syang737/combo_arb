"""Live execution engine — TRIPLE-GUARDED, disabled by default.

A real order is only ever sent when ALL of the following hold:
  * ``config.execution.live_enabled`` is true, AND
  * ``config.mode`` == ``live``, AND
  * env ``CONFIRM_LIVE_TRADING`` == ``YES``.

Any call to :meth:`execute` while not armed raises immediately. This class is
scaffolding for a future live rollout; the default engine everywhere is paper.
"""

from __future__ import annotations

import logging

from combo_arb.config import AppConfig
from combo_arb.execution.base import ExecutionEngine
from combo_arb.kalshi.client import KalshiClient
from combo_arb.models import Fill, InstrumentType, LegPrice, Order, OrderStatus

log = logging.getLogger(__name__)


class LiveTradingNotArmed(RuntimeError):
    pass


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

    def _to_kalshi_order(self, order: Order) -> dict:
        """Map an internal Order to a Kalshi order-entry payload (price in cents)."""
        return {
            "ticker": order.instrument,
            "action": order.action,           # buy | sell
            "side": order.side.value,         # yes | no
            "count": order.qty,
            "type": "limit",
            "yes_price": int(round(order.price * 100)) if order.side.value == "yes" else None,
            "no_price": int(round(order.price * 100)) if order.side.value == "no" else None,
        }

    def execute(self, orders: list[Order], leg_prices: dict[str, LegPrice]) -> list[Fill]:
        if not self.cfg.live_trading_armed():
            raise LiveTradingNotArmed(
                "Live trading is not armed. Set execution.live_enabled=true, mode=live, "
                "and env CONFIRM_LIVE_TRADING=YES to enable real order entry."
            )
        # NOTE: combos/MVE order entry may use a distinct endpoint; leg orders use
        # /portfolio/orders. Confirm against the live API before enabling.
        fills: list[Fill] = []
        for order in orders:
            if order.instrument_type == InstrumentType.COMBO:
                log.error("combo order entry not wired for live; skipping %s", order.instrument)
                order.status = OrderStatus.REJECTED
                continue
            payload = {k: v for k, v in self._to_kalshi_order(order).items() if v is not None}
            resp = self.client.create_order(payload)
            od = resp.get("order", {})
            order.kalshi_order_id = od.get("order_id")
            order.status = OrderStatus.FILLED if od.get("status") == "executed" else OrderStatus.NEW
            fills.append(
                Fill(
                    order_id=order.kalshi_order_id or order.instrument,
                    instrument=order.instrument,
                    side=order.side,
                    action=order.action,
                    price=order.price,
                    qty=int(od.get("count", order.qty)),
                    fee=0.0,  # reconcile from fills endpoint in a full implementation
                )
            )
        return fills
