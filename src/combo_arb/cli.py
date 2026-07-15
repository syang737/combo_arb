"""Command-line interface.

    combo-arb scan       one-shot scan, print flagged signals
    combo-arb run        run the controller loop (paper by default)
    combo-arb replay     backtest a recorded JSONL frame file
    combo-arb gen-sample write a synthetic sample frame file
    combo-arb markets    read-only auth smoke test against the live API (no orders)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from combo_arb.config import AppConfig, Mode
from combo_arb.kalshi.base import MarketDataClient
from combo_arb.kalshi.mock_client import MockKalshiClient
from combo_arb.telemetry.metrics import Metrics, configure_logging

app = typer.Typer(add_completion=False, help="Kalshi combo arbitrage engine")

_DEFAULT_CONFIG = "config/config.example.yaml"


def _load_cfg(config: Optional[str]) -> AppConfig:
    path = config or (_DEFAULT_CONFIG if Path(_DEFAULT_CONFIG).exists() else None)
    return AppConfig.load(path)


def _require_live_credentials(cfg: AppConfig) -> None:
    """Fail early (and informatively) when a live data source has no usable creds.

    Without this, a missing key silently produces empty auth headers and an opaque
    401 from Kalshi. Here we say exactly what was and wasn't loaded.
    """
    s = cfg.secrets
    if not (s.kalshi_api_key_id and s.kalshi_private_key_path):
        typer.echo("Live source needs Kalshi credentials, but none were loaded:")
        typer.echo(f"  KALSHI_API_KEY_ID loaded:   {bool(s.kalshi_api_key_id)}")
        typer.echo(f"  KALSHI_PRIVATE_KEY_PATH:    {s.kalshi_private_key_path or '(unset)'}")
        typer.echo("Put them in a .env in the repo root (NOT .env.txt) or set the env vars,")
        typer.echo("then re-run. Auth is required for /communications/rfqs.")
        raise typer.Exit(code=1)
    if not Path(s.kalshi_private_key_path).exists():
        typer.echo(f"Private key file not found: {s.kalshi_private_key_path}")
        typer.echo("Check KALSHI_PRIVATE_KEY_PATH points to your PKCS#8 .pem file.")
        raise typer.Exit(code=1)


def _make_client(cfg: AppConfig, source: str) -> MarketDataClient:
    if source == "live":
        _require_live_credentials(cfg)
        from combo_arb.kalshi.client import KalshiClient
        return KalshiClient(cfg)
    return MockKalshiClient(random_walk=True)


@app.command()
def scan(
    config: Optional[str] = typer.Option(None, help="Path to config YAML"),
    source: str = typer.Option("mock", help="mock | live"),
    top: int = typer.Option(20, help="Max rows to show, ranked by edge"),
    flagged_only: bool = typer.Option(False, help="Show only flagged (tradeable) combos"),
    log_level: str = typer.Option("WARNING"),
) -> None:
    """Scan once and print every combo's edge + gap-to-flag (near-misses included)."""
    configure_logging(log_level)
    cfg = _load_cfg(config)
    client = _make_client(cfg, source)
    from combo_arb.scanner.scanner import Scanner

    scanner = Scanner(client, cfg)
    signals = scanner.scan()
    evals = sorted(scanner.last_evaluations, key=lambda e: e.arbitrage_margin, reverse=True)
    if flagged_only:
        evals = [e for e in evals if e.flagged]
    if not evals:
        typer.echo("No combos to show (no RFQs returned, or none matched the filter).")
        return

    band = cfg.thresholds.near_miss_band
    typer.echo(
        f"Direction: {cfg.strategy.direction}. Evaluated "
        f"{len(scanner.last_evaluations)} combo(s); {len(signals)} flagged.\n"
        f"  edge = fair-vs-quote net of fees;  gap = edge - buffer (>=0 flags).\n"
    )
    typer.echo(f"  {'combo':<30} {'quote':>6} {'fair':>6} {'edge':>7} {'gap':>7}  status")
    for e in evals[:top]:
        status = "FLAG" if e.flagged else ("near" if e.gap_to_flag >= -band else "")
        typer.echo(
            f"  {e.mve_collection_ticker[:30]:<30} {e.combo_quote_yes:>6.3f} "
            f"{e.fair_combo:>6.3f} {e.arbitrage_margin:>7.3f} {e.gap_to_flag:>7.3f}  {status}"
        )


