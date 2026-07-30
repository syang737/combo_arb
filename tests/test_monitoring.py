import json
import sqlite3

import pytest

from combo_arb.models import (
    ArbSignal,
    ComboEvaluation,
    ComboLeg,
    Fill,
    InstrumentType,
    LegPrice,
    Order,
    PnL,
    Position,
    Side,
    SignalAction,
)
from combo_arb.monitoring import queries
from combo_arb.persistence.db import Database


@pytest.fixture
def populated_db(tmp_path):
    path = str(tmp_path / "mon.db")
    db = Database(path)
    # A flagged signal + evaluation, and a non-flagged (near-miss) evaluation.
    db.insert_signal(ArbSignal(
        rfq_id="s1", mve_collection_ticker="COMBO_AB",
        legs=[ComboLeg(leg_ticker="A")], leg_prices={"A": LegPrice(leg_ticker="A")},
        combo_quote_yes=0.10, fair_combo=0.20, fees_estimate=0.02,
        margin_threshold=0.02, arbitrage_margin=0.08, size=20,
        action=SignalAction.HEDGE_VIA_LEGS,
    ))
    db.insert_evaluation(ComboEvaluation(
        rfq_id="s1", mve_collection_ticker="COMBO_AB", direction="buy_underpriced",
        combo_quote_yes=0.10, fair_combo=0.20, fees_estimate=0.02, buffer=0.0,
        arbitrage_margin=0.08, flagged=True,
    ))
    db.insert_evaluation(ComboEvaluation(
        rfq_id="n1", mve_collection_ticker="COMBO_XY", direction="buy_underpriced",
        combo_quote_yes=0.18, fair_combo=0.20, fees_estimate=0.02, buffer=0.0,
        arbitrage_margin=-0.02, flagged=False,
    ))
    db.insert_fill(Fill(order_id="o1", instrument="COMBO_AB", side=Side.YES,
                        action="buy", price=0.10, qty=20, fee=0.05))
    db.insert_pnl(PnL(realized=-1.5, unrealized=2.5, equity=1.0))
    db.upsert_position(Position(instrument="A", instrument_type=InstrumentType.LEG,
                               net_qty=8, avg_price=0.51))
    db.commit()
    db.close()
    return path


def test_db_status(populated_db):
    st = queries.db_status(populated_db)
    assert st["exists"] is True
    assert st["row_counts"]["arb_signals"] == 1
    assert st["row_counts"]["combo_evaluations"] == 2
    assert st["last_update_iso"] is not None


def test_pnl_summary(populated_db):
    p = queries.pnl_summary(populated_db)
    assert p["realized"] == pytest.approx(-1.5)
    assert p["unrealized"] == pytest.approx(2.5)
    assert p["equity"] == pytest.approx(1.0)
    assert p["trades"] == 1


def test_recent_signals(populated_db):
    sigs = queries.recent_signals(populated_db, limit=10)
    assert len(sigs) == 1
    assert sigs[0]["mve_collection_ticker"] == "COMBO_AB"
    assert sigs[0]["ts_iso"] is not None


def test_top_near_misses_excludes_flagged(populated_db):
    nm = queries.top_near_misses(populated_db, limit=10)
    tickers = {r["mve_collection_ticker"] for r in nm}
    assert "COMBO_XY" in tickers      # the non-flagged near miss
    assert "COMBO_AB" not in tickers  # flagged -> excluded


def test_open_positions(populated_db):
    pos = queries.open_positions(populated_db)
    assert len(pos) == 1 and pos[0]["instrument"] == "A" and pos[0]["net_qty"] == 8


def test_evaluation_history(populated_db):
    hist = queries.evaluation_history(populated_db, "COMBO_AB", limit=10)
    assert len(hist) == 1 and hist[0]["flagged"] == 1


