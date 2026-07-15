"""Arbitrage scanner.

For each combo RFQ: fetch the legs' top-of-book, compute the fair combo value
(product of leg probabilities), and flag when

    combo_quote_yes > fair_combo + margin_threshold

i.e. the combo YES is *overpriced* relative to the joint probability implied by
the underlyings, by more than fees + buffer. Per the design, only overpriced-YES
is actionable (we do not assume the combo NO side is tradeable). Flagged signals
carry ``HEDGE_VIA_LEGS``; the controller/risk layer decides whether the hedge is
operationally executable or the signal is emitted as ``SIGNAL_ONLY``.
"""

from __future__ import annotations

import logging

from combo_arb.config import AppConfig
from combo_arb.kalshi.base import MarketDataClient
from combo_arb.models import ArbSignal, ComboEvaluation, SignalAction
from combo_arb.pricing.model import price_combo

log = logging.getLogger(__name__)


class Scanner:
    def __init__(self, client: MarketDataClient, cfg: AppConfig):
        self.client = client
        self.cfg = cfg
        self.last_rfqs: list = []               # RFQs seen in the most recent scan
        self.last_evaluations: list[ComboEvaluation] = []  # every priceable combo

    def scan(self) -> list[ArbSignal]:
        """Return flagged arbitrage signals; record all evaluations as a side effect."""
        signals: list[ArbSignal] = []
        evaluations: list[ComboEvaluation] = []
        rfqs = self.client.get_combo_rfqs()
        self.last_rfqs = rfqs
        for rfq in rfqs:
            tickers = [leg.leg_ticker for leg in rfq.legs]
            leg_prices = self.client.get_leg_prices(tickers)
            if len(leg_prices) < len(tickers):
                log.debug("skipping %s: missing leg prices", rfq.rfq_id)
                continue

            result = price_combo(rfq, leg_prices, self.cfg)
            if result is None:
                log.debug("skipping %s: unpriceable", rfq.rfq_id)
                continue

            evaluations.append(
                ComboEvaluation(
                    rfq_id=rfq.rfq_id,
                    mve_collection_ticker=rfq.mve_collection_ticker,
                    direction=self.cfg.strategy.direction,
                    combo_quote_yes=rfq.quote_yes,
                    fair_combo=result.fair_combo,
                    fees_estimate=result.fees_estimate,
                    buffer=result.buffer,
                    arbitrage_margin=result.arbitrage_margin,
                    flagged=result.flagged,
                )
            )
            if not result.flagged:
                continue

            signals.append(
                ArbSignal(
                    rfq_id=rfq.rfq_id,
                    mve_collection_ticker=rfq.mve_collection_ticker,
                    legs=rfq.legs,
                    leg_prices=leg_prices,
                    combo_quote_yes=rfq.quote_yes,
                    fair_combo=result.fair_combo,
                    fees_estimate=result.fees_estimate,
                    margin_threshold=result.margin_threshold,
                    arbitrage_margin=result.arbitrage_margin,
                    size=rfq.size,
                    action=SignalAction.HEDGE_VIA_LEGS,
                )
            )
        self.last_evaluations = evaluations
        return signals
