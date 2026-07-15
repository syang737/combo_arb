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

from combo_arb.models import ArbSignal, Fill, Side


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


def _scenario_pnl(trade: HedgedTrade, outcomes: dict[str, bool]) -> float:
    sig = trade.signal
    # Combo resolves YES iff every leg resolves in the combo's favour.
    combo_yes = True
    for leg in sig.legs:
        underlying_yes = outcomes[leg.leg_ticker]
        leg_ok = underlying_yes if leg.side == Side.YES else (not underlying_yes)
        combo_yes = combo_yes and leg_ok

    pnl = _fill_settlement_pnl(trade.combo_fill, combo_yes)
    for hf in trade.hedge_fills:
        underlying_yes = outcomes[hf.instrument]
        resolves = underlying_yes if hf.side == Side.YES else (not underlying_yes)
        pnl += _fill_settlement_pnl(hf, resolves)
    return pnl


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
