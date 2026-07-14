"""Replay recorded market frames through the live strategy and report metrics.

Frame format (one JSON object per line, JSONL):
    {"ts": <float>,
     "legs": {"A": {"best_bid":.., "best_ask":.., "last_trade_price":..}, ...},
     "rfqs": [{"rfq_id":.., "mve_collection_ticker":.., "legs":[{"leg_ticker":.., "side":"yes","ratio":1}],
               "quote_yes":.., "quote_no":.., "size":..}]}

Each frame is fed to the same :class:`Controller` (cumulative risk + PnL) via an
in-memory mock client, so backtests exercise the exact production code path.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from combo_arb.config import AppConfig
from combo_arb.kalshi.mock_client import MockKalshiClient, synthetic_scenario
from combo_arb.models import ComboLeg, ComboRFQ, LegPrice, Side
from combo_arb.orchestration.controller import Controller
from combo_arb.persistence.db import Database


@dataclass
class ReplayReport:
    n_frames: int
    n_signals: int
    n_executed: int
    fill_rate: float
    total_expected_pnl: float
    total_immediate_cash: float
    mean_trade_pnl: float
    pnl_std: float
    sharpe: float                 # per-trade, unannualized
    avg_settlement_win_rate: float
    max_drawdown: float

    def render(self) -> str:
        lines = ["Backtest report", "-" * 32]
        for k, v in asdict(self).items():
            lines.append(f"{k:>26}: {v:.4f}" if isinstance(v, float) else f"{k:>26}: {v}")
        return "\n".join(lines)


def _frame_to_market(frame: dict) -> tuple[dict[str, LegPrice], list[ComboRFQ]]:
    legs = {
        t: LegPrice(leg_ticker=t, **{k: v for k, v in d.items() if k in
                                     ("best_bid", "best_ask", "last_trade_price")})
        for t, d in frame.get("legs", {}).items()
    }
    rfqs = []
    for r in frame.get("rfqs", []):
        rfqs.append(
            ComboRFQ(
                rfq_id=r["rfq_id"],
                mve_collection_ticker=r["mve_collection_ticker"],
                legs=[ComboLeg(leg_ticker=l["leg_ticker"],
                               side=Side(l.get("side", "yes")),
                               ratio=int(l.get("ratio", 1))) for l in r["legs"]],
                quote_yes=r["quote_yes"],
                quote_no=r.get("quote_no"),
                size=int(r.get("size", 1)),
            )
        )
    return legs, rfqs


def load_frames(path: str | Path) -> list[dict]:
    frames = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                frames.append(json.loads(line))
    return frames


def replay(path: str | Path, cfg: AppConfig, db: Optional[Database] = None) -> ReplayReport:
    frames = load_frames(path)
    if not frames:
        return ReplayReport(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    legs0, rfqs0 = _frame_to_market(frames[0])
    client = MockKalshiClient(leg_prices=legs0, rfqs=rfqs0)
    controller = Controller(cfg, client, db=db)

    n_signals = n_executed = 0
    trade_pnls: list[float] = []
    immediate_cash_total = 0.0
    win_rates: list[float] = []
    equity_curve: list[float] = []
    cum = 0.0

    for frame in frames:
        legs, rfqs = _frame_to_market(frame)
        client._legs, client._rfqs = legs, rfqs
        cycle = controller.run_once()
        n_signals += cycle.signals
        for oc in cycle.outcomes:
            if oc.executed:
                n_executed += 1
                trade_pnls.append(oc.expected_pnl)
                immediate_cash_total += oc.immediate_cash
                win_rates.append(oc.win_rate)
                cum += oc.expected_pnl
                equity_curve.append(cum)

    mean_pnl = sum(trade_pnls) / len(trade_pnls) if trade_pnls else 0.0
    if len(trade_pnls) > 1:
        var = sum((p - mean_pnl) ** 2 for p in trade_pnls) / len(trade_pnls)
        std = math.sqrt(var)
    else:
        std = 0.0
    sharpe = (mean_pnl / std) if std > 0 else 0.0

    # Max drawdown on the cumulative expected-PnL curve.
    peak = -math.inf
    max_dd = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)

    return ReplayReport(
        n_frames=len(frames),
        n_signals=n_signals,
        n_executed=n_executed,
        fill_rate=(n_executed / n_signals) if n_signals else 0.0,
        total_expected_pnl=sum(trade_pnls),
        total_immediate_cash=immediate_cash_total,
        mean_trade_pnl=mean_pnl,
        pnl_std=std,
        sharpe=sharpe,
        avg_settlement_win_rate=(sum(win_rates) / len(win_rates)) if win_rates else 0.0,
        max_drawdown=max_dd,
    )


def generate_sample_frames(path: str | Path, n: int = 200, seed: int = 7) -> None:
    """Write a synthetic JSONL sample: a random-walking market that intermittently
    misprices the AB combo, producing a realistic mix of arb / no-arb frames."""
    rng = random.Random(seed)
    base_legs, base_rfqs = synthetic_scenario()
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as fh:
        ts = 1_700_000_000.0
        mids = {"A": 0.50, "B": 0.40, "C": 0.30}
        for _ in range(n):
            ts += 1.0
            frame_legs = {}
            for t, m in mids.items():
                m = max(0.05, min(0.95, m + rng.uniform(-0.02, 0.02)))
                mids[t] = m
                frame_legs[t] = {
                    "best_bid": round(m - 0.01, 4),
                    "best_ask": round(m + 0.01, 4),
                    "last_trade_price": round(m, 4),
                }
            fair_ab = mids["A"] * mids["B"]
            # Quote the AB combo around fair, sometimes overpriced enough to arb.
            overpricing = rng.choice([0.0, 0.0, 0.04, 0.08, 0.12, -0.04])
            quote_ab = round(max(0.01, min(0.99, fair_ab + overpricing)), 3)
            rfqs = [{
                "rfq_id": f"rfq-ab-{int(ts)}",
                "mve_collection_ticker": "COMBO_AB",
                "legs": [{"leg_ticker": "A", "side": "yes", "ratio": 1},
                         {"leg_ticker": "B", "side": "yes", "ratio": 1}],
                "quote_yes": quote_ab,
                "quote_no": round(1 - quote_ab - 0.02, 3),
                "size": 20,
            }]
            fh.write(json.dumps({"ts": ts, "legs": frame_legs, "rfqs": rfqs}) + "\n")
