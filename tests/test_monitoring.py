import sqlite3

import pytest

from combo_arb.models import (
    ArbSignal,
    ComboEvaluation,
    ComboLeg,
    Fill,
    InstrumentType,
    LegPrice,
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
