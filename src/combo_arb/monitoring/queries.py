"""Read-only queries over the engine's SQLite DB.

Every connection is opened in read-only mode (``file:...?mode=ro``) so this layer
can never write or trade. Functions take a DB path and return plain,
JSON-serializable dicts/lists suitable for returning straight from an MCP tool.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_DEFAULT_DB = "data/combo_arb.db"
_TABLES = (
    "market_snapshots", "combo_rfqs", "combo_evaluations", "arb_signals",
    "orders", "fills", "positions", "pnl", "latency", "open_trades", "market_names",
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
    """Most recent fills, with instrument_type (combo/leg) joined from orders so a
    combo fill can't be mistaken for a leg fill (or vice versa) in the UI."""
    conn = _connect_ro(path)
    if conn is None:
        return [_missing(path)]
    try:
        rows = _rows(conn,
            "SELECT f.ts AS ts, f.order_id AS order_id, f.instrument AS instrument, "
            "f.side AS side, f.action AS action, f.price AS price, f.qty AS qty, "
            "f.fee AS fee, o.instrument_type AS instrument_type "
            "FROM fills f LEFT JOIN orders o ON o.order_id = f.order_id "
            "ORDER BY f.ts DESC LIMIT ?", (limit,))
        for r in rows:
            r["ts_iso"] = _iso(r["ts"])
        return rows
    finally:
        conn.close()


def pnl_series(path: str, limit: int = 500) -> list[dict]:
    """Equity/PnL points over time (ascending) for the equity curve. Returns at most
    ``limit`` most-recent points, oldest first."""
    conn = _connect_ro(path)
    if conn is None:
        return [_missing(path)]
    try:
        rows = _rows(conn,
            "SELECT ts, realized, unrealized, equity FROM "
            "(SELECT ts, realized, unrealized, equity FROM pnl ORDER BY ts DESC LIMIT ?) "
            "ORDER BY ts ASC", (limit,))
        for r in rows:
            r["ts_iso"] = _iso(r["ts"])
        return rows
    finally:
        conn.close()


def open_trades_list(path: str, limit: int = 50) -> list[dict]:
    """Currently-open trades (oldest first) awaiting settlement."""
    conn = _connect_ro(path)
    if conn is None:
        return [_missing(path)]
    try:
        try:
            rows = _rows(conn,
                "SELECT signal_ref, mve_collection_ticker, opened_ts, expected_pnl "
                "FROM open_trades WHERE status='open' ORDER BY opened_ts ASC LIMIT ?", (limit,))
        except sqlite3.OperationalError:
            return []  # table predates settlement tracking
        for r in rows:
            r["opened_iso"] = _iso(r["opened_ts"])
        return rows
    finally:
        conn.close()


def recent_trades(path: str, limit: int = 50) -> list[dict]:
    """Trade history: settled + expired trades, most recently closed first, with the
    realized PnL and the trade-time Monte-Carlo estimate for comparison."""
    conn = _connect_ro(path)
    if conn is None:
        return [_missing(path)]
    try:
        try:
            rows = _rows(conn,
                "SELECT signal_ref, mve_collection_ticker, status, opened_ts, settled_ts, "
                "expected_pnl, realized_pnl FROM open_trades WHERE status IN ('settled','expired') "
                "ORDER BY settled_ts DESC LIMIT ?", (limit,))
        except sqlite3.OperationalError:
            return []
        for r in rows:
            r["opened_iso"] = _iso(r["opened_ts"])
            r["settled_iso"] = _iso(r["settled_ts"])
        return rows
    finally:
        conn.close()


def market_names_map(path: str) -> dict:
    """Ticker -> human-readable display name (captured by the engine at scan time).
    Empty if the table doesn't exist yet or nothing has been named."""
    conn = _connect_ro(path)
    if conn is None:
        return {}
    try:
        try:
            rows = conn.execute("SELECT ticker, display_name FROM market_names").fetchall()
        except sqlite3.OperationalError:
            return {}
        return {r["ticker"]: r["display_name"] for r in rows}
    finally:
        conn.close()


def _combo_resolved_yes(legs_json, outcomes: dict):
    """Did the (bought-YES) combo pay? Combo resolves YES iff every selected leg resolves
    in the combo's favour (YES-side leg -> underlying YES; NO-side leg -> underlying NO).
    Returns None if outcomes are missing/incomplete (e.g. expired trades)."""
    if not outcomes:
        return None
    try:
        legs = json.loads(legs_json or "[]")
    except (TypeError, ValueError):
        return None
    if not legs:
        return None
    for leg in legs:
        o = outcomes.get(leg.get("leg_ticker"))
        if o is None:
            return None
        favourable = o if leg.get("side", "yes") == "yes" else (not o)
        if not favourable:
            return False
    return True


