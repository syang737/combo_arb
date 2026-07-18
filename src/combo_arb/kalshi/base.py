"""Shared market-data client interface.

Both the live :class:`~combo_arb.kalshi.client.KalshiClient` and the offline
:class:`~combo_arb.kalshi.mock_client.MockKalshiClient` implement this, so the
scanner / controller are agnostic to the data source.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from combo_arb.models import ComboRFQ, LegPrice


class MarketDataClient(ABC):
    @abstractmethod
    def get_leg_price(self, ticker: str) -> LegPrice:
        """Top-of-book snapshot for one leg market."""

    def get_combo_quote(self, rfq: ComboRFQ) -> Optional[float]:
        """Fresh executable combo price at evaluation time.

        Default returns the price attached at discovery (fine for the offline
        mock, whose data is static). The live client overrides this to re-read the
        combo market so the quote is captured together with its legs.
        """
        return rfq.quote_yes

    def get_leg_prices(self, tickers: list[str]) -> dict[str, LegPrice]:
        """Snapshots for several legs (default: per-ticker calls; override to batch)."""
        return {t: self.get_leg_price(t) for t in tickers}

    @abstractmethod
    def get_combo_rfqs(self) -> list[ComboRFQ]:
        """Current combo (multivariate event) quotes / RFQs to evaluate."""

    def close(self) -> None:  # pragma: no cover - trivial
        pass
