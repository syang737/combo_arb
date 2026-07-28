"""Settlement sweep: recognizing resolved markets and closing open trades.

Regression guard for the bug where the sweep only accepted Kalshi ``status ==
"settled"`` while the API actually finalizes markets as ``"finalized"`` -- which
left every open trade unsettled forever, filling the ``max_open_signals`` cap and
silently halting trading.
"""

from __future__ import annotations

import json
import time

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
    db: Database, signal_ref: str, combo_ticker: str, opened_ts: float = 0.0
) -> None:
    """Persist one hedged trade (combo buy + two leg hedges) as an open trade."""
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
        mve_collection_ticker=combo_ticker,
        legs_json=json.dumps([{"leg_ticker": "A", "side": "yes"}, {"leg_ticker": "B", "side": "yes"}]),
        opened_ts=opened_ts,
        expected_pnl=1.23,
    )
    db.commit()


def test_finalized_legs_close_the_trade():
    db = Database(":memory:")
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
    assert db.is_trade_open("t1") is False
    assert db.count_open_trades() == 0


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