def test_open_trades_summary(tmp_path):
    path = str(tmp_path / "ot.db")
    db = Database(path)
    db.insert_open_trade(signal_ref="t-open", mve_collection_ticker="C1",
                         legs_json="[]", opened_ts=100.0, expected_pnl=1.0)
    db.insert_open_trade(signal_ref="t-set", mve_collection_ticker="C2",
                         legs_json="[]", opened_ts=50.0, expected_pnl=1.0)
    db.settle_open_trade("t-set", settled_ts=200.0, realized_pnl=2.5)
    db.insert_open_trade(signal_ref="t-exp", mve_collection_ticker="C3",
                         legs_json="[]", opened_ts=60.0, expected_pnl=1.0)
    db.expire_open_trade("t-exp", settled_ts=210.0)
    db.commit()
    db.close()

    s = queries.open_trades_summary(path)
    assert s["open"] == 1 and s["settled"] == 1 and s["expired"] == 1
    assert s["settled_realized_pnl"] == pytest.approx(2.5)
    assert s["oldest_open_signal_ref"] == "t-open"
    assert len(s["recent_settlements"]) == 2


def test_recent_fills_includes_instrument_type(tmp_path):
    """Fills join to their order for instrument_type, so a combo fill (always side=yes)
    can't be confused with a leg fill (side may be no) in the dashboard."""
    path = str(tmp_path / "rf.db")
    db = Database(path)
    db.insert_order(Order(instrument="C", instrument_type=InstrumentType.COMBO, side=Side.YES,
                          action="buy", price=0.1, qty=3, signal_ref="t1", order_id="oc"))
    db.insert_order(Order(instrument="A", instrument_type=InstrumentType.LEG, side=Side.NO,
                          action="buy", price=0.5, qty=1, signal_ref="t1", order_id="oa"))
    db.insert_fill(Fill(order_id="oc", instrument="C", instrument_type=InstrumentType.COMBO,
                        side=Side.YES, action="buy", price=0.1, qty=3, fee=0.0))
    db.insert_fill(Fill(order_id="oa", instrument="A", side=Side.NO,
                        action="buy", price=0.5, qty=1, fee=0.0))
    db.commit()
    db.close()

    rows = {r["instrument"]: r for r in queries.recent_fills(path)}
    assert rows["C"]["instrument_type"] == "combo" and rows["C"]["side"] == "yes"
    assert rows["A"]["instrument_type"] == "leg" and rows["A"]["side"] == "no"


def test_market_names_map(tmp_path):
    path = str(tmp_path / "names.db")
    db = Database(path)
    db.upsert_market_name("A", "Team A wins")
    db.upsert_market_name("A", "Team A wins (updated)")   # idempotent overwrite
    db.upsert_market_name("", "ignored")                   # no-op (empty ticker)
    db.upsert_market_name("B", None)                        # no-op (empty name)
    db.commit()
    db.close()
    assert queries.market_names_map(path) == {"A": "Team A wins (updated)"}


def test_trades_grouped(tmp_path):
    path = str(tmp_path / "grp.db")
    db = Database(path)
    for o in [
        Order(instrument="C", instrument_type=InstrumentType.COMBO, side=Side.YES,
              action="buy", price=0.1, qty=10, signal_ref="t1", order_id="oc"),
        Order(instrument="A", instrument_type=InstrumentType.LEG, side=Side.NO,
              action="buy", price=0.5, qty=5, signal_ref="t1", order_id="oa"),
    ]:
        db.insert_order(o)
    db.insert_fill(Fill(order_id="oc", instrument="C", instrument_type=InstrumentType.COMBO,
                        side=Side.YES, action="buy", price=0.1, qty=10, fee=0.02))
    db.insert_fill(Fill(order_id="oa", instrument="A", side=Side.NO,
                        action="buy", price=0.5, qty=5, fee=0.01))
    db.insert_open_trade(signal_ref="t1", mve_collection_ticker="C", legs_json="[]",
                         opened_ts=100.0, expected_pnl=1.0)
    db.commit()
    db.close()

    rows = queries.trades_grouped(path, closed=False)
    assert len(rows) == 1
    tr = rows[0]
    assert tr["signal_ref"] == "t1" and tr["status"] == "open"
    assert tr["combo"]["instrument"] == "C" and tr["combo"]["qty"] == 10
    assert len(tr["legs"]) == 1 and tr["legs"][0]["instrument"] == "A"
    assert queries.trades_grouped(path, closed=True) == []   # nothing closed yet
    # fees roll up across combo (0.02) + leg (0.01), and mode is carried from the orders
    # (Order.mode defaults to "paper") so the UI can label estimated vs. actual fees.
    assert tr["total_fees"] == pytest.approx(0.03)
    assert tr["mode"] == "paper"


