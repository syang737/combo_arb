"""SQLite persistence.

Append-only tables for market snapshots, combo RFQs, arb signals, orders, fills,
positions, PnL, and latency. Single file (config ``persistence.db_path``); good
for replay + telemetry and trivially migratable to a time-series DB later.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from combo_arb.models import ArbSignal, ComboRFQ, Fill, LegPrice, Order, PnL, Position, Side

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, leg_ticker TEXT, best_bid REAL, best_ask REAL,
    last_trade_price REAL, implied_prob REAL
);
CREATE INDEX IF NOT EXISTS ix_snap_ts ON market_snapshots(ts);

CREATE TABLE IF NOT EXISTS combo_rfqs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, rfq_id TEXT, mve_collection_ticker TEXT, legs_json TEXT,
    quote_yes REAL, quote_no REAL, size INTEGER
);
CREATE INDEX IF NOT EXISTS ix_rfq_ts ON combo_rfqs(ts);

CREATE TABLE IF NOT EXISTS arb_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, rfq_id TEXT, mve_collection_ticker TEXT,
    combo_quote_yes REAL, fair_combo REAL, fees_estimate REAL,
    margin_threshold REAL, arbitrage_margin REAL, size INTEGER, action TEXT
);
CREATE INDEX IF NOT EXISTS ix_sig_ts ON arb_signals(ts);

CREATE TABLE IF NOT EXISTS combo_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, rfq_id TEXT, mve_collection_ticker TEXT, direction TEXT,
    combo_quote_yes REAL, fair_combo REAL, fees_estimate REAL, buffer REAL,
    arbitrage_margin REAL, gap_to_flag REAL, flagged INTEGER
);
CREATE INDEX IF NOT EXISTS ix_eval_ts ON combo_evaluations(ts);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, order_id TEXT, kalshi_order_id TEXT, mode TEXT, signal_ref TEXT,
    instrument TEXT, instrument_type TEXT, side TEXT, action TEXT,
    price REAL, qty INTEGER, status TEXT
);
CREATE INDEX IF NOT EXISTS ix_ord_ts ON orders(ts);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, order_id TEXT, instrument TEXT, side TEXT, action TEXT,
    price REAL, qty INTEGER, fee REAL
);
CREATE INDEX IF NOT EXISTS ix_fill_ts ON fills(ts);

CREATE TABLE IF NOT EXISTS positions (
    instrument TEXT PRIMARY KEY,
    instrument_type TEXT, net_qty INTEGER, avg_price REAL, updated_ts REAL
);

CREATE TABLE IF NOT EXISTS pnl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, realized REAL, unrealized REAL, equity REAL
);
CREATE INDEX IF NOT EXISTS ix_pnl_ts ON pnl(ts);

CREATE TABLE IF NOT EXISTS latency (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, stage TEXT, ms REAL
);
CREATE INDEX IF NOT EXISTS ix_lat_ts ON latency(ts);

CREATE TABLE IF NOT EXISTS open_trades (
    signal_ref TEXT PRIMARY KEY,           -- rfq_id of the originating signal
    mve_collection_ticker TEXT,
    legs_json TEXT,                        -- [{"leg_ticker":.., "side":"yes"|"no"}, ...]
    opened_ts REAL,
    expected_pnl REAL,                     -- Monte-Carlo estimate recorded at trade time
    status TEXT DEFAULT 'open',            -- open | settled
    settled_ts REAL,
    realized_pnl REAL
);
CREATE INDEX IF NOT EXISTS ix_open_trades_status ON open_trades(status);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # -- writers -----------------------------------------------------------
    def insert_snapshot(self, lp: LegPrice, implied_prob: Optional[float] = None) -> None:
        self.conn.execute(
            "INSERT INTO market_snapshots(ts, leg_ticker, best_bid, best_ask, "
            "last_trade_price, implied_prob) VALUES (?,?,?,?,?,?)",
            (lp.timestamp, lp.leg_ticker, lp.best_bid, lp.best_ask,
             lp.last_trade_price, implied_prob),
        )

    def insert_rfq(self, rfq: ComboRFQ) -> None:
        self.conn.execute(
            "INSERT INTO combo_rfqs(ts, rfq_id, mve_collection_ticker, legs_json, "
            "quote_yes, quote_no, size) VALUES (?,?,?,?,?,?,?)",
            (rfq.quote_time, rfq.rfq_id, rfq.mve_collection_ticker,
             json.dumps([leg.model_dump() for leg in rfq.legs]),
             rfq.quote_yes, rfq.quote_no, rfq.size),
        )

    def insert_signal(self, sig: ArbSignal) -> None:
        self.conn.execute(
            "INSERT INTO arb_signals(ts, rfq_id, mve_collection_ticker, combo_quote_yes, "
            "fair_combo, fees_estimate, margin_threshold, arbitrage_margin, size, action) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sig.timestamp, sig.rfq_id, sig.mve_collection_ticker, sig.combo_quote_yes,
             sig.fair_combo, sig.fees_estimate, sig.margin_threshold, sig.arbitrage_margin,
             sig.size, sig.action.value),
        )

    def insert_evaluation(self, ev) -> None:
        self.conn.execute(
            "INSERT INTO combo_evaluations(ts, rfq_id, mve_collection_ticker, direction, "
            "combo_quote_yes, fair_combo, fees_estimate, buffer, arbitrage_margin, "
            "gap_to_flag, flagged) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ev.timestamp, ev.rfq_id, ev.mve_collection_ticker, ev.direction,
             ev.combo_quote_yes, ev.fair_combo, ev.fees_estimate, ev.buffer,
             ev.arbitrage_margin, ev.gap_to_flag, int(ev.flagged)),
        )

    def insert_order(self, order: Order) -> None:
        self.conn.execute(
            "INSERT INTO orders(ts, order_id, kalshi_order_id, mode, signal_ref, instrument, "
            "instrument_type, side, action, price, qty, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (order.timestamp, order.order_id, order.kalshi_order_id, order.mode,
             order.signal_ref, order.instrument, order.instrument_type.value,
             order.side.value, order.action, order.price, order.qty, order.status.value),
        )

    def insert_fill(self, fill: Fill) -> None:
        self.conn.execute(
            "INSERT INTO fills(ts, order_id, instrument, side, action, price, qty, fee) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (fill.timestamp, fill.order_id, fill.instrument, fill.side.value,
             fill.action, fill.price, fill.qty, fill.fee),
        )

    def upsert_position(self, pos: Position) -> None:
        self.conn.execute(
            "INSERT INTO positions(instrument, instrument_type, net_qty, avg_price, updated_ts) "
            "VALUES (?,?,?,?,?) ON CONFLICT(instrument) DO UPDATE SET "
            "instrument_type=excluded.instrument_type, net_qty=excluded.net_qty, "
            "avg_price=excluded.avg_price, updated_ts=excluded.updated_ts",
            (pos.instrument, pos.instrument_type.value, pos.net_qty, pos.avg_price, pos.updated_ts),
        )

    def insert_pnl(self, pnl: PnL) -> None:
        self.conn.execute(
            "INSERT INTO pnl(ts, realized, unrealized, equity) VALUES (?,?,?,?)",
            (pnl.timestamp, pnl.realized, pnl.unrealized, pnl.equity),
        )

    def insert_open_trade(
        self,
        signal_ref: str,
        mve_collection_ticker: str,
        legs_json: str,
        opened_ts: float,
        expected_pnl: float,
    ) -> None:
        # signal_ref (an RFQ id) is expected to be unique per trade; OR IGNORE is a
        # defensive fallback (not a real-world path -- rfq_ids don't recur) so a
        # stray duplicate can't crash the whole run.
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO open_trades(signal_ref, mve_collection_ticker, legs_json, "
            "opened_ts, expected_pnl, status) VALUES (?,?,?,?,?,'open')",
            (signal_ref, mve_collection_ticker, legs_json, opened_ts, expected_pnl),
        )
        if cur.rowcount == 0:
            log.warning(
                "open_trades already has a row for signal_ref=%s; not overwriting "
                "(this trade's settlement won't be tracked)", signal_ref,
            )

    def is_trade_open(self, signal_ref: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM open_trades WHERE signal_ref=? AND status='open'", (signal_ref,)
        ).fetchone()
        return row is not None

    def get_open_trades(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM open_trades WHERE status='open'"
        ).fetchall()

    def count_open_trades(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM open_trades WHERE status='open'"
        ).fetchone()
        return row["n"]

    def get_trade_fills(
        self, signal_ref: str, combo_ticker: str
    ) -> tuple[Optional[Fill], list[Fill]]:
        """Reconstruct a trade's combo + hedge fills from the append-only orders/fills
        log (joined on order_id -> signal_ref, since fills don't carry it directly)."""
        rows = self.conn.execute(
            "SELECT f.* FROM fills f JOIN orders o ON o.order_id = f.order_id "
            "WHERE o.signal_ref = ?",
            (signal_ref,),
        ).fetchall()
        fills = [
            Fill(
                order_id=r["order_id"], instrument=r["instrument"], side=Side(r["side"]),
                action=r["action"], price=r["price"], qty=r["qty"], fee=r["fee"],
                timestamp=r["ts"],
            )
            for r in rows
        ]
        combo_fill = next((f for f in fills if f.instrument == combo_ticker), None)
        hedge_fills = [f for f in fills if f.instrument != combo_ticker]
        return combo_fill, hedge_fills

    def settle_open_trade(self, signal_ref: str, settled_ts: float, realized_pnl: float) -> None:
        self.conn.execute(
            "UPDATE open_trades SET status='settled', settled_ts=?, realized_pnl=? "
            "WHERE signal_ref=?",
            (settled_ts, realized_pnl, signal_ref),
        )

    def get_positions(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM positions").fetchall()

    def insert_latency(self, stage: str, ms: float, ts: Optional[float] = None) -> None:
        import time
        self.conn.execute(
            "INSERT INTO latency(ts, stage, ms) VALUES (?,?,?)",
            (ts if ts is not None else time.time(), stage, ms),
        )

    def commit(self) -> None:
        self.conn.commit()

    # -- reads (summary) ---------------------------------------------------
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for tbl in ("market_snapshots", "combo_rfqs", "combo_evaluations",
                    "arb_signals", "orders", "fills", "pnl"):
            out[tbl] = self.conn.execute(f"SELECT COUNT(*) AS n FROM {tbl}").fetchone()["n"]
        return out

    def total_pnl(self) -> dict[str, float]:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(realized),0) AS realized, "
            "COALESCE(SUM(unrealized),0) AS unrealized FROM pnl"
        ).fetchone()
        return {"realized": row["realized"], "unrealized": row["unrealized"]}

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
