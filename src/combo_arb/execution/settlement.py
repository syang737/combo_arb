"""Settlement / PnL realization for a hedged combo trade.

Binary settlement: each leg's underlying resolves YES with its implied
probability; the combo resolves YES iff every selected leg resolves in the
combo's favour (YES-side leg -> underlying YES, NO-side leg -> underlying NO).

We are SHORT the combo YES (sold it to the requester) and hold the delta hedge
in the legs. PnL for one settlement scenario:

    combo:      qty * premium  - qty * (1 if combo_yes else 0)   - combo_fee
    each hedge: hqty * (1 if that side resolves else 0) - hqty*price - hedge_fee

Leg draws are independent here; ``correlation_factor`` already biases the fair
value. A full copula / common-factor model is a documented follow-up.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field

from combo_arb.config import AppConfig
from combo_arb.models import ArbSignal, ComboLeg, Fill, InstrumentType, Order, Side
from combo_arb.pricing.fees import taker_fee


@dataclass
class HedgedTrade:
    signal: ArbSignal
    combo_fill: Fill
    hedge_fills: list[Fill]
    leg_probs: dict[str, float]          # leg_ticker -> underlying YES prob
    leg_sides: dict[str, Side] = field(default_factory=dict)  # combo side per leg


def _fill_cash(fill: Fill) -> float:
    """Cash at trade time for a fill: buys pay out, sells collect; minus fees."""
    gross = fill.qty * fill.price
    return (-gross if fill.action == "buy" else gross) - fill.fee


def _fill_settlement_pnl(fill: Fill, resolves: bool) -> float:
    """Settlement PnL for a fill. A buy earns (payout - price); a sell earns
    (price - payout); ``resolves`` is whether the fill's side pays $1."""
    payout = 1.0 if resolves else 0.0
    if fill.action == "buy":
        return fill.qty * (payout - fill.price) - fill.fee
    return fill.qty * (fill.price - payout) - fill.fee


def immediate_cash(trade: HedgedTrade) -> float:
    """Net cash at trade time across the combo fill and all hedge fills."""
    cash = _fill_cash(trade.combo_fill)
    for hf in trade.hedge_fills:
        cash += _fill_cash(hf)
    return cash


def _resolve_combo(legs: list[ComboLeg], outcomes: dict[str, bool]) -> bool:
    """Combo resolves YES iff every selected leg resolves in the combo's favour."""
    combo_yes = True
    for leg in legs:
        underlying_yes = outcomes[leg.leg_ticker]
        leg_ok = underlying_yes if leg.side == Side.YES else (not underlying_yes)
        combo_yes = combo_yes and leg_ok
    return combo_yes


def _trade_pnl(
    legs: list[ComboLeg], combo_fill: Fill, hedge_fills: list[Fill], outcomes: dict[str, bool]
) -> float:
    pnl = _fill_settlement_pnl(combo_fill, _resolve_combo(legs, outcomes))
    for hf in hedge_fills:
        underlying_yes = outcomes[hf.instrument]
        resolves = underlying_yes if hf.side == Side.YES else (not underlying_yes)
        pnl += _fill_settlement_pnl(hf, resolves)
    return pnl


def _scenario_pnl(trade: HedgedTrade, outcomes: dict[str, bool]) -> float:
    return _trade_pnl(trade.signal.legs, trade.combo_fill, trade.hedge_fills, outcomes)


def settle_pnl(
    legs: list[ComboLeg], combo_fill: Fill, hedge_fills: list[Fill], outcomes: dict[str, bool]
) -> float:
    """Realized PnL from ACTUAL settlement outcomes (not a Monte-Carlo draw).

    Same AND-rule math as the trade-time estimate in :func:`simulate_pnl`, but driven
    by each leg's real resolved result once its market has actually settled.
    """
    return _trade_pnl(legs, combo_fill, hedge_fills, outcomes)


def simulate_pnl(
    trade: HedgedTrade, n_scenarios: int = 2000, seed: int = 42
) -> dict[str, float]:
    """Monte-Carlo the settlement PnL distribution for a hedged trade."""
    rng = random.Random(seed)
    pnls: list[float] = []
    tickers = list(trade.leg_probs.keys())
    for _ in range(n_scenarios):
        outcomes = {t: rng.random() < trade.leg_probs[t] for t in tickers}
        pnls.append(_scenario_pnl(trade, outcomes))

    mean = statistics.fmean(pnls)
    std = statistics.pstdev(pnls) if len(pnls) > 1 else 0.0
    wins = sum(1 for p in pnls if p > 0)
    return {
        "expected_pnl": mean,
        "pnl_std": std,
        "win_rate": wins / len(pnls) if pnls else 0.0,
        "min_pnl": min(pnls) if pnls else 0.0,
        "max_pnl": max(pnls) if pnls else 0.0,
        "immediate_cash": immediate_cash(trade),
    }


def expected_pnl_for_orders(
    signal: ArbSignal,
    orders: list[Order],
    leg_probs: dict[str, float],
    cfg: AppConfig,
    n_scenarios: int = 2000,
    seed: int = 42,
) -> float:
    """Pre-trade estimate of the full hedged-package expected PnL for a set of
    INTENDED orders, before they are placed.

    The scanner's per-contract combo edge (arbitrage_margin) is computed before the
    hedge is sized/rounded to whole contracts, so a signal can clear that threshold
    while the honest full-package number here is negative (rounding, per-leg fees,
    and the AND-rule's negative convexity all bite only once the actual hedge is
    built). This lets the controller check the real number BEFORE executing, instead
    of only finding out after the trade is already placed.

    Fees are estimated via ``taker_fee`` on each order's own price/qty -- the same
    estimate used elsewhere before real fill fees are known; live/paper reconcile
    the true fee once fills exist, but that's not available pre-trade.
    """
    combo_order = next((o for o in orders if o.instrument_type == InstrumentType.COMBO), None)
    if combo_order is None:
        return 0.0
    combo_fill = Fill(
        order_id="prospective", instrument=combo_order.instrument,
        instrument_type=InstrumentType.COMBO, side=combo_order.side,
        action=combo_order.action, price=combo_order.price, qty=combo_order.qty,
        fee=taker_fee(combo_order.price, combo_order.qty, cfg.fees),
    )
    hedge_fills = [
        Fill(
            order_id="prospective", instrument=o.instrument, instrument_type=o.instrument_type,
            side=o.side, action=o.action, price=o.price, qty=o.qty,
            fee=taker_fee(o.price, o.qty, cfg.fees),
        )
        for o in orders if o is not combo_order
    ]
    trade = HedgedTrade(
        signal=signal, combo_fill=combo_fill, hedge_fills=hedge_fills, leg_probs=leg_probs,
    )
    return simulate_pnl(trade, n_scenarios=n_scenarios, seed=seed)["expected_pnl"]
