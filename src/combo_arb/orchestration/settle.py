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
    expired: bool = False  # closed because a leg was permanently un-fetchable, not resolved


# Kalshi's terminal (resolved) market states. The API finalizes a market as
# "finalized"; "settled" is accepted defensively for any series/version that
# reports the older string. Anything else (active/closed/...) is not yet resolved.
_RESOLVED_STATUSES = {"finalized", "settled"}


def _market_result(market: dict) -> Optional[bool]:
    """True/False once a market has actually resolved; None while still open/closed."""
    if (market.get("status") or "").lower() not in _RESOLVED_STATUSES:
        return None
    result = (market.get("result") or "").lower()
    if result == "yes":
        return True
    if result == "no":
        return False
    return None  # resolved but void / no yes-no outcome -> leave unsettled


def sweep_settlements(
    client: MarketDataClient, db: Database, max_open_age_s: float = 0.0
) -> list[SettledTrade]:
    """Poll leg markets for every open trade; realize PnL for any fully resolved.

    A trade older than ``max_open_age_s`` (if > 0) that still has a leg which can't
    be fetched (delisted / rolled-off market that errors instead of settling) is
    marked *expired* so it stops holding a risk slot forever.
    """
    get_market = getattr(client, "get_market", None)
    if get_market is None:
        return []  # data source can't report settlement (e.g. offline/mock)

    open_trades = db.get_open_trades()
    if not open_trades:
        return []

    tickers = {
        leg["leg_ticker"] for row in open_trades for leg in json.loads(row["legs_json"])
    }
    outcomes: dict[str, Optional[bool]] = {}  # None = fetched-but-unresolved or errored
    errored: set[str] = set()                 # fetch raised -> leg may be delisted
    for ticker in tickers:
        try:
            outcomes[ticker] = _market_result(get_market(ticker))
        except Exception as exc:  # network/API hiccup -- retried next sweep
            log.warning("settlement check failed for %s: %s", ticker, exc)
            outcomes[ticker] = None
            errored.add(ticker)

    now = time.time()
    settled: list[SettledTrade] = []
    n_expired = 0
    for row in open_trades:
        legs = [ComboLeg(**leg) for leg in json.loads(row["legs_json"])]
        trade_outcomes = {leg.leg_ticker: outcomes.get(leg.leg_ticker) for leg in legs}

        if any(v is None for v in trade_outcomes.values()):
            # Not fully resolved. Expire only if the trade is old AND stuck on a leg
            # we genuinely can't fetch (not merely a not-yet-resolved future game).
            stuck = any(leg.leg_ticker in errored for leg in legs)
            opened = row["opened_ts"]
            age = (now - opened) if opened is not None else 0.0
            if max_open_age_s and stuck and age > max_open_age_s:
                db.expire_open_trade(row["signal_ref"], settled_ts=now)
                settled.append(SettledTrade(
                    signal_ref=row["signal_ref"],
                    mve_collection_ticker=row["mve_collection_ticker"],
                    realized_pnl=0.0,
                    expected_pnl=row["expected_pnl"] or 0.0,
                    expired=True,
                ))
                n_expired += 1
                log.warning(
                    "EXPIRED open trade %s (%s): un-fetchable leg after %.0fh; freeing "
                    "risk slot, realized pnl unknown -> recorded 0. Legs: %s",
                    row["signal_ref"], row["mve_collection_ticker"], age / 3600.0,
                    [leg.leg_ticker for leg in legs if leg.leg_ticker in errored],
                )
            continue  # otherwise leave open; retried next sweep

        combo_fill, hedge_fills = db.get_trade_fills(row["signal_ref"])
        if combo_fill is None:
            log.warning(
                "open trade %s has no combo fill on record; settling at 0 pnl",
                row["signal_ref"],
            )
            realized = 0.0
        else:
            realized = settle_pnl(legs, combo_fill, hedge_fills, trade_outcomes)

        db.settle_open_trade(
            row["signal_ref"], settled_ts=now, realized_pnl=realized,
            outcomes_json=json.dumps({t: bool(v) for t, v in trade_outcomes.items()}),
        )
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

    # Per-sweep heartbeat: proves the sweep ran and shows why trades are/aren't closing.
    resolved_legs = sum(1 for t, v in outcomes.items() if v is not None)
    pending_legs = sum(1 for t, v in outcomes.items() if v is None and t not in errored)
    log.info(
        "settlement sweep: %d open, legs %d resolved/%d pending/%d error, "
        "%d settled, %d expired",
        len(open_trades), resolved_legs, pending_legs, len(errored),
        len(settled) - n_expired, n_expired,
    )

    if settled:
        db.commit()
    return settled