def test_trades_grouped_closed_outcomes(tmp_path):
    path = str(tmp_path / "grpc.db")
    db = Database(path)
    for o in [
        Order(instrument="C", instrument_type=InstrumentType.COMBO, side=Side.YES,
              action="buy", price=0.1, qty=3, signal_ref="t1", order_id="oc"),
        Order(instrument="A", instrument_type=InstrumentType.LEG, side=Side.NO,
              action="buy", price=0.5, qty=1, signal_ref="t1", order_id="oa"),
        Order(instrument="B", instrument_type=InstrumentType.LEG, side=Side.NO,
              action="buy", price=0.5, qty=2, signal_ref="t1", order_id="ob"),
    ]:
        db.insert_order(o)
    db.insert_fill(Fill(order_id="oc", instrument="C", instrument_type=InstrumentType.COMBO,
                        side=Side.YES, action="buy", price=0.1, qty=3, fee=0.0))
    db.insert_fill(Fill(order_id="oa", instrument="A", side=Side.NO, action="buy", price=0.5, qty=1, fee=0.0))
    db.insert_fill(Fill(order_id="ob", instrument="B", side=Side.NO, action="buy", price=0.5, qty=2, fee=0.0))
    db.insert_open_trade(signal_ref="t1", mve_collection_ticker="C",
                         legs_json=json.dumps([{"leg_ticker": "A", "side": "yes"},
                                               {"leg_ticker": "B", "side": "yes"}]),
                         opened_ts=1.0, expected_pnl=-0.02)
    db.settle_open_trade("t1", settled_ts=2.0, realized_pnl=0.5,
                         outcomes_json=json.dumps({"A": True, "B": False}))
    db.commit()
    db.close()

    tr = queries.trades_grouped(path, closed=True)[0]
    assert tr["realized_pnl"] == 0.5 and tr["expected_pnl"] == -0.02
    # combo bought YES pays iff all yes-side legs resolve YES; B resolved NO -> combo NO
    assert tr["combo_resolved_yes"] is False
    res = {l["instrument"]: l["resolved_yes"] for l in tr["legs"]}
    assert res == {"A": True, "B": False}
    # Both legs are held NO (hedges): "paid" (did the position win) is the OPPOSITE of
    # the raw market result -- this is the exact distinction that can't be read off
    # resolved_yes alone. A resolved YES (market fact) but the NO position LOSES;
    # B resolved NO but the NO position WINS.
    paid = {l["instrument"]: l["paid"] for l in tr["legs"]}
    assert paid == {"A": False, "B": True}


def test_missing_db_returns_error(tmp_path):
    missing = str(tmp_path / "nope.db")
    assert "error" in queries.db_status(missing)
    assert "error" in queries.pnl_summary(missing)


def test_connection_is_read_only(populated_db):
    conn = queries._connect_ro(populated_db)
    with pytest.raises(sqlite3.OperationalError):  # cannot write through a RO connection
        conn.execute("INSERT INTO pnl(ts, realized, unrealized, equity) VALUES (0,0,0,0)")
    conn.close()


def test_resolve_db_path_env(monkeypatch):
    monkeypatch.setenv("COMBO_ARB_DB", "/tmp/custom.db")
    assert queries.resolve_db_path() == "/tmp/custom.db"
    assert queries.resolve_db_path("/explicit.db") == "/explicit.db"  # arg wins
