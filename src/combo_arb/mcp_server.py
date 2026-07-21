"""Read-only MCP server exposing combo-arb monitoring tools over stdio.

Run via the ``combo-arb-mcp`` console script or ``python -m combo_arb.mcp_server``.
The DB path resolves from ``--db`` -> ``$COMBO_ARB_DB`` -> config
``persistence.db_path`` -> the default. Every tool is read-only.
"""

from __future__ import annotations

from combo_arb.monitoring import queries

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - only hit when the extra isn't installed
    raise SystemExit(
        "The MCP SDK is not installed. Install it with:  pip install -e '.[mcp]'"
    ) from exc

mcp = FastMCP("combo-arb")


def _db() -> str:
    return queries.resolve_db_path()


@mcp.tool()
def db_status() -> dict:
    """Overview of the engine's database: path, size, last update time, and row
    counts for every table. Use this first to confirm the engine is writing data."""
    return queries.db_status(_db())


@mcp.tool()
def pnl_summary() -> dict:
    """Cumulative paper-trading PnL: realized, unrealized, latest equity, and the
    number of trades recorded."""
    return queries.pnl_summary(_db())


@mcp.tool()
def recent_signals(limit: int = 20) -> list:
    """The most recent flagged (tradeable) combo arbitrage signals."""
    return queries.recent_signals(_db(), limit)


@mcp.tool()
def top_near_misses(limit: int = 20) -> list:
    """Combos closest to becoming tradeable — largest gap_to_flag while still below
    the threshold. The best view of how close the market is getting to an edge."""
    return queries.top_near_misses(_db(), limit)


@mcp.tool()
def recent_fills(limit: int = 20) -> list:
    """The most recent simulated (paper) fills."""
    return queries.recent_fills(_db(), limit)


@mcp.tool()
def open_positions() -> list:
    """Current open positions (non-zero net quantity)."""
    return queries.open_positions(_db())


@mcp.tool()
def evaluation_history(collection_ticker: str, limit: int = 50) -> list:
    """Quote / fair / gap history over time for a single combo collection ticker."""
    return queries.evaluation_history(_db(), collection_ticker, limit)


def main() -> None:
    import argparse
    import os

    ap = argparse.ArgumentParser(description="combo-arb read-only MCP server (stdio)")
    ap.add_argument("--db", help="Path to combo_arb.db (overrides $COMBO_ARB_DB)")
    args, _ = ap.parse_known_args()
    if args.db:
        os.environ["COMBO_ARB_DB"] = args.db
    mcp.run()


if __name__ == "__main__":
    main()
