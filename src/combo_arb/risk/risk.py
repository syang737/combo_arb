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

import math
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
from combo_arb.pricing.model import leg_deltas as _leg_deltas

_EPS = 1e-9


def leg_deltas(
    signal: ArbSignal, leg_prices: dict[str, LegPrice], cfg: AppConfig
) -> dict[str, float]:
    """Hedge ratios for a signal's legs (thin wrapper over the pricing model)."""
    return _leg_deltas(signal.legs, leg_prices, cfg.pricing)


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


class HedgeModel(Protocol):
    def build(
        self, signal: ArbSignal, qty: int, leg_prices: dict[str, LegPrice], cfg: AppConfig
    ) -> tuple[Order, list[Order]]:
        ...


def _leg_buy_yes(delta: float, sell_combo: bool) -> bool:
    """Which side to BUY to hedge leg ``i``.

    Short combo (sell) -> hold long delta: buy YES when delta>0, else NO.
    Long combo (buy)   -> hold short delta: buy NO  when delta>0, else YES.
    """
    return (delta > 0) if sell_combo else (delta < 0)


def _leg_entry_price(lp: Optional[LegPrice], buy_yes: bool) -> float:
    """Taker-cross entry price for buying the YES or NO side of a leg."""
    if lp is None:
        return 0.5
    if buy_yes:
        return lp.best_ask if lp.best_ask is not None else (lp.mid if lp.mid is not None else 0.5)
    # Buy NO = cross to the YES bid: NO ask = 1 - yes_bid.
    if lp.best_bid is not None:
        return 1.0 - lp.best_bid
    return (1.0 - lp.mid) if lp.mid is not None else 0.5


class DeltaHedgeModel:
    """Default approximate hedge. Direction-aware:

    * sell_overpriced -> sell the combo YES, delta-hedge by buying the legs.
    * buy_underpriced -> buy the combo YES, delta-hedge by shorting the legs.
    """

    def build(
        self, signal: ArbSignal, qty: int, leg_prices: dict[str, LegPrice], cfg: AppConfig
    ) -> tuple[Order, list[Order]]:
        sell_combo = cfg.strategy.direction == "sell_overpriced"
        # INVARIANT: a combo's own order side is always YES -- Kalshi only exposes a
        # directly tradeable YES side for these combo markets (see module docstring);
        # we never assume the NO side is quotable/tradeable. Direction only changes the
        # ACTION (buy vs sell), never the side. This must hold even in paper mode, since
        # paper is meant to emulate what a real order would actually be.
        combo_side = Side.YES
        combo_order = Order(
            # The tradeable combo market (live); fall back to the collection ticker
            # for the offline mock, which has no market_ticker.
            instrument=signal.market_ticker or signal.mve_collection_ticker,
            instrument_type=InstrumentType.COMBO,
            side=combo_side,
            action="sell" if sell_combo else "buy",
            price=signal.combo_quote_yes,
            qty=qty,
            mode=cfg.mode.value,
            signal_ref=signal.rfq_id,
            status=OrderStatus.NEW,
        )
        if combo_order.side != Side.YES:  # pragma: no cover - defensive, cannot happen above
            raise ValueError(
                f"refusing to build a combo order with side={combo_order.side!r}: "
                "combos are only tradeable on the YES side"
            )

        deltas = leg_deltas(signal, leg_prices, cfg)
        hedge_orders: list[Order] = []
        for leg in signal.legs:
            d = deltas.get(leg.leg_ticker, 0.0)
            hedge_qty = round(qty * abs(d))
            if hedge_qty <= 0:
                continue
            buy_yes = _leg_buy_yes(d, sell_combo)
            hedge_orders.append(
                Order(
                    instrument=leg.leg_ticker,
                    instrument_type=InstrumentType.LEG,
                    side=Side.YES if buy_yes else Side.NO,
                    action="buy",
                    price=_leg_entry_price(leg_prices.get(leg.leg_ticker), buy_yes),
                    qty=hedge_qty,
                    mode=cfg.mode.value,
                    signal_ref=signal.rfq_id,
                    status=OrderStatus.NEW,
                )
            )
        return combo_order, hedge_orders


def per_contract_capital(
    signal: ArbSignal, leg_prices: dict[str, LegPrice], cfg: AppConfig
) -> float:
    """Cash outlay per combo contract: leg-hedge cost + (combo premium if buying).

    Selling the combo collects premium (not counted as capital deployed); buying it
    pays the premium, so that is added to the hedge cost.
    """
    sell_combo = cfg.strategy.direction == "sell_overpriced"
    deltas = leg_deltas(signal, leg_prices, cfg)
    cost = 0.0
    for ticker, d in deltas.items():
        lp = leg_prices.get(ticker)
        p = lp.mid if lp and lp.mid is not None else 0.5
        buy_yes = _leg_buy_yes(d, sell_combo)
        cost += abs(d) * (p if buy_yes else (1.0 - p))
    if not sell_combo:
        cost += signal.combo_quote_yes
    return cost