@app.command()
def run(
    config: Optional[str] = typer.Option(None, help="Path to config YAML"),
    source: str = typer.Option("mock", help="mock | live (market data source)"),
    iterations: int = typer.Option(5, help="Number of scan cycles (0 = run forever)"),
    log_level: str = typer.Option("INFO"),
) -> None:
    """Run the controller loop; simulate (paper) or route (live, if armed) trades."""
    configure_logging(log_level)
    cfg = _load_cfg(config)
    from combo_arb.orchestration.controller import Controller
    from combo_arb.persistence.db import Database

    db = Database(cfg.persistence.db_path)
    metrics = Metrics(latency_sink=db.insert_latency)
    client = _make_client(cfg, source)
    controller = Controller(cfg, client, db=db, metrics=metrics)

    mode_note = "LIVE (armed)" if cfg.live_trading_armed() else f"{cfg.mode.value} (safe)"
    typer.echo(f"Running {iterations or 'unbounded'} cycle(s) in {mode_note} mode...")

    cycles = controller.run(max_iterations=iterations or None)
    executed = sum(c.executed for c in cycles)
    signals = sum(c.signals for c in cycles)
    typer.echo(f"cycles={len(cycles)} signals={signals} executed={executed}")
    typer.echo(f"db counts: {db.counts()}")
    typer.echo(f"cumulative pnl: {db.total_pnl()}")
    db.close()


@app.command()
def replay(
    file: str = typer.Argument(..., help="Path to JSONL frame file"),
    config: Optional[str] = typer.Option(None, help="Path to config YAML"),
    persist: bool = typer.Option(False, help="Write results to the SQLite DB"),
    log_level: str = typer.Option("WARNING"),
) -> None:
    """Backtest the strategy over recorded frames and print metrics."""
    configure_logging(log_level)
    cfg = _load_cfg(config)
    from combo_arb.backtest.replay import replay as run_replay
    from combo_arb.persistence.db import Database

    db = Database(cfg.persistence.db_path) if persist else None
    report = run_replay(file, cfg, db=db)
    typer.echo(report.render())
    if db is not None:
        db.close()


@app.command("gen-sample")
def gen_sample(
    out: str = typer.Option("data/sample/frames.jsonl", help="Output JSONL path"),
    n: int = typer.Option(200, help="Number of frames"),
    seed: int = typer.Option(7),
) -> None:
    """Generate a synthetic sample frame file for replay/testing."""
    from combo_arb.backtest.replay import generate_sample_frames

    generate_sample_frames(out, n=n, seed=seed)
    typer.echo(f"Wrote {n} frames to {out}")


@app.command()
def markets(
    config: Optional[str] = typer.Option(None, help="Path to config YAML"),
    limit: int = typer.Option(5),
) -> None:
    """Auth smoke test: validate credentials against an authenticated endpoint
    (/portfolio/balance) then list a few markets. Never places orders."""
    configure_logging("INFO")
    cfg = _load_cfg(config)
    _require_live_credentials(cfg)
    from combo_arb.kalshi.client import KalshiClient

    client = KalshiClient(cfg)
    try:
        # /markets is PUBLIC and cannot confirm auth; hit an authenticated endpoint.
        try:
            balance = client.get_balance()
        except RuntimeError as exc:
            typer.echo(f"Auth FAILED ({cfg.environment.value}): {exc}")
            raise typer.Exit(code=1)
        typer.echo(f"Auth OK ({cfg.environment.value}). Balance: {balance.get('balance')} cents.")
        rows = client.get_markets(limit=limit)
        typer.echo(f"{len(rows)} market(s):")
        for m in rows:
            typer.echo(f"  {m.get('ticker')}: yes_bid={m.get('yes_bid')} yes_ask={m.get('yes_ask')}")
    finally:
        client.close()


if __name__ == "__main__":  # pragma: no cover
    app()
