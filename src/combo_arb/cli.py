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


def _make_client(cfg: AppConfig, source: str) -> MarketDataClient:
    if source == "live":
        from combo_arb.kalshi.client import KalshiClient
        return KalshiClient(cfg)
    return MockKalshiClient(random_walk=True)


@app.command()
def scan(
    config: Optional[str] = typer.Option(None, help="Path to config YAML"),
    source: str = typer.Option("mock", help="mock | live"),
    log_level: str = typer.Option("INFO"),
) -> None:
    """Scan once and print flagged arbitrage signals."""
    configure_logging(log_level)
    cfg = _load_cfg(config)
    client = _make_client(cfg, source)
    from combo_arb.scanner.scanner import Scanner

    signals = Scanner(client, cfg).scan()
    if not signals:
        typer.echo("No arbitrage signals above threshold.")
        return
    typer.echo(f"{len(signals)} signal(s):")
    for s in signals:
        typer.echo(
            f"  {s.rfq_id} [{s.mve_collection_ticker}] quote_yes={s.combo_quote_yes:.3f} "
            f"fair={s.fair_combo:.3f} fees={s.fees_estimate:.3f} "
            f"margin={s.arbitrage_margin:.3f} size={s.size}"
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
    """Read-only auth smoke test: list a few live markets (never places orders)."""
    configure_logging("INFO")
    cfg = _load_cfg(config)
    if not (cfg.secrets.kalshi_api_key_id and cfg.secrets.kalshi_private_key_path):
        typer.echo("Missing KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH in env/.env.")
        raise typer.Exit(code=1)
    from combo_arb.kalshi.client import KalshiClient

    client = KalshiClient(cfg)
    try:
        rows = client.get_markets(limit=limit)
        typer.echo(f"Auth OK ({cfg.environment.value}). {len(rows)} market(s):")
        for m in rows:
            typer.echo(f"  {m.get('ticker')}: yes_bid={m.get('yes_bid')} yes_ask={m.get('yes_ask')}")
    finally:
        client.close()


if __name__ == "__main__":  # pragma: no cover
    app()
