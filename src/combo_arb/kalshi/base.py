"""Shared market-data client interface.

Both the live :class:`~combo_arb.kalshi.client.KalshiClient` and the offline
:class:`~combo_arb.kalshi.mock_client.MockKalshiClient` implement this, so the
scanner / controller are agnostic to the data source.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from combo_arb.models import ComboRFQ, LegPrice


class MarketDataClient(ABC):
    @abstractmethod
    def get_leg_price(self, ticker: str) -> LegPrice:
        """Top-of-book snapshot for one leg market."""

    def get_leg_prices(self, tickers: list[str]) -> dict[str, LegPrice]:
        """Snapshots for several legs (default: per-ticker calls; override to batch)."""
        return {t: self.get_leg_price(t) for t in tickers}

    @abstractmethod
    def get_combo_rfqs(self) -> list[ComboRFQ]:
        """Current combo (multivariate event) quotes / RFQs to evaluate."""

    def close(self) -> None:  # pragma: no cover - trivial
        pass
