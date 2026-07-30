"""Configuration and secrets.

Non-secret runtime config comes from a YAML file (see config/config.example.yaml).
Secrets (API key id, private key path, live-trading confirmation) come only from
the environment / .env and are never written to the YAML.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Kalshi REST base URLs.
API_BASE_URLS = {
    # Production host per the current Kalshi REST docs.
    "prod": "https://external-api.kalshi.com/trade-api/v2",
    # Demo/sandbox host — confirm against your account before relying on it.
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}


class Mode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class Environment(str, Enum):
    PROD = "prod"
    DEMO = "demo"


class StrategyConfig(BaseModel):
    # buy_underpriced: flag combos quoted BELOW fair, buy the combo YES, hedge by
    #   shorting the legs. This is the buy-only trader's direction (default).
    # sell_overpriced: flag combos quoted ABOVE fair, sell the combo YES (RFQ maker),
    #   hedge by buying the legs. Requires the ability to sell/quote the combo.
    direction: str = "buy_underpriced"  # buy_underpriced | sell_overpriced


class PricingConfig(BaseModel):
    prob_source: str = "mid"            # mid | last
    thin_book_spread: float = 0.10
    correlation_factor: float = 1.0
    settlement_model: str = "binary"    # binary | fractional


class FeesConfig(BaseModel):
    taker_rate: float = 0.07
    maker_ratio: float = 0.25
    min_fee_per_contract: float = 0.01


class ThresholdsConfig(BaseModel):
    buffer_abs: float = 0.01
    buffer_pct: float = 0.005
    min_margin: float = 0.0
    # Apply the safety buffer on top of fees. Auto-forced on when live is armed
    # regardless of this setting. On by default so paper mode's fill/reject behaviour
    # matches live -- measured: without a buffer, marginal signals (edge clearing fees
    # by a fraction of a cent) execute and lose more often than not once real fill
    # prices, whole-contract hedge rounding, and the AND-rule convexity are accounted
    # for (a "near miss" -- most-but-not-all legs hitting -- is the single worst
    # outcome and often the second most likely one).
    apply_buffer: bool = True
    # How far BELOW the flag threshold an edge can be and still be persisted as a
    # "near miss" (for buffer calibration). Larger = more rows logged.
    near_miss_band: float = 0.05
    # Reject a trade (signal-only) if the pre-trade modelled full-package expected PnL
    # (combo + every hedge leg, real fill prices/fees, Monte-Carlo settlement) isn't
    # above this. The scanner's per-contract combo edge (arbitrage_margin) is flagged
    # BEFORE the hedge is priced/rounded to whole contracts, so a signal can clear that
    # threshold while the honest full-package number is negative -- this catches that
    # gap instead of finding out only after the trade is already placed.
    min_expected_pnl: float = 0.0


class RiskConfig(BaseModel):
    capital_per_trade: float = 100.0
    max_contracts_per_trade: int = 100
    max_position_per_market: int = 500
    max_total_exposure: float = 5000.0
    max_open_signals: int = 25
    kill_switch: bool = False
    # A trade is sized so every leg whose |delta| exceeds this gets >=1 whole hedge
    # contract; if that fully-hedged size doesn't fit capital/limits the trade is
    # emitted signal-only rather than executed partially hedged. Legs below this
    # delta are treated as immaterial (a negligible sliver of residual risk).
    min_hedge_delta: float = 0.01
    # Reject a trade (signal-only) if any hedge leg's entry price would exceed this.
    # A leg priced above ~0.95 is nearly certain to resolve as the market already
    # expects: hedging it buys very little real protection (there's almost no room
    # left for its probability to move against the combo) while still tying up
    # capital and paying a fee on it.
    max_leg_price: float = 0.95
    # Max legs in a combo we'll trade. A combo pays only if EVERY leg hits, so each
    # leg's hedge ratio is the product of the *other* legs' probabilities -- which
    # shrinks fast as legs are added. Past ~3 legs the fees on N hedge legs exceed
    # the protection they buy (measured: hedging is +EV at 2 legs, ~breakeven at 3,
    # negative at 5+), and the worst case is "all but one leg hit" -- the second most
    # likely outcome. Combos with more legs are emitted signal-only.
    max_legs: int = 3


class ExecutionConfig(BaseModel):
    live_enabled: bool = False
    fill_model: str = "taker_cross"     # taker_cross | mid | depth_prob (paper)
    combo_fill_price: str = "yes_bid"   # yes_bid | quote_yes (paper)
    # -- live-only knobs --
    time_in_force: str = "ioc"          # ioc = fill-now-or-cancel (limits legging exposure)
    fill_poll_timeout_s: float = 5.0    # how long to poll /portfolio/fills for reconciliation
    unwind_on_partial: bool = True      # flatten a partially-filled hedged set back to zero
    require_balance_check: bool = True  # verify account balance before placing live orders
    max_trades_per_run: int = 0         # 0 = unlimited; >0 caps trades per process (safety)


class DiscoveryConfig(BaseModel):
    # rfq:     evaluate combos that currently have open RFQs (proven; combos only
    #          appear when someone requests a quote).
    # markets: enumerate combo markets directly from /markets (price + legs are on
    #          the market object), covering the whole universe, not just open RFQs.
    source: str = "rfq"                       # rfq | markets
    # For markets mode: which series to enumerate. Empty = scan /markets broadly
    # (heavy). Confirm the exact series tickers for your account.
    series_tickers: list[str] = []
    market_status: str = "open"


class PollingConfig(BaseModel):
    interval_ms: int = 1000
    max_requests_per_sec: int = 8
    # Cap combos priced per scan (each costs 1 combo-market + N leg-market reads).
    max_combos_per_scan: int = 25
    # Reuse a leg price across combos only if fetched within this window. 0 = always
    # fetch fresh (no reuse) so a combo is never evaluated against a stale leg.
    leg_cache_ttl_ms: int = 0


class PersistenceConfig(BaseModel):
    db_path: str = "data/combo_arb.db"


class SettlementSimConfig(BaseModel):
    n_scenarios: int = 2000
    seed: int = 42


class SettlementConfig(BaseModel):
    # How often (wall-clock seconds) to poll open trades' leg markets for real
    # resolution. Throttled independently of polling.interval_ms since it costs
    # one API call per distinct open leg ticker, on top of the scan budget.
    check_interval_s: float = 30.0
    # Terminal handling for a trade whose leg can never be fetched (delisted /
    # rolled-off market that returns errors instead of a settled payload). A trade
    # older than this (wall-clock seconds) that still has an *un-fetchable* leg is
    # marked "expired" so it stops holding a risk slot forever. Only errored legs
    # trigger this -- a leg that fetches fine but isn't resolved yet (far-future
    # game) keeps the trade open normally. 0 disables. Default 24h.
    max_open_age_s: float = 86400.0


class Secrets(BaseSettings):
    """Loaded from environment / .env only."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    kalshi_api_key_id: Optional[str] = Field(default=None, alias="KALSHI_API_KEY_ID")
    kalshi_private_key_path: Optional[str] = Field(default=None, alias="KALSHI_PRIVATE_KEY_PATH")
    confirm_live_trading: Optional[str] = Field(default=None, alias="CONFIRM_LIVE_TRADING")

    @property
    def live_confirmed(self) -> bool:
        return (self.confirm_live_trading or "").strip().upper() == "YES"


