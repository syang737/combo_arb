import pytest

from combo_arb.models import ComboLeg, ComboRFQ, LegPrice, Side
from combo_arb.pricing.model import (
    combo_implied_by_legs,
    implied_prob,
    price_combo,
)


def test_implied_prob_mid(cfg):
    lp = LegPrice(leg_ticker="A", best_bid=0.49, best_ask=0.51)
    assert implied_prob(lp, cfg.pricing) == pytest.approx(0.50)


def test_implied_prob_thin_book_falls_back_to_last(cfg):
    cfg.pricing.prob_source = "mid"
    cfg.pricing.thin_book_spread = 0.05
    lp = LegPrice(leg_ticker="A", best_bid=0.30, best_ask=0.70, last_trade_price=0.55)
    # spread 0.40 > 0.05 -> use last trade
    assert implied_prob(lp, cfg.pricing) == pytest.approx(0.55)


def test_implied_prob_last_source(cfg):
    cfg.pricing.prob_source = "last"
    lp = LegPrice(leg_ticker="A", best_bid=0.49, best_ask=0.51, last_trade_price=0.60)
    assert implied_prob(lp, cfg.pricing) == pytest.approx(0.60)


def test_product_model(cfg, legs, arb_rfq):
    fair = combo_implied_by_legs(arb_rfq, legs, cfg.pricing)
    assert fair == pytest.approx(0.50 * 0.40, abs=1e-9)


def test_correlation_factor_lifts(cfg, legs, arb_rfq):
    cfg.pricing.correlation_factor = 1.5
    fair = combo_implied_by_legs(arb_rfq, legs, cfg.pricing)
    assert fair == pytest.approx(0.20 * 1.5, abs=1e-9)


def test_no_side_leg_uses_complement(cfg):
    legs = {"A": LegPrice(leg_ticker="A", best_bid=0.69, best_ask=0.71)}
    rfq = ComboRFQ(
        rfq_id="r", mve_collection_ticker="C",
        legs=[ComboLeg(leg_ticker="A", side=Side.NO)], quote_yes=0.5, size=1,
    )
    fair = combo_implied_by_legs(rfq, legs, cfg.pricing)
    assert fair == pytest.approx(1 - 0.70, abs=1e-9)  # NO leg contributes 1-p


def test_missing_leg_price_unpriceable(cfg, arb_rfq):
    assert combo_implied_by_legs(arb_rfq, {}, cfg.pricing) is None
    assert price_combo(arb_rfq, {}, cfg) is None


def test_flagged_when_overpriced(cfg, legs, arb_rfq):
    res = price_combo(arb_rfq, legs, cfg)
    assert res is not None
    assert res.flagged is True
    assert res.arbitrage_margin > 0


def test_not_flagged_when_fair(cfg, legs, fair_rfq):
    res = price_combo(fair_rfq, legs, cfg)
    assert res is not None
    assert res.flagged is False
