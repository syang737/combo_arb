"""Settlement sweep.

Paper trades never closed on their own: nothing checked whether an open trade's
underlying markets had actually resolved, so ``RiskManager.open_signals``
(incremented per trade, never decremented) behaved like a one-shot lifetime cap
per process run instead of a true concurrency limit. This sweep closes that gap:
once every leg of an open trade has settled, it realizes actual PnL (replacing the
Monte-Carlo estimate taken at trade-open time) so the caller can free the trade's
risk slot.

Only the LEG markets are polled -- the combo's own payoff is fully determined by
its legs (AND rule; see ``execution/settlement.py``), so the combo's own ticker
(which, under RFQ discovery, is a collection-level ticker shared across many
combos and not itself directly settleable) never needs to be queried.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from combo_arb.execution.settlement import settle_pnl
from combo_arb.kalshi.base import MarketDataClient
from combo_arb.models import ComboLeg
from combo_arb.persistence.db import Database

log = logging.getLogger(__name__)


@dataclass
class SettledTrade:
    signal_ref: str
    mve_collection_ticker: str
    realized_pnl: float
    expected_pnl: float


def _market_result(market: dict) -> Optional[bool]:
    """True/False once a market has actually settled; None while still open/closed."""
    if (market.get("status") or "").lower() != "settled":
        return None
    result = (market.get("result") or "").lower()
    if result == "yes":
        return True
    if result == "no":
        return False
    return None


def sweep_settlements(client: MarketDataClient, db: Database) -> list[SettledTrade]:
    """Poll leg markets for every open trade; realize PnL for any fully resolved."""
    get_market = getattr(client, "get_market", None)
    if get_market is None:
        return []  # data source can't report settlement (e.g. offline/mock)

    open_trades = db.get_open_trades()
    if not open_trades:
        return []

    tickers = {
        leg["leg_ticker"] for row in open_trades for leg in json.loads(row["legs_json"])
    }
    outcomes: dict[str, Optional[bool]] = {}
    for ticker in tickers:
        try:
            outcomes[ticker] = _market_result(get_market(ticker))
        except Exception as exc:  # network/API hiccup -- retried next sweep
            log.warning("settlement check failed for %s: %s", ticker, exc)
            outcomes[ticker] = None

    settled: list[SettledTrade] = []
    for row in open_trades:
        legs = [ComboLeg(**leg) for leg in json.loads(row["legs_json"])]
        trade_outcomes = {leg.leg_ticker: outcomes.get(leg.leg_ticker) for leg in legs}
        if any(v is None for v in trade_outcomes.values()):
            continue  # at least one leg hasn't settled yet

        combo_fill, hedge_fills = db.get_trade_fills(
            row["signal_ref"], row["mve_collection_ticker"]
        )
        if combo_fill is None:
            log.warning(
                "open trade %s has no combo fill on record; settling at 0 pnl",
                row["signal_ref"],
            )
            realized = 0.0
        else:
            realized = settle_pnl(legs, combo_fill, hedge_fills, trade_outcomes)

        db.settle_open_trade(row["signal_ref"], settled_ts=time.time(), realized_pnl=realized)
        settled.append(
            SettledTrade(
                signal_ref=row["signal_ref"],
                mve_collection_ticker=row["mve_collection_ticker"],
                realized_pnl=realized,
                expected_pnl=row["expected_pnl"] or 0.0,
            )
        )
        log.info(
            "settled %s (%s): realized pnl %.4f",
            row["signal_ref"], row["mve_collection_ticker"], realized,
        )

    if settled:
        db.commit()
    return settled
