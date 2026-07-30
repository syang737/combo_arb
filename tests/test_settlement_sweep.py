"""Settlement sweep: recognizing resolved markets and closing open trades.

Regression guard for the bug where the sweep only accepted Kalshi ``status ==
"settled"`` while the API actually finalizes markets as ``"finalized"`` -- which
left every open trade unsettled forever, filling the ``max_open_signals`` cap and
silently halting trading.
"""

from __future__ import annotations

import json
import time

import pytest

from combo_arb.models import Fill, InstrumentType, Order, OrderStatus, Side
from combo_arb.orchestration.settle import _market_result, sweep_settlements
from combo_arb.persistence.db import Database


# -- _market_result: which statuses count as resolved --------------------------
def test_finalized_market_is_resolved():
    # The real Kalshi terminal status (the bug: this used to return None).
    assert _market_result({"status": "finalized", "result": "yes"}) is True
    assert _market_result({"status": "finalized", "result": "no"}) is False


def test_settled_still_accepted():
    # Older/defensive string stays supported.
    assert _market_result({"status": "settled", "result": "yes"}) is True
    assert _market_result({"status": "settled", "result": "no"}) is False


def test_unresolved_or_void_returns_none():
    assert _market_result({"status": "active", "result": ""}) is None
    assert _market_result({"status": "closed", "result": ""}) is None
    # Resolved but with no yes/no outcome (voided) -> leave unsettled.
    assert _market_result({"status": "finalized", "result": ""}) is None
    assert _market_result({"status": "finalized", "result": "void"}) is None


# -- sweep_settlements: a finalized trade actually closes ----------------------
class _FakeClient:
    """Minimal market-data client: returns a canned status/result per ticker.
    A ticker mapped to an Exception raises it (simulates a delisted/errored leg)."""

    def __init__(self, markets: dict[str, object]):
        self._markets = markets

    def get_market(self, ticker: str) -> dict:
        m = self._markets[ticker]
        if isinstance(m, Exception):
            raise m
        return m


def _seed_open_trade(
    db: Database, signal_ref: str, combo_ticker: str, opened_ts: float = 0.0,
    mve_collection_ticker: str | None = None,
) -> None:
    """Persist one hedged trade (combo buy + two leg hedges) as an open trade.

    ``combo_ticker`` is the tradeable MARKET ticker (the combo order's actual
    ``instrument``); ``mve_collection_ticker`` defaults to a DIFFERENT string, matching
    real Kalshi MVE combos where the two are distinct -- this is deliberate so tests
    exercise (and would catch a regression of) the real-world ticker mismatch, rather
    than accidentally using the same string for both as earlier fixtures did.
    """
    if mve_collection_ticker is None:
        mve_collection_ticker = combo_ticker + "-COLLECTION"
    orders = [
        Order(instrument=combo_ticker, instrument_type=InstrumentType.COMBO, side=Side.YES,
              action="buy", price=0.10, qty=10, signal_ref=signal_ref, order_id="o-combo",
              status=OrderStatus.FILLED),
        Order(instrument="A", instrument_type=InstrumentType.LEG, side=Side.NO,
              action="buy", price=0.50, qty=5, signal_ref=signal_ref, order_id="o-a",
              status=OrderStatus.FILLED),
        Order(instrument="B", instrument_type=InstrumentType.LEG, side=Side.NO,
              action="buy", price=0.60, qty=5, signal_ref=signal_ref, order_id="o-b",
              status=OrderStatus.FILLED),
    ]
    fills = [
        Fill(order_id="o-combo", instrument=combo_ticker, instrument_type=InstrumentType.COMBO,
             side=Side.YES, action="buy", price=0.10, qty=10, fee=0.01),
        Fill(order_id="o-a", instrument="A", side=Side.NO, action="buy", price=0.50, qty=5, fee=0.01),
        Fill(order_id="o-b", instrument="B", side=Side.NO, action="buy", price=0.60, qty=5, fee=0.01),
    ]
    for o in orders:
        db.insert_order(o)
    for f in fills:
        db.insert_fill(f)
    db.insert_open_trade(
        signal_ref=signal_ref,
        mve_collection_ticker=mve_collection_ticker,
        legs_json=json.dumps([{"leg_ticker": "A", "side": "yes"}, {"leg_ticker": "B", "side": "yes"}]),
        opened_ts=opened_ts,
        expected_pnl=1.23,
    )
    db.commit()


