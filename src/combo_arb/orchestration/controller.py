"""Controller: routes signals from the scanner through risk to execution.

One ``run_once`` cycle:
  scan -> for each flagged signal: persist signal + snapshots -> risk sizing/hedge
  -> execute (paper or guarded-live) -> record orders, fills, positions, PnL.

PnL bookkeeping (paper): ``realized`` = net cash moved at trade time
(premium - hedge outlay - fees); ``unrealized`` = expected remaining settlement
value (Monte-Carlo mean minus realized); ``equity`` = running expected PnL.
Rate limits, the kill switch, and signal-only downgrades are enforced here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from combo_arb.config import AppConfig, Mode
from combo_arb.execution.base import ExecutionEngine
from combo_arb.execution.paper import PaperExecutionEngine
from combo_arb.execution.settlement import HedgedTrade, immediate_cash, simulate_pnl
from combo_arb.kalshi.base import MarketDataClient
from combo_arb.models import ArbSignal, PnL, SignalAction
from combo_arb.persistence.db import Database
from combo_arb.pricing.model import implied_prob
from combo_arb.risk.risk import RiskManager
from combo_arb.scanner.scanner import Scanner
from combo_arb.telemetry.metrics import Metrics

log = logging.getLogger(__name__)


@dataclass
class TradeOutcome:
    signal: ArbSignal
    executed: bool
    reason: str
    qty: int = 0
    expected_pnl: float = 0.0
    immediate_cash: float = 0.0
    win_rate: float = 0.0


@dataclass
class CycleResult:
    signals: int = 0
    executed: int = 0
    outcomes: list[TradeOutcome] = field(default_factory=list)


class Controller:
    def __init__(
        self,
        cfg: AppConfig,
        client: MarketDataClient,
        db: Optional[Database] = None,
        metrics: Optional[Metrics] = None,
        engine: Optional[ExecutionEngine] = None,
    ):
        self.cfg = cfg
        self.client = client
        self.db = db
        self.metrics = metrics or Metrics()
        self.scanner = Scanner(client, cfg)
        self.risk = RiskManager(cfg)
        self.engine = engine or self._default_engine()
        self._cum_equity = 0.0

    def _default_engine(self) -> ExecutionEngine:
        if self.cfg.mode == Mode.LIVE:
            # Imported lazily so paper runs never require the live client/creds.
            from combo_arb.execution.live import LiveExecutionEngine
            from combo_arb.kalshi.client import KalshiClient

            client = self.client if isinstance(self.client, KalshiClient) else KalshiClient(self.cfg)
            return LiveExecutionEngine(self.cfg, client)
        return PaperExecutionEngine(self.cfg)

    def run_once(self) -> CycleResult:
        result = CycleResult()
        with self.metrics.timer("scan"):
            signals = self.scanner.scan()
        result.signals = len(signals)
        self.metrics.incr("signals", len(signals))

        if self.db is not None:
            for rfq in self.scanner.last_rfqs:
                self.db.insert_rfq(rfq)

        for sig in signals:
            outcome = self._handle_signal(sig)
            result.outcomes.append(outcome)
            if outcome.executed:
                result.executed += 1

        if self.db is not None:
            self.db.commit()
        return result

    def _handle_signal(self, sig: ArbSignal) -> TradeOutcome:
        leg_probs = {
            leg.leg_ticker: implied_prob(sig.leg_prices[leg.leg_ticker], self.cfg.pricing)
            for leg in sig.legs
            if leg.leg_ticker in sig.leg_prices
        }

        decision = self.risk.evaluate(sig, sig.leg_prices)
        if not decision.approved:
            sig.action = SignalAction.SIGNAL_ONLY
            self._persist_signal(sig, leg_probs)
            self.metrics.incr("signal_only")
            log.info("signal-only %s: %s", sig.rfq_id, decision.reason)
            return TradeOutcome(sig, False, decision.reason)

        sig.action = SignalAction.HEDGE_VIA_LEGS
        self._persist_signal(sig, leg_probs)

        with self.metrics.timer("execute"):
            fills = self.engine.execute(decision.all_orders, sig.leg_prices)

        combo_fills = [f for f in fills if f.instrument == sig.mve_collection_ticker]
        hedge_fills = [f for f in fills if f.instrument != sig.mve_collection_ticker]
        if not combo_fills:
            log.warning("no combo fill for %s; skipping PnL", sig.rfq_id)
            return TradeOutcome(sig, False, "combo not filled")
        trade = HedgedTrade(
            signal=sig, combo_fill=combo_fills[0], hedge_fills=hedge_fills, leg_probs=leg_probs,
        )

        for f in fills:
            self.risk.register_fill(f)
        self.risk.mark_signal_opened()
        self.metrics.incr("executed")

        pnl_stats = simulate_pnl(
            trade,
            n_scenarios=self.cfg.settlement_sim.n_scenarios,
            seed=self.cfg.settlement_sim.seed,
        )
        self._persist_execution(sig, decision, fills, trade, pnl_stats)

        return TradeOutcome(
            signal=sig,
            executed=True,
            reason="executed",
            qty=decision.qty,
            expected_pnl=pnl_stats["expected_pnl"],
            immediate_cash=pnl_stats["immediate_cash"],
            win_rate=pnl_stats["win_rate"],
        )

    # -- persistence helpers ----------------------------------------------
    def _persist_signal(self, sig: ArbSignal, leg_probs: dict) -> None:
        if self.db is None:
            return
        self.db.insert_signal(sig)
        for ticker, lp in sig.leg_prices.items():
            self.db.insert_snapshot(lp, leg_probs.get(ticker))

    def _persist_execution(self, sig, decision, fills, trade, pnl_stats) -> None:
        if self.db is None:
            return
        for order in decision.all_orders:
            self.db.insert_order(order)
        for f in fills:
            self.db.insert_fill(f)
        for pos in self.risk.positions.values():
            self.db.upsert_position(pos)

        realized = immediate_cash(trade)
        unrealized = pnl_stats["expected_pnl"] - realized
        self._cum_equity += pnl_stats["expected_pnl"]
        self.db.insert_pnl(PnL(realized=realized, unrealized=unrealized, equity=self._cum_equity))

    # -- loop --------------------------------------------------------------
    def run(self, max_iterations: Optional[int] = None) -> list[CycleResult]:
        results: list[CycleResult] = []
        interval = self.cfg.polling.interval_ms / 1000.0
        i = 0
        try:
            while max_iterations is None or i < max_iterations:
                if self.cfg.risk.kill_switch:
                    log.warning("kill switch engaged; stopping loop")
                    break
                results.append(self.run_once())
                i += 1
                if max_iterations is None or i < max_iterations:
                    time.sleep(interval)
        except KeyboardInterrupt:  # pragma: no cover - interactive
            log.info("interrupted; shutting down")
        return results
