import math

import pytest

from combo_arb.config import FeesConfig
from combo_arb.pricing.fees import fee, maker_fee, marginal_fee_per_contract, taker_fee


@pytest.fixture
def fees() -> FeesConfig:
    return FeesConfig()


def test_taker_fee_schedule(fees):
    # Documented values (aggregate rounded up to the cent).
    assert taker_fee(0.50, 100, fees) == 1.75
    assert taker_fee(0.10, 100, fees) == 0.63
    assert taker_fee(0.90, 100, fees) == 0.63
    assert taker_fee(0.50, 1, fees) == 0.02  # single contract rounds up


def test_taker_fee_no_float_overshoot(fees):
    # 0.07*0.5*0.5*100 == 1.75 exactly in intent; must not ceil to 1.76.
    assert taker_fee(0.50, 100, fees) == 1.75


def test_maker_is_quarter_of_taker(fees):
    # Before rounding maker == 25% of taker; check the unrounded marginal rate.
    t = marginal_fee_per_contract(0.50, maker=False, cfg=fees)
    m = marginal_fee_per_contract(0.50, maker=True, cfg=fees)
    assert math.isclose(m, 0.25 * t)


def test_fee_dispatch(fees):
    assert fee(0.50, 100, maker=False, cfg=fees) == taker_fee(0.50, 100, fees)
    assert fee(0.50, 100, maker=True, cfg=fees) == maker_fee(0.50, 100, fees)


def test_marginal_fee_peaks_at_half(fees):
    assert marginal_fee_per_contract(0.50, cfg=fees) > marginal_fee_per_contract(0.10, cfg=fees)
    assert marginal_fee_per_contract(0.50, cfg=fees) == pytest.approx(0.0175)


def test_price_clamped(fees):
    # Prices outside 1c..99c are clamped, never negative fees.
    assert taker_fee(0.0, 100, fees) >= 0.0
    assert taker_fee(1.0, 100, fees) >= 0.0
