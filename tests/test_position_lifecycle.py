"""Positions are closed when a trade settles/expires, and the one-time backfill
collapses the historical blotter to only live exposure."""

from __future__ import annotations

from combo_arb.kalshi.mock_client import MockKalshiClient
from combo_arb.models import Fill, InstrumentType, Order, Position, Side
from combo_arb.orchestration.controller import Controller
from combo_arb.persistence.db import Database
from combo_arb.risk.risk import RiskManager


def test_close_fill_reverses_position(cfg):
    rm = RiskManager(cfg)
    rm.register_fill(Fill(order_id="o", instrument="A", side=Side.NO, action="buy",
                          price=0.5, qty=4, fee=0.0))
    assert rm.positions["A"].net_qty == 4
    rm.close_fill(Fill(order_id="o", instrument="A", side=Side.NO, action="buy",
                       price=0.5, qty=4, fee=0.0))
    assert "A" not in rm.positions          # zeroed -> removed


def _seed_trade(db, signal_ref, combo, legs, status="open", opened_ts=100.0):
    """One trade: combo buy + leg buys, with matching positions, at the given status."""
    db.insert_order(Order(instrument=combo, instrument_type=InstrumentType.COMBO, side=Side.YES,
                          action="buy", price=0.1, qty=10, signal_ref=signal_ref, order_id=signal_ref + "-c"))
    db.insert_fill(Fill(order_id=signal_ref + "-c", instrument=combo, instrument_type=InstrumentType.COMBO,
                        side=Side.YES, action="buy", price=0.1, qty=10, fee=0.0))
    for i, (leg, qty) in enumerate(legs):
        db.insert_order(Order(instrument=leg, instrument_type=InstrumentType.LEG, side=Side.NO,
                              action="buy", price=0.5, qty=qty, signal_ref=signal_ref,
                              order_id=f"{signal_ref}-l{i}"))
        db.insert_fill(Fill(order_id=f"{signal_ref}-l{i}", instrument=leg, side=Side.NO,
                            action="buy", price=0.5, qty=qty, fee=0.0))
    db.insert_open_trade(signal_ref=signal_ref, mve_collection_ticker=combo, legs_json="[]",
                         opened_ts=opened_ts, expected_pnl=0.0)
    if status != "open":
        db.settle_open_trade(signal_ref, settled_ts=opened_ts + 1, realized_pnl=0.0)


def test_startup_rebuild_drops_settled_positions(tmp_path, cfg):
    path = str(tmp_path / "pl.db")
    db = Database(path)
    _seed_trade(db, "t1", "C", [("A", 4)], status="settled")
    db.upsert_position(Position(instrument="C", instrument_type=InstrumentType.COMBO, net_qty=10, avg_price=0.1))
    db.upsert_position(Position(instrument="A", instrument_type=InstrumentType.LEG, net_qty=4, avg_price=0.5))
    db.commit()

    # Constructing the controller rebuilds positions from open trades only.
    Controller(cfg, MockKalshiClient(), db=db)
    assert db.get_positions() == []          # settled trade contributes nothing

    # Idempotent: a second startup leaves it empty.
    Controller(cfg, MockKalshiClient(), db=db)
    assert db.get_positions() == []


def test_close_only_removes_that_trades_contribution(tmp_path, cfg):
    path = str(tmp_path / "pl2.db")
    db = Database(path)
    # Leg A is held by a settled trade (qty 4) AND a still-open trade (qty 3).
    _seed_trade(db, "t1", "C1", [("A", 4)], status="settled")
    _seed_trade(db, "t2", "C2", [("A", 3)], status="open")
    db.upsert_position(Position(instrument="C1", instrument_type=InstrumentType.COMBO, net_qty=10, avg_price=0.1))
    db.upsert_position(Position(instrument="C2", instrument_type=InstrumentType.COMBO, net_qty=10, avg_price=0.1))
    db.upsert_position(Position(instrument="A", instrument_type=InstrumentType.LEG, net_qty=7, avg_price=0.5))
    db.commit()

    Controller(cfg, MockKalshiClient(), db=db)

    pos = {r["instrument"]: r["net_qty"] for r in db.get_positions()}
    assert "C1" not in pos          # settled combo closed
    assert pos.get("C2") == 10      # open trade untouched
    assert pos.get("A") == 3        # only t1's 4 removed; t2's 3 remains
