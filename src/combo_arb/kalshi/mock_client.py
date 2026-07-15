"""Offline mock client — no credentials required.

Serves a fixed or randomly-walked snapshot of leg prices + combo RFQs so the
scanner, paper engine, controller and tests can run end-to-end without touching
the network. ``synthetic_scenario`` is the single source of the demo data (also
reused to seed the replay sample file).
"""

from __future__ import annotations

import random

from combo_arb.kalshi.base import MarketDataClient
from combo_arb.models import ComboLeg, ComboRFQ, LegPrice, Side


def synthetic_scenario() -> tuple[dict[str, LegPrice], list[ComboRFQ]]:
    """A small, deterministic market.

    Legs A/B/C with mids 0.50 / 0.40 / 0.30.
      * COMBO_AB (legs A,B): fair ~= 0.50*0.40 = 0.20, quoted YES 0.10 -> BUYABLE
        under the default buy_underpriced direction.
      * COMBO_ABC (legs A,B,C): fair ~= 0.06, quoted YES 0.065 -> no edge after fees.
    """
    legs = {
        "A": LegPrice(leg_ticker="A", best_bid=0.49, best_ask=0.51, last_trade_price=0.50),
        "B": LegPrice(leg_ticker="B", best_bid=0.39, best_ask=0.41, last_trade_price=0.40),
        "C": LegPrice(leg_ticker="C", best_bid=0.29, best_ask=0.31, last_trade_price=0.30),
    }
    rfqs = [
        ComboRFQ(
            rfq_id="rfq-ab",
            mve_collection_ticker="COMBO_AB",
            legs=[ComboLeg(leg_ticker="A"), ComboLeg(leg_ticker="B")],
            quote_yes=0.10,
            quote_no=0.88,
            size=20,
        ),
        ComboRFQ(
            rfq_id="rfq-abc",
            mve_collection_ticker="COMBO_ABC",
            legs=[ComboLeg(leg_ticker="A"), ComboLeg(leg_ticker="B"), ComboLeg(leg_ticker="C")],
            quote_yes=0.065,
            quote_no=0.93,
            size=15,
        ),
    ]
    return legs, rfqs


class MockKalshiClient(MarketDataClient):
    def __init__(
        self,
        leg_prices: dict[str, LegPrice] | None = None,
        rfqs: list[ComboRFQ] | None = None,
        *,
        random_walk: bool = False,
        seed: int = 42,
    ):
        if leg_prices is None or rfqs is None:
            gen_legs, gen_rfqs = synthetic_scenario()
            leg_prices = leg_prices or gen_legs
            rfqs = rfqs or gen_rfqs
        self._legs = leg_prices
        self._rfqs = rfqs
        self._random_walk = random_walk
        self._rng = random.Random(seed)

    def _maybe_walk(self) -> None:
        if not self._random_walk:
            return
        for lp in self._legs.values():
            if lp.best_bid is None or lp.best_ask is None:
                continue
            step = self._rng.uniform(-0.01, 0.01)
            mid = max(0.02, min(0.98, (lp.best_bid + lp.best_ask) / 2 + step))
            half = (lp.best_ask - lp.best_bid) / 2
            lp.best_bid = round(mid - half, 4)
            lp.best_ask = round(mid + half, 4)
            lp.last_trade_price = round(mid, 4)

    def get_leg_price(self, ticker: str) -> LegPrice:
        if ticker not in self._legs:
            raise KeyError(f"unknown leg ticker: {ticker}")
        return self._legs[ticker]

    def get_leg_prices(self, tickers: list[str]) -> dict[str, LegPrice]:
        self._maybe_walk()
        return {t: self._legs[t] for t in tickers if t in self._legs}

    def get_combo_rfqs(self) -> list[ComboRFQ]:
        return list(self._rfqs)
