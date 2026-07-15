import pytest

from combo_arb.config import AppConfig
from combo_arb.models import ComboLeg, ComboRFQ, LegPrice


@pytest.fixture
def cfg() -> AppConfig:
    """Default config (no YAML needed) with deterministic settlement sim."""
    c = AppConfig()
    c.settlement_sim.n_scenarios = 3000
    c.settlement_sim.seed = 1
    return c


@pytest.fixture
def legs() -> dict[str, LegPrice]:
    return {
        "A": LegPrice(leg_ticker="A", best_bid=0.49, best_ask=0.51, last_trade_price=0.50),
        "B": LegPrice(leg_ticker="B", best_bid=0.39, best_ask=0.41, last_trade_price=0.40),
    }


@pytest.fixture
def underpriced_rfq() -> ComboRFQ:
    """AB combo: fair ~0.20, quoted YES 0.10 -> BUYABLE (buy_underpriced)."""
    return ComboRFQ(
        rfq_id="rfq-ab-under",
        mve_collection_ticker="COMBO_AB",
        legs=[ComboLeg(leg_ticker="A"), ComboLeg(leg_ticker="B")],
        quote_yes=0.10,
        size=20,
    )


@pytest.fixture
def overpriced_rfq() -> ComboRFQ:
    """AB combo: fair ~0.20, quoted YES 0.30 -> SELLABLE (sell_overpriced)."""
    return ComboRFQ(
        rfq_id="rfq-ab-over",
        mve_collection_ticker="COMBO_AB",
        legs=[ComboLeg(leg_ticker="A"), ComboLeg(leg_ticker="B")],
        quote_yes=0.30,
        size=20,
    )


@pytest.fixture
def fair_rfq() -> ComboRFQ:
    """AB combo quoted at fair -> flags in neither direction."""
    return ComboRFQ(
        rfq_id="rfq-ab-fair",
        mve_collection_ticker="COMBO_AB",
        legs=[ComboLeg(leg_ticker="A"), ComboLeg(leg_ticker="B")],
        quote_yes=0.20,
        size=20,
    )