class AppConfig(BaseModel):
    mode: Mode = Mode.PAPER
    environment: Environment = Environment.PROD
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    fees: FeesConfig = Field(default_factory=FeesConfig)
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    polling: PollingConfig = Field(default_factory=PollingConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    settlement_sim: SettlementSimConfig = Field(default_factory=SettlementSimConfig)
    settlement: SettlementConfig = Field(default_factory=SettlementConfig)

    # Secrets are attached at load time, excluded from serialization.
    secrets: Secrets = Field(default_factory=Secrets, exclude=True)

    @property
    def api_base_url(self) -> str:
        return API_BASE_URLS[self.environment.value]

    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> "AppConfig":
        """Load YAML config (if present) and overlay a few env overrides + secrets."""
        data: dict = {}
        path = path or os.environ.get("COMBO_ARB_CONFIG")
        if path and Path(path).exists():
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}

        # Env overrides for the two top-level switches.
        if os.environ.get("COMBO_ARB_MODE"):
            data["mode"] = os.environ["COMBO_ARB_MODE"]
        if os.environ.get("COMBO_ARB_ENVIRONMENT"):
            data["environment"] = os.environ["COMBO_ARB_ENVIRONMENT"]

        cfg = cls(**data)
        cfg.secrets = Secrets()
        return cfg

    def live_trading_armed(self) -> bool:
        """All three guards must hold before any real order can be placed."""
        return (
            self.execution.live_enabled
            and self.mode == Mode.LIVE
            and self.secrets.live_confirmed
        )