def test_finalized_legs_close_the_trade():
    db = Database(":memory:")
    # Note: _seed_open_trade's combo_ticker ("COMBO_AB") and its default
    # mve_collection_ticker ("COMBO_AB-COLLECTION") are DELIBERATELY different, as they
    # are for every real Kalshi MVE combo -- this is what regression-tests the
    # combo-fill-misclassification bug (get_trade_fills used to match on
    # mve_collection_ticker and would silently find no combo fill, always zeroing
    # realized_pnl).
    _seed_open_trade(db, "t1", "COMBO_AB")
    assert db.is_trade_open("t1") is True

    client = _FakeClient({
        "A": {"status": "finalized", "result": "yes"},
        "B": {"status": "finalized", "result": "yes"},
    })
    settled = sweep_settlements(client, db)

    assert len(settled) == 1
    assert settled[0].signal_ref == "t1"
    assert settled[0].expected_pnl == 1.23
    # combo buy YES 10@0.10 fee .01 -> 10*(1-.10)-.01 = 8.99
    # leg A buy NO 5@0.50 fee .01, underlying YES -> NO doesn't pay -> 5*(0-.50)-.01 = -2.51
    # leg B buy NO 5@0.60 fee .01, underlying YES -> NO doesn't pay -> 5*(0-.60)-.01 = -3.01
    # total = 8.99 - 2.51 - 3.01 = 3.47 (NOT 0.0 -- the bug this guards against)
    assert settled[0].realized_pnl == pytest.approx(3.47)
    assert db.is_trade_open("t1") is False
    assert db.count_open_trades() == 0
    # actual leg resolutions are persisted for later PnL attribution
    row = db.conn.execute("SELECT outcomes_json, realized_pnl FROM open_trades WHERE signal_ref='t1'").fetchone()
    assert json.loads(row["outcomes_json"]) == {"A": True, "B": True}
    assert row["realized_pnl"] == pytest.approx(3.47)


def test_realized_pnl_matches_user_reported_trade():
    """Regression test for the exact trade a user reported: combo YES qty=3 @ 0.346,
    hedged by buying NO on two legs (qty=1 @ 0.170, qty=3 @ 0.530); both legs resolve
    YES so the combo pays and both NO hedges lose their stake. Dashboard showed $0.00
    realized because get_trade_fills couldn't find the combo fill (ticker mismatch
    between the order's market_ticker and the trade's mve_collection_ticker); the true
    gross PnL is ~+$0.202 before fees.
    """
    db = Database(":memory:")
    signal_ref = "t-user"
    combo_ticker = "KXMVETENNIS-S123"        # the tradeable market_ticker
    mve_collection_ticker = "KXMVETENNIS"     # a different collection-level ticker
    orders = [
        Order(instrument=combo_ticker, instrument_type=InstrumentType.COMBO, side=Side.YES,
              action="buy", price=0.346, qty=3, signal_ref=signal_ref, order_id="o-combo",
              status=OrderStatus.FILLED),
        Order(instrument="FRITZ", instrument_type=InstrumentType.LEG, side=Side.NO,
              action="buy", price=0.170, qty=1, signal_ref=signal_ref, order_id="o-fritz",
              status=OrderStatus.FILLED),
        Order(instrument="COCCIARETTO", instrument_type=InstrumentType.LEG, side=Side.NO,
              action="buy", price=0.530, qty=3, signal_ref=signal_ref, order_id="o-cocc",
              status=OrderStatus.FILLED),
    ]
    fills = [
        Fill(order_id="o-combo", instrument=combo_ticker, instrument_type=InstrumentType.COMBO,
             side=Side.YES, action="buy", price=0.346, qty=3, fee=0.0125),
        Fill(order_id="o-fritz", instrument="FRITZ", side=Side.NO, action="buy",
             price=0.170, qty=1, fee=0.01),
        Fill(order_id="o-cocc", instrument="COCCIARETTO", side=Side.NO, action="buy",
             price=0.530, qty=3, fee=0.06),
    ]
    for o in orders:
        db.insert_order(o)
    for f in fills:
        db.insert_fill(f)
    db.insert_open_trade(
        signal_ref=signal_ref, mve_collection_ticker=mve_collection_ticker,
        legs_json=json.dumps([
            {"leg_ticker": "FRITZ", "side": "yes"}, {"leg_ticker": "COCCIARETTO", "side": "yes"},
        ]),
        opened_ts=0.0, expected_pnl=0.04,
    )
    db.commit()

    client = _FakeClient({
        "FRITZ": {"status": "finalized", "result": "yes"},
        "COCCIARETTO": {"status": "finalized", "result": "yes"},
    })
    settled = sweep_settlements(client, db)

    assert len(settled) == 1
    # gross = (3 - 3*0.346) - 1*0.170 - 3*0.530 = 0.202; minus fees (0.0125+0.01+0.06=0.0825)
    gross = (3 - 3 * 0.346) - 1 * 0.170 - 3 * 0.530
    expected = gross - (0.0125 + 0.01 + 0.06)
    assert settled[0].realized_pnl == pytest.approx(expected)
    assert settled[0].realized_pnl > 0    # NOT $0.00 -- the reported bug