class RiskManager:
    """Enforces limits, sizes trades, and produces hedge orders."""

    def __init__(self, cfg: AppConfig, hedge_model: Optional[HedgeModel] = None):
        self.cfg = cfg
        self.hedge_model = hedge_model or DeltaHedgeModel()
        self.positions: dict[str, Position] = {}
        self.total_exposure: float = 0.0
        self.open_signals: int = 0

    def _full_hedge_min_qty(self, signal: ArbSignal, leg_prices: dict[str, LegPrice]) -> int:
        """Smallest combo qty at which every *material* leg gets >=1 whole hedge contract.

        Hedge ratios are fractional (leg delta = product of the other legs' probs), so a
        single combo can't be delta-hedged in whole contracts -- the trade must be sized up
        until ``qty * delta_i >= 1`` for the smallest material delta. Legs with
        ``|delta| <= min_hedge_delta`` are treated as immaterial and don't drive sizing.
        """
        deltas = leg_deltas(signal, leg_prices, self.cfg)
        eps = self.cfg.risk.min_hedge_delta
        material = [abs(d) for d in deltas.values() if abs(d) > eps]
        if not material:
            return 1  # nothing material to hedge (rare); no size-up required
        return math.ceil(1.0 / min(material))

    def _sizing(self, signal: ArbSignal, leg_prices: dict[str, LegPrice]) -> tuple[int, str]:
        """Return (qty, reason). qty=0 means don't trade (reason explains why).

        We trade the *smallest fully-hedged* size. If that doesn't fit the per-trade
        capital / contract caps, we emit signal-only rather than execute a partial (naked)
        combo -- a partial hedge isn't an arb.

        NOTE: ``signal.size`` is intentionally NOT used as a cap here -- markets-discovery
        sets it to a placeholder 1, which would force every trade naked. Real available
        liquidity should be enforced at live-execution time against the book depth.
        """
        r = self.cfg.risk
        cost = max(per_contract_capital(signal, leg_prices, self.cfg), _EPS)
        qty_affordable = min(r.max_contracts_per_trade, int(r.capital_per_trade // cost))
        if qty_affordable <= 0:
            return 0, "sized to zero (capital/limits too tight)"
        qty_min = self._full_hedge_min_qty(signal, leg_prices)
        if qty_min > qty_affordable:
            return 0, (f"cannot fully hedge within limits "
                       f"(need qty>={qty_min}, affordable {qty_affordable})")
        return qty_min, "approved"

    def evaluate(self, signal: ArbSignal, leg_prices: dict[str, LegPrice]) -> RiskDecision:
        r = self.cfg.risk
        if r.kill_switch:
            return RiskDecision(False, "kill_switch engaged")
        if self.open_signals >= r.max_open_signals:
            return RiskDecision(False, "max_open_signals reached")

        qty, reason = self._sizing(signal, leg_prices)
        if qty <= 0:
            return RiskDecision(False, reason)

        cost = per_contract_capital(signal, leg_prices, self.cfg)
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
            pos = Position(instrument=fill.instrument, instrument_type=fill.instrument_type)
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

    def close_fill(self, fill) -> None:
        """Reverse a fill out of positions when its trade settles/expires (the contract
        resolved). Removes only this fill's contribution; a position shared with another
        open trade keeps that trade's quantity. Deletes the row when it reaches zero."""
        pos = self.positions.get(fill.instrument)
        if pos is None:
            return
        signed = fill.qty if fill.action == "buy" else -fill.qty
        pos.net_qty -= signed
        self.total_exposure = max(0.0, self.total_exposure - abs(signed) * fill.price)
        if pos.net_qty == 0:
            del self.positions[fill.instrument]

    def mark_signal_opened(self) -> None:
        self.open_signals += 1

    def mark_signal_closed(self) -> None:
        """Free a trade's risk slot once it's actually settled (see settle sweep)."""
        self.open_signals = max(0, self.open_signals - 1)

    def hydrate_open_signals(self, n: int) -> None:
        """Restore the count of still-open trades on startup, so a process restart
        doesn't silently forget about risk that's still outstanding."""
        self.open_signals = max(0, n)

    def hydrate_positions(self, positions: list[Position]) -> None:
        for pos in positions:
            self.positions[pos.instrument] = pos
