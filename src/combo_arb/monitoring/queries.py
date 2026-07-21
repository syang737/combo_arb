"""Read-only queries over the engine's SQLite DB.

Every connection is opened in read-only mode (``file:...?mode=ro``) so this layer
can never write or trade. Functions take a DB path and return plain,
JSON-serializable dicts/lists suitable for returning straight from an MCP tool.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_DEFAULT_DB = "data/combo_arb.db"
_TABLES = (
    "market_snapshots", "combo_rfqs", "combo_evaluations", "arb_signals",
    "orders", "fills", "positions", "pnl", "latency",
)


def resolve_db_path(explicit: Optional[str] = None) -> str:
    """--db/arg -> $COMBO_ARB_DB -> config persistence.db_path -> default."""
    if explicit:
        return explicit
    if os.environ.get("COMBO_ARB_DB"):
        return os.environ["COMBO_ARB_DB"]
    try:
        from combo_arb.config import AppConfig
        return AppConfig.load().persistence.db_path
    except Exception:  # noqa: BLE001 - fall back to the default path
        return _DEFAULT_DB


def _iso(ts: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _connect_ro(path: str) -> Optional[sqlite3.Connection]:
    if not Path(path).exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _missing(path: str) -> dict:
    return {"error": f"database not found at {path} (has the engine run and written yet?)"}


# -- tools ----------------------------------------------------------------
def db_status(path: str) -> dict:
    """Overview: path, size, last update, and row counts per table."""
    if not Path(path).exists():
        return {"db_path": path, "exists": False, **_missing(path)}
    conn = _connect_ro(path)
    try:
        counts = {}
        for tbl in _TABLES:
            try:
                counts[tbl] = conn.execute(f"SELECT COUNT(*) n FROM {tbl}").fetchone()["n"]
            except sqlite3.OperationalError:
                counts[tbl] = None  # table not present yet
        last = conn.execute(
            "SELECT MAX(ts) t FROM combo_evaluations"
        ).fetchone()["t"] if counts.get("combo_evaluations") else None
        return {
            "db_path": path,
            "exists": True,
            "size_bytes": Path(path).stat().st_size,
            "last_update_ts": last,
            "last_update_iso": _iso(last),
            "row_counts": counts,
        }
    finally:
        conn.close()


def pnl_summary(path: str) -> dict:
    """Cumulative paper PnL: realized, unrealized, latest equity, trade count."""
    conn = _connect_ro(path)
    if conn is None:
        return _missing(path)
    try:
        agg = conn.execute(
            "SELECT COALESCE(SUM(realized),0) realized, COALESCE(SUM(unrealized),0) unrealized, "
            "COUNT(*) n FROM pnl"
        ).fetchone()
        latest = conn.execute("SELECT equity, ts FROM pnl ORDER BY ts DESC LIMIT 1").fetchone()
        return {
            "realized": round(agg["realized"], 4),
            "unrealized": round(agg["unrealized"], 4),
            "equity": round(latest["equity"], 4) if latest else 0.0,
            "trades": agg["n"],
            "as_of_iso": _iso(latest["ts"]) if latest else None,
        }
    finally:
        conn.close()


def recent_signals(path: str, limit: int = 20) -> list[dict]:
    """Most recent flagged (tradeable) combos from arb_signals."""
    conn = _connect_ro(path)
    if conn is None:
        return [_missing(path)]
    try:
        rows = _rows(conn,
            "SELECT ts, rfq_id, mve_collection_ticker, combo_quote_yes, fair_combo, "
            "fees_estimate, arbitrage_margin, size, action FROM arb_signals "
            "ORDER BY ts DESC LIMIT ?", (limit,))
        for r in rows:
            r["ts_iso"] = _iso(r["ts"])
        return rows
    finally:
        conn.close()


def top_near_misses(path: str, limit: int = 20) -> list[dict]:
    """Combos closest to flagging (highest gap_to_flag, still below the line)."""
    conn = _connect_ro(path)
    if conn is None:
        return [_missing(path)]
    try:
        rows = _rows(conn,
            "SELECT ts, mve_collection_ticker, direction, combo_quote_yes, fair_combo, "
            "fees_estimate, buffer, arbitrage_margin, gap_to_flag FROM combo_evaluations "
            "WHERE flagged=0 ORDER BY gap_to_flag DESC, ts DESC LIMIT ?", (limit,))
        for r in rows:
            r["ts_iso"] = _iso(r["ts"])
        return rows
    finally:
        conn.close()


def recent_fills(path: str, limit: int = 20) -> list[dict]:
    """Most recent (paper) fills."""
    conn = _connect_ro(path)
    if conn is None:
        return [_missing(path)]
    try:
        rows = _rows(conn,
            "SELECT ts, order_id, instrument, side, action, price, qty, fee FROM fills "
            "ORDER BY ts DESC LIMIT ?", (limit,))
        for r in rows:
            r["ts_iso"] = _iso(r["ts"])
        return rows
    finally:
        conn.close()


def open_positions(path: str) -> list[dict]:
    """Positions with non-zero net quantity."""
    conn = _connect_ro(path)
    if conn is None:
        return [_missing(path)]
    try:
        rows = _rows(conn,
            "SELECT instrument, instrument_type, net_qty, avg_price, updated_ts FROM positions "
            "WHERE net_qty != 0 ORDER BY ABS(net_qty) DESC")
        for r in rows:
            r["updated_iso"] = _iso(r["updated_ts"])
        return rows
    finally:
        conn.close()


def evaluation_history(path: str, collection_ticker: str, limit: int = 50) -> list[dict]:
    """Quote/fair/gap history for one combo collection over time."""
    conn = _connect_ro(path)
    if conn is None:
        return [_missing(path)]
    try:
        rows = _rows(conn,
            "SELECT ts, combo_quote_yes, fair_combo, fees_estimate, arbitrage_margin, "
            "gap_to_flag, flagged FROM combo_evaluations WHERE mve_collection_ticker = ? "
            "ORDER BY ts DESC LIMIT ?", (collection_ticker, limit))
        for r in rows:
            r["ts_iso"] = _iso(r["ts"])
        return rows
    finally:
        conn.close()
