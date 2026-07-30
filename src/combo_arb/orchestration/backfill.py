"""One-time backfill: recompute realized PnL for trades settled before the
combo-fill-misclassification fix, and rebuild the pnl event log so pnl_summary /
pnl_series (the dashboard's PnL tiles + equity curve) stay consistent with the
corrected numbers.

Background: ``Database.get_trade_fills`` used to classify a trade's combo fill by
comparing its ticker to ``mve_collection_ticker`` (a collection-level ticker), but the
combo order's real ``instrument`` is the tradeable ``market_ticker`` -- a different
string for every real Kalshi MVE combo. So the combo fill was never found, and
``sweep_settlements`` fell back to ``realized_pnl = 0.0`` regardless of the true
outcome. That bug is fixed (classification now uses ``orders.instrument_type``), but
already-settled trades still carry the wrong (usually 0.0) realized_pnl in the DB, and
every pnl row inserted at settlement time carries the same wrong number -- which,
because ``equity`` is a running cumulative sum, also skews every later equity value.

This module recomputes the truth from source data (fills + the leg outcomes already
persisted in ``open_trades.outcomes_json``) and rebuilds the pnl table from scratch, so
there is no need to guess which historical pnl row belongs to which trade (the pnl
table itself has no such link, and nothing besides the controller ever wrote to it).
Idempotent: safe to run more than once, and running it on a DB with no bug present is a
no-op (recomputed values match what's already stored).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from combo_arb.execution.settlement import HedgedTrade, immediate_cash, settle_pnl
from combo_arb.models import ComboLeg
from combo_arb.persistence.db import Database

log = logging.getLogger(__name__)

_EPS = 1e-9


@dataclass
class BackfillReport:
    trades_scanned: int = 0
    realized_pnl_corrected: int = 0
    pnl_rows_written: int = 0
    skipped_missing_fills: int = 0
    skipped_missing_outcomes: int = 0


def rebuild_history(db: Database) -> BackfillReport:
    """Recompute realized_pnl for every settled trade and rebuild the pnl event log.

    For every trade (any status), reconstructs its open-time cash movement from fills
    (unaffected by the bug -- this was always classified correctly) and, for settled
    trades, its true realized PnL from the stored leg outcomes. Replaces the pnl table
    with a freshly computed, chronologically ordered event log so the running equity
    curve reflects the corrections.
    """
    report = BackfillReport()
    trades = db.get_all_trades()
    report.trades_scanned = len(trades)

    events: list[tuple[float, float, float, float]] = []  # ts, realized, unrealized, equity_delta

    for row in trades:
        combo_fill, hedge_fills = db.get_trade_fills(row["signal_ref"])
        if combo_fill is None:
            # No fills on record at all for this trade (shouldn't happen in practice;
            # a trade is only persisted after its combo fill is confirmed) -- can't
            # reconstruct its open-time cash movement, so it's left out of the replay.
            report.skipped_missing_fills += 1
            log.warning(
                "backfill: %s has no combo fill on record; excluding from pnl rebuild",
                row["signal_ref"],
            )
            continue

        expected = row["expected_pnl"] or 0.0
        cash = immediate_cash(HedgedTrade(
            signal=None, combo_fill=combo_fill, hedge_fills=hedge_fills, leg_probs={},
        ))
        events.append((row["opened_ts"], cash, expected - cash, expected))

        if row["status"] == "settled":
            try:
                outcomes = json.loads(row["outcomes_json"]) if row["outcomes_json"] else {}
                legs = [ComboLeg(**leg) for leg in json.loads(row["legs_json"] or "[]")]
            except (TypeError, ValueError):
                outcomes, legs = {}, []
            if legs and outcomes and all(leg.leg_ticker in outcomes for leg in legs):
                realized = settle_pnl(legs, combo_fill, hedge_fills, outcomes)
            else:
                # Predates outcome persistence or legs_json is empty -- can't redo the
                # math; keep whatever is already recorded rather than guessing.
                realized = row["realized_pnl"] or 0.0
                report.skipped_missing_outcomes += 1
            if row["realized_pnl"] is None or abs(realized - row["realized_pnl"]) > _EPS:
                db.update_trade_realized_pnl(row["signal_ref"], realized)
                report.realized_pnl_corrected += 1
            events.append((row["settled_ts"], realized, -expected, realized - expected))
        elif row["status"] == "expired":
            # Outcome genuinely unknown (leg was un-fetchable) -- nothing to recompute.
            realized = row["realized_pnl"] or 0.0
            events.append((row["settled_ts"], realized, -expected, realized - expected))
        # status == "open": only the open-time event above applies; no settle event yet.

    events.sort(key=lambda e: e[0])
    cum_equity = 0.0
    pnl_rows: list[tuple[float, float, float, float]] = []
    for ts, realized, unrealized, equity_delta in events:
        cum_equity += equity_delta
        pnl_rows.append((ts, realized, unrealized, cum_equity))

    db.replace_pnl_history(pnl_rows)
    db.commit()
    report.pnl_rows_written = len(pnl_rows)
    log.info(
        "pnl backfill: %d trades scanned, %d realized_pnl corrected, %d pnl rows "
        "rebuilt (%d skipped: no fills, %d skipped: no outcomes)",
        report.trades_scanned, report.realized_pnl_corrected, report.pnl_rows_written,
        report.skipped_missing_fills, report.skipped_missing_outcomes,
    )
    return report
