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
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}


class Mode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class Environment(str, Enum):
    PROD = "prod"
    DEMO = "demo"


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


class RiskConfig(BaseModel):
    capital_per_trade: float = 100.0
    max_contracts_per_trade: int = 100
    max_position_per_market: int = 500
    max_total_exposure: float = 5000.0
    max_open_signals: int = 25
    kill_switch: bool = False


class ExecutionConfig(BaseModel):
    live_enabled: bool = False
    fill_model: str = "taker_cross"     # taker_cross | mid | depth_prob
    combo_fill_price: str = "yes_bid"   # yes_bid | quote_yes


class PollingConfig(BaseModel):
    interval_ms: int = 1000
    max_requests_per_sec: int = 8


class PersistenceConfig(BaseModel):
    db_path: str = "data/combo_arb.db"


class SettlementSimConfig(BaseModel):
    n_scenarios: int = 2000
    seed: int = 42


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
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    fees: FeesConfig = Field(default_factory=FeesConfig)
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    polling: PollingConfig = Field(default_factory=PollingConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    settlement_sim: SettlementSimConfig = Field(default_factory=SettlementSimConfig)

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
