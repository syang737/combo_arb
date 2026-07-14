"""Risk management, sizing, and the leg-hedge model.

Hedging note (important): a combo pays $1 iff *all* legs resolve YES, so its
payoff is the *product* of the legs — a nonlinear (AND) function. No static
portfolio of single-leg binaries replicates it exactly, so "hedge via legs" is
a **first-order delta hedge**, not a riskless lock. For a short combo we hold

    delta_i = d(combo)/d(p_i) = (product of the other legs' contributions)

units of leg ``i`` (buying YES when delta_i > 0, NO when < 0), scaled by the
configured ``correlation_factor``. Residual convexity / correlation risk remains
and is covered by the fee+buffer edge required at scan time. ``HedgeModel`` is a
protocol so this can be swapped for a better model later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

from combo_arb.config import AppConfig
from combo_arb.models import (
    ArbSignal,
    InstrumentType,
    LegPrice,
    Order,
    OrderStatus,
    Position,
    Side,
)
from combo_arb.pricing.model import implied_prob

_EPS = 1e-9


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    qty: int = 0
    combo_order: Optional[Order] = None
    hedge_orders: list[Order] = field(default_factory=list)

    @property
    def all_orders(self) -> list[Order]:
        orders = list(self.hedge_orders)
        if self.combo_order is not None:
            orders.insert(0, self.combo_order)
        return orders


def leg_deltas(
    signal: ArbSignal,
    leg_prices: dict[str, LegPrice],
    cfg: AppConfig,
) -> dict[str, float]:
    """Signed first-order delta of the combo w.r.t. each leg's underlying prob.

    Contribution c_i = p_i (YES leg) or (1 - p_i) (NO leg); combo = product(c_i).
    delta_i = (product of other c_j) * d(c_i)/d(p_i), where d(c_i)/d(p_i) is +1
    for a YES leg and -1 for a NO leg. Scaled by ``correlation_factor``.
    """
    contribs: dict[str, float] = {}
    signs: dict[str, float] = {}
    for leg in signal.legs:
        lp = leg_prices.get(leg.leg_ticker)
        p = implied_prob(lp, cfg.pricing) if lp else None
        if p is None:
            continue
        if leg.side == Side.YES:
            contribs[leg.leg_ticker] = p
            signs[leg.leg_ticker] = 1.0
        else:
            contribs[leg.leg_ticker] = 1.0 - p
            signs[leg.leg_ticker] = -1.0

    deltas: dict[str, float] = {}
    k = cfg.pricing.correlation_factor
    for ticker in contribs:
        prod_others = 1.0
        for other, c in contribs.items():
            if other != ticker:
                prod_others *= c
        deltas[ticker] = prod_others * signs[ticker] * k
    return deltas


class HedgeModel(Protocol):
    def build(
        self, signal: ArbSignal, qty: int, leg_prices: dict[str, LegPrice], cfg: AppConfig
    ) -> tuple[Order, list[Order]]:
        ...


class DeltaHedgeModel:
    """Default approximate hedge: short the combo YES, delta-hedge each leg."""

    def build(
        self, signal: ArbSignal, qty: int, leg_prices: dict[str, LegPrice], cfg: AppConfig
    ) -> tuple[Order, list[Order]]:
        combo_order = Order(
            instrument=signal.mve_collection_ticker,
            instrument_type=InstrumentType.COMBO,
            side=Side.YES,
            action="sell",
            price=signal.combo_quote_yes,
            qty=qty,
            mode=cfg.mode.value,
            signal_ref=signal.rfq_id,
            status=OrderStatus.NEW,
        )

        deltas = leg_deltas(signal, leg_prices, cfg)
        hedge_orders: list[Order] = []
        for leg in signal.legs:
            d = deltas.get(leg.leg_ticker, 0.0)
            hedge_qty = round(qty * abs(d))
            if hedge_qty <= 0:
                continue
            lp = leg_prices.get(leg.leg_ticker)
            buy_yes = d > 0
            # Taker cross to enter the hedge: pay the ask (YES) side.
            price = (lp.best_ask if lp and lp.best_ask is not None else (lp.mid if lp else 0.5))
            hedge_orders.append(
                Order(
                    instrument=leg.leg_ticker,
                    instrument_type=InstrumentType.LEG,
                    side=Side.YES if buy_yes else Side.NO,
                    action="buy",
                    price=price if buy_yes else (1.0 - price if price is not None else 0.5),
                    qty=hedge_qty,
                    mode=cfg.mode.value,
                    signal_ref=signal.rfq_id,
                    status=OrderStatus.NEW,
                )
            )
        return combo_order, hedge_orders


def per_contract_hedge_cost(
    signal: ArbSignal, leg_prices: dict[str, LegPrice], cfg: AppConfig
) -> float:
    """Cash outlay to hedge one combo contract (sum of |delta_i| * leg_price)."""
    deltas = leg_deltas(signal, leg_prices, cfg)
    cost = 0.0
    for ticker, d in deltas.items():
        lp = leg_prices.get(ticker)
        price = lp.mid if lp and lp.mid is not None else 0.5
        cost += abs(d) * price
    return cost


class RiskManager:
    """Enforces limits, sizes trades, and produces hedge orders."""

    def __init__(self, cfg: AppConfig, hedge_model: Optional[HedgeModel] = None):
        self.cfg = cfg
        self.hedge_model = hedge_model or DeltaHedgeModel()
        self.positions: dict[str, Position] = {}
        self.total_exposure: float = 0.0
        self.open_signals: int = 0

    def _size(self, signal: ArbSignal, leg_prices: dict[str, LegPrice]) -> int:
        r = self.cfg.risk
        cost = max(per_contract_hedge_cost(signal, leg_prices, self.cfg), _EPS)
        qty_by_capital = int(r.capital_per_trade // cost)
        return max(0, min(signal.size, r.max_contracts_per_trade, qty_by_capital))

    def evaluate(self, signal: ArbSignal, leg_prices: dict[str, LegPrice]) -> RiskDecision:
        r = self.cfg.risk
        if r.kill_switch:
            return RiskDecision(False, "kill_switch engaged")
        if self.open_signals >= r.max_open_signals:
            return RiskDecision(False, "max_open_signals reached")

        qty = self._size(signal, leg_prices)
        if qty <= 0:
            return RiskDecision(False, "sized to zero (capital/limits too tight)")

        cost = per_contract_hedge_cost(signal, leg_prices, self.cfg)
        added_exposure = qty * cost
        if self.total_exposure + added_exposure > r.max_total_exposure:
            return RiskDecision(False, "max_total_exposure would be exceeded")

        combo_order, hedge_orders = self.hedge_model.build(signal, qty, leg_prices, self.cfg)

        # Per-market position cap check (projected).
        for od in hedge_orders:
            projected = abs(self._projected_net(od))
            if projected > r.max_position_per_market:
                return RiskDecision(False, f"max_position_per_market exceeded on {od.instrument}")

        return RiskDecision(True, "approved", qty, combo_order, hedge_orders)

    def _projected_net(self, order: Order) -> int:
        pos = self.positions.get(order.instrument)
        current = pos.net_qty if pos else 0
        signed = order.qty if order.action == "buy" else -order.qty
        return current + signed

    def register_fill(self, fill) -> None:
        """Update positions + exposure from a fill (called by the controller)."""
        pos = self.positions.get(fill.instrument)
        if pos is None:
            pos = Position(
                instrument=fill.instrument,
                instrument_type=(
                    InstrumentType.COMBO
                    if fill.instrument.startswith(("COMBO", "MVE"))
                    else InstrumentType.LEG
                ),
            )
            self.positions[fill.instrument] = pos
        signed = fill.qty if fill.action == "buy" else -fill.qty
        new_net = pos.net_qty + signed
        # Simple weighted-average cost update on same-direction adds.
        if pos.net_qty == 0 or (pos.net_qty > 0) == (signed > 0):
            total = abs(pos.net_qty) + abs(signed)
            pos.avg_price = (
                (abs(pos.net_qty) * pos.avg_price + abs(signed) * fill.price) / total
                if total
                else fill.price
            )
        pos.net_qty = new_net
        self.total_exposure += abs(signed) * fill.price

    def mark_signal_opened(self) -> None:
        self.open_signals += 1