def test_unresolved_leg_keeps_trade_open():
    db = Database(":memory:")
    _seed_open_trade(db, "t2", "COMBO_AB")

    client = _FakeClient({
        "A": {"status": "finalized", "result": "yes"},
        "B": {"status": "closed", "result": ""},  # one leg not resolved yet
    })
    settled = sweep_settlements(client, db)

    assert settled == []
    assert db.is_trade_open("t2") is True
    assert db.count_open_trades() == 1


# -- terminal handling: an old trade stuck on a delisted (un-fetchable) leg -----
def test_old_trade_with_delisted_leg_expires():
    db = Database(":memory:")
    _seed_open_trade(db, "t3", "COMBO_AB", opened_ts=0.0)  # opened at epoch -> very old

    client = _FakeClient({
        "A": {"status": "finalized", "result": "yes"},
        "B": RuntimeError("Kalshi request failed: 500 server error"),  # delisted -> errors
    })
    settled = sweep_settlements(client, db, max_open_age_s=3600)

    assert len(settled) == 1 and settled[0].expired is True
    assert settled[0].realized_pnl == 0.0
    assert db.is_trade_open("t3") is False
    assert db.count_open_trades() == 0
    status = db.conn.execute(
        "SELECT status, realized_pnl FROM open_trades WHERE signal_ref='t3'"
    ).fetchone()
    assert status["status"] == "expired" and status["realized_pnl"] is None


def test_recent_trade_with_errored_leg_stays_open():
    """A transient fetch error within the grace window must NOT expire the trade."""
    db = Database(":memory:")
    _seed_open_trade(db, "t4", "COMBO_AB", opened_ts=time.time())  # just opened

    client = _FakeClient({
        "A": {"status": "finalized", "result": "yes"},
        "B": RuntimeError("Kalshi request failed: 500 server error"),
    })
    settled = sweep_settlements(client, db, max_open_age_s=3600)

    assert settled == []
    assert db.is_trade_open("t4") is True


def test_expiry_disabled_by_default_keeps_trade_open():
    db = Database(":memory:")
    _seed_open_trade(db, "t5", "COMBO_AB", opened_ts=0.0)

    client = _FakeClient({
        "A": {"status": "finalized", "result": "yes"},
        "B": RuntimeError("boom"),
    })
    settled = sweep_settlements(client, db)  # max_open_age_s defaults to 0 -> disabled

    assert settled == []
    assert db.is_trade_open("t5") is True