def trades_grouped(path: str, closed: bool = False, limit: int = 50) -> list[dict]:
    """Trades grouped as combo + its hedge legs (one entry per trade / signal_ref).

    ``closed=False`` returns still-open trades (oldest first); ``closed=True`` returns
    settled + expired history (most recently closed first). Each fill is classified as
    combo vs leg by its order's ``instrument_type`` (robust to ticker naming), joined
    ``fills`` -> ``orders`` on ``order_id``."""
    conn = _connect_ro(path)
    if conn is None:
        return [_missing(path)]
    try:
        statuses = ("settled", "expired") if closed else ("open",)
        marks = ",".join("?" * len(statuses))
        order_by = "settled_ts DESC" if closed else "opened_ts ASC"
        try:
            trades = conn.execute(
                f"SELECT signal_ref, mve_collection_ticker, status, opened_ts, settled_ts, "
                f"expected_pnl, realized_pnl, legs_json, outcomes_json FROM open_trades "
                f"WHERE status IN ({marks}) ORDER BY {order_by} LIMIT ?",
                (*statuses, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        out: list[dict] = []
        for t in trades:
            try:
                outcomes = json.loads(t["outcomes_json"]) if t["outcomes_json"] else {}
            except (TypeError, ValueError):
                outcomes = {}
            fills = conn.execute(
                "SELECT o.instrument AS instrument, o.instrument_type AS itype, o.side AS side, "
                "o.action AS action, f.price AS price, f.qty AS qty, f.fee AS fee "
                "FROM fills f JOIN orders o ON o.order_id = f.order_id WHERE o.signal_ref = ?",
                (t["signal_ref"],)).fetchall()
            combo = None
            legs: list[dict] = []
            for r in fills:
                leg = {"instrument": r["instrument"], "side": r["side"], "action": r["action"],
                       "qty": r["qty"], "price": r["price"], "fee": r["fee"],
                       # how the underlying actually resolved (None if unknown / combo row)
                       "resolved_yes": outcomes.get(r["instrument"])}
                if r["itype"] == "combo" and combo is None:
                    combo = leg
                else:
                    legs.append(leg)
            out.append({
                "signal_ref": t["signal_ref"],
                "mve_collection_ticker": t["mve_collection_ticker"],
                "status": t["status"],
                "opened_iso": _iso(t["opened_ts"]),
                "settled_iso": _iso(t["settled_ts"]),
                "expected_pnl": t["expected_pnl"],
                "realized_pnl": t["realized_pnl"],
                "combo_resolved_yes": _combo_resolved_yes(t["legs_json"], outcomes),
                "combo": combo,
                "legs": legs,
            })
        return out
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


def open_trades_summary(path: str, limit: int = 15) -> dict:
    """Settlement state: how many trades are open vs settled/expired, realized PnL
    from settled ones, the oldest still-open trade, and recent settlements.

    The single best view of whether settlements are flowing: if ``open`` stays pinned
    at ``max_open_signals`` with nothing settling, the engine is wedged."""
    conn = _connect_ro(path)
    if conn is None:
        return _missing(path)
    try:
        try:
            by_status = {
                r["status"]: r["n"]
                for r in conn.execute(
                    "SELECT status, COUNT(*) n FROM open_trades GROUP BY status"
                ).fetchall()
            }
        except sqlite3.OperationalError:
            return {"error": "open_trades table not present yet (engine predates settlement tracking)"}
        realized = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl),0) r FROM open_trades WHERE status='settled'"
        ).fetchone()["r"]
        oldest = conn.execute(
            "SELECT signal_ref, opened_ts FROM open_trades WHERE status='open' "
            "ORDER BY opened_ts ASC LIMIT 1"
        ).fetchone()
        recent = _rows(conn,
            "SELECT signal_ref, mve_collection_ticker, status, settled_ts, realized_pnl "
            "FROM open_trades WHERE status IN ('settled','expired') "
            "ORDER BY settled_ts DESC LIMIT ?", (limit,))
        for r in recent:
            r["settled_iso"] = _iso(r["settled_ts"])
        return {
            "open": by_status.get("open", 0),
            "settled": by_status.get("settled", 0),
            "expired": by_status.get("expired", 0),
            "settled_realized_pnl": round(realized, 4),
            "oldest_open_signal_ref": oldest["signal_ref"] if oldest else None,
            "oldest_open_iso": _iso(oldest["opened_ts"]) if oldest else None,
            "recent_settlements": recent,
        }
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
