"""PnL backfill: recompute realized_pnl for trades settled under the combo-fill
misclassification bug, and rebuild the pnl event log/equity curve to match.
"""

from __future__ import annotations

import json

import pytest

from combo_arb.models import Fill, InstrumentType, Order, OrderStatus, PnL, Side
from combo_arb.orchestration.backfill import rebuild_history
from combo_arb.persistence.db import Database


def _seed_settled_trade(
    db: Database, signal_ref: str, *, stored_realized_pnl: float,
    opened_ts: float, settled_ts: float, expected_pnl: float = 0.04,
) -> None:
    """A settled trade mirroring the user-reported case: combo YES qty=3 @ 0.346,
    hedged by buying NO on two legs (qty=1 @ 0.170, qty=3 @ 0.530); both legs resolved
    YES. True realized PnL is ~+0.1195; ``stored_realized_pnl`` lets us simulate the
    historical bug's wrong value (e.g. 0.0) independent of what's actually correct.
    """
    combo_ticker = f"{signal_ref}-MARKET"
    collection_ticker = f"{signal_ref}-COLLECTION"  # deliberately different
    orders = [
        Order(instrument=combo_ticker, instrument_type=InstrumentType.COMBO, side=Side.YES,
              action="buy", price=0.346, qty=3, signal_ref=signal_ref, order_id=signal_ref + "-c",
              status=OrderStatus.FILLED),
        Order(instrument="FRITZ", instrument_type=InstrumentType.LEG, side=Side.NO,
              action="buy", price=0.170, qty=1, signal_ref=signal_ref, order_id=signal_ref + "-f",
              status=OrderStatus.FILLED),
        Order(instrument="COCC", instrument_type=InstrumentType.LEG, side=Side.NO,
              action="buy", price=0.530, qty=3, signal_ref=signal_ref, order_id=signal_ref + "-k",
              status=OrderStatus.FILLED),
    ]
    fills = [
        Fill(order_id=signal_ref + "-c", instrument=combo_ticker, instrument_type=InstrumentType.COMBO,
             side=Side.YES, action="buy", price=0.346, qty=3, fee=0.0125),
        Fill(order_id=signal_ref + "-f", instrument="FRITZ", side=Side.NO, action="buy",
             price=0.170, qty=1, fee=0.01),
        Fill(order_id=signal_ref + "-k", instrument="COCC", side=Side.NO, action="buy",
             price=0.530, qty=3, fee=0.06),
    ]
    for o in orders:
        db.insert_order(o)
    for f in fills:
        db.insert_fill(f)
    db.insert_open_trade(
        signal_ref=signal_ref, mve_collection_ticker=collection_ticker,
        legs_json=json.dumps([
            {"leg_ticker": "FRITZ", "side": "yes"}, {"leg_ticker": "COCC", "side": "yes"},
        ]),
        opened_ts=opened_ts, expected_pnl=expected_pnl,
    )
    db.settle_open_trade(
        signal_ref, settled_ts=settled_ts, realized_pnl=stored_realized_pnl,
        outcomes_json=json.dumps({"FRITZ": True, "COCC": True}),
    )


_TRUE_REALIZED = pytest.approx((3 - 3 * 0.346) - 1 * 0.170 - 3 * 0.530 - (0.0125 + 0.01 + 0.06))


def test_backfill_corrects_wrong_realized_pnl(tmp_path):
    path = str(tmp_path / "bf.db")
    db = Database(path)
    # Simulates the historical bug: realized_pnl wrongly recorded as 0.0 at settlement.
    _seed_settled_trade(db, "t1", stored_realized_pnl=0.0, opened_ts=100.0, settled_ts=200.0)
    db.commit()

    report = rebuild_history(db)

    assert report.trades_scanned == 1
    assert report.realized_pnl_corrected == 1
    row = db.conn.execute(
        "SELECT realized_pnl FROM open_trades WHERE signal_ref='t1'"
    ).fetchone()
    assert row["realized_pnl"] == _TRUE_REALIZED
    db.close()


def test_backfill_leaves_correct_rows_alone(tmp_path):
    """If realized_pnl is already correct, nothing is 'corrected' (idempotent no-op)."""
    path = str(tmp_path / "bf2.db")
    db = Database(path)
    true_val = (3 - 3 * 0.346) - 1 * 0.170 - 3 * 0.530 - (0.0125 + 0.01 + 0.06)
    _seed_settled_trade(db, "t1", stored_realized_pnl=true_val, opened_ts=100.0, settled_ts=200.0)
    db.commit()

    report = rebuild_history(db)

    assert report.realized_pnl_corrected == 0
    db.close()


def test_backfill_is_idempotent(tmp_path):
    path = str(tmp_path / "bf3.db")
    db = Database(path)
    _seed_settled_trade(db, "t1", stored_realized_pnl=0.0, opened_ts=100.0, settled_ts=200.0)
    db.commit()

    first = rebuild_history(db)
    cols = "ts, realized, unrealized, equity"  # exclude autoincrement id, which legitimately
                                                # advances on each delete+reinsert pass
    rows_after_first = db.conn.execute(f"SELECT {cols} FROM pnl ORDER BY ts").fetchall()
    second = rebuild_history(db)
    rows_after_second = db.conn.execute(f"SELECT {cols} FROM pnl ORDER BY ts").fetchall()

    assert first.realized_pnl_corrected == 1
    assert second.realized_pnl_corrected == 0  # already correct on the 2nd pass
    assert [tuple(r) for r in rows_after_first] == [tuple(r) for r in rows_after_second]
    db.close()


def test_pnl_history_rebuilt_with_correct_equity(tmp_path):
    """The pnl table's equity curve reflects the CORRECTED realized_pnl, not the wrong
    stored value -- this is the 'keep the pnl graph consistent' requirement."""
    path = str(tmp_path / "bf4.db")
    db = Database(path)
    _seed_settled_trade(db, "t1", stored_realized_pnl=0.0, opened_ts=100.0, settled_ts=200.0,
                       expected_pnl=0.04)
    db.commit()

    rebuild_history(db)

    rows = db.conn.execute("SELECT ts, realized, unrealized, equity FROM pnl ORDER BY ts").fetchall()
    assert len(rows) == 2  # one open-time event, one settle-time event
    open_row, settle_row = rows
    # open-time: cash = 3*(-0.346)-0.0125 (combo buy) + 1*(-0.170)-0.01 (leg) + 3*(-0.530)-0.06 (leg)
    combo_cash = 3 * -0.346 - 0.0125
    leg1_cash = 1 * -0.170 - 0.01
    leg2_cash = 3 * -0.530 - 0.06
    open_cash = combo_cash + leg1_cash + leg2_cash
    assert open_row["realized"] == pytest.approx(open_cash)
    assert open_row["equity"] == pytest.approx(0.04)  # equity_delta = +expected_pnl
    # settle-time: realized = TRUE realized (not the wrong stored 0.0)
    assert settle_row["realized"] == _TRUE_REALIZED
    # equity = open_equity + (true_realized - expected_pnl), NOT the old (0.0 - expected_pnl)
    assert settle_row["equity"] == pytest.approx(0.04 + (settle_row["realized"] - 0.04))
    db.close()


def test_open_trade_contributes_only_open_event(tmp_path):
    """A still-open trade has no settlement yet -- only its open-time cash event should
    appear in the rebuilt pnl history, and its realized_pnl is left untouched (None)."""
    path = str(tmp_path / "bf5.db")
    db = Database(path)
    combo_ticker, collection = "t1-MARKET", "t1-COLLECTION"
    db.insert_order(Order(instrument=combo_ticker, instrument_type=InstrumentType.COMBO,
                          side=Side.YES, action="buy", price=0.1, qty=5, signal_ref="t1", order_id="oc"))
    db.insert_fill(Fill(order_id="oc", instrument=combo_ticker, instrument_type=InstrumentType.COMBO,
                        side=Side.YES, action="buy", price=0.1, qty=5, fee=0.01))
    db.insert_open_trade(signal_ref="t1", mve_collection_ticker=collection, legs_json="[]",
                         opened_ts=50.0, expected_pnl=0.5)
    db.commit()

    report = rebuild_history(db)

    assert report.realized_pnl_corrected == 0
    rows = db.conn.execute("SELECT realized, equity FROM pnl").fetchall()
    assert len(rows) == 1
    assert rows[0]["equity"] == pytest.approx(0.5)
    row = db.conn.execute("SELECT realized_pnl FROM open_trades WHERE signal_ref='t1'").fetchone()
    assert row["realized_pnl"] is None
    db.close()


def test_missing_outcomes_leaves_realized_pnl_unchanged(tmp_path):
    """A settled row that predates outcome persistence (no outcomes_json) can't be
    recomputed -- its existing realized_pnl must be left as-is, not zeroed or guessed."""
    path = str(tmp_path / "bf6.db")
    db = Database(path)
    db.insert_order(Order(instrument="C", instrument_type=InstrumentType.COMBO, side=Side.YES,
                          action="buy", price=0.1, qty=3, signal_ref="t1", order_id="oc"))
    db.insert_fill(Fill(order_id="oc", instrument="C", instrument_type=InstrumentType.COMBO,
                        side=Side.YES, action="buy", price=0.1, qty=3, fee=0.01))
    db.insert_open_trade(signal_ref="t1", mve_collection_ticker="COLL", legs_json="[]",
                         opened_ts=10.0, expected_pnl=0.1)
    # settle WITHOUT outcomes_json (simulates a pre-outcomes-tracking historical row)
    db.settle_open_trade("t1", settled_ts=20.0, realized_pnl=1.23, outcomes_json=None)
    db.commit()

    report = rebuild_history(db)

    assert report.skipped_missing_outcomes == 1
    assert report.realized_pnl_corrected == 0
    row = db.conn.execute("SELECT realized_pnl FROM open_trades WHERE signal_ref='t1'").fetchone()
    assert row["realized_pnl"] == 1.23  # untouched
    db.close()
