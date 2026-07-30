import pytest

from combo_arb.kalshi.mock_client import MockKalshiClient
from combo_arb.models import ArbSignal, ComboLeg, LegPrice, Side, SignalAction
from combo_arb.risk.risk import DeltaHedgeModel, RiskManager, leg_deltas
from combo_arb.scanner.scanner import Scanner


def _signal(cfg, legs, rfq):
    client = MockKalshiClient(leg_prices=legs, rfqs=[rfq])
    return Scanner(client, cfg).scan()[0]


def test_delta_hedge_quantities(cfg, legs, underpriced_rfq):
    sig = _signal(cfg, legs, underpriced_rfq)
    deltas = leg_deltas(sig, legs, cfg)
    # delta_A = p_B = 0.40 ; delta_B = p_A = 0.50 (independent of direction)
    assert deltas["A"] == pytest.approx(0.40, abs=1e-9)
    assert deltas["B"] == pytest.approx(0.50, abs=1e-9)


def test_buy_hedge_shorts_legs(cfg, legs, underpriced_rfq):
    # buy_underpriced: buy the combo YES, hedge by BUYING leg NO (shorting legs).
    sig = _signal(cfg, legs, underpriced_rfq)
    combo, hedges = DeltaHedgeModel().build(sig, qty=20, leg_prices=legs, cfg=cfg)
    assert combo.action == "buy" and combo.side == Side.YES
    assert all(o.side == Side.NO and o.action == "buy" for o in hedges)
    qtys = {o.instrument: o.qty for o in hedges}
    assert qtys["A"] == round(20 * 0.40)  # 8
    assert qtys["B"] == round(20 * 0.50)  # 10


def test_sell_hedge_buys_leg_yes(cfg, legs, overpriced_rfq):
    cfg.strategy.direction = "sell_overpriced"
    sig = _signal(cfg, legs, overpriced_rfq)
    combo, hedges = DeltaHedgeModel().build(sig, qty=20, leg_prices=legs, cfg=cfg)
    assert combo.action == "sell" and combo.side == Side.YES
    assert all(o.side == Side.YES and o.action == "buy" for o in hedges)


def test_kill_switch_blocks(cfg, legs, underpriced_rfq):
    cfg.risk.kill_switch = True
    rm = RiskManager(cfg)
    dec = rm.evaluate(_signal(cfg, legs, underpriced_rfq), legs)
    assert not dec.approved and "kill_switch" in dec.reason


def test_cannot_fully_hedge_is_signal_only(cfg, legs, underpriced_rfq):
    # Tight capital: a fully-hedged trade needs qty>=3 (min delta 0.40 -> ceil(1/0.40)),
    # but floor(1.0/0.60)=1 is affordable. Must NOT trade a naked 1-lot -> signal-only.
    cfg.risk.capital_per_trade = 1.0
    cfg.risk.max_contracts_per_trade = 1000
    rm = RiskManager(cfg)
    dec = rm.evaluate(_signal(cfg, legs, underpriced_rfq), legs)
    assert not dec.approved and "hedge" in dec.reason


def test_full_hedge_sizes_up(cfg, legs, underpriced_rfq):
    # Ample capital: size to the smallest fully-hedged qty and hedge every leg (>=1 each).
    cfg.risk.capital_per_trade = 100.0
    rm = RiskManager(cfg)
    dec = rm.evaluate(_signal(cfg, legs, underpriced_rfq), legs)
    assert dec.approved
    assert dec.qty == 3  # ceil(1 / min delta 0.40)
    qtys = {o.instrument: o.qty for o in dec.hedge_orders}
    assert len(dec.hedge_orders) == 2
    assert qtys["A"] >= 1 and qtys["B"] >= 1  # nothing dropped


def test_max_leg_price_rejects_overpriced_leg(cfg):
    # RiskManager.evaluate() tested directly against a hand-built (already-flagged)
    # signal, rather than routing through the scanner -- an extreme leg price would
    # collapse the fair value and stop the scanner from flagging it as underpriced in
    # the first place, which isn't what this test is about.
    #
    # Leg A's NO-hedge entry price = 1 - yes_bid = 1 - 0.01 = 0.99, over the default
    # 0.95 cap -- nearly certain, so hedging it buys almost no real protection.
    leg_prices = {
        "A": LegPrice(leg_ticker="A", best_bid=0.01, best_ask=0.03, last_trade_price=0.02),
        "B": LegPrice(leg_ticker="B", best_bid=0.39, best_ask=0.41, last_trade_price=0.40),
    }
    sig = ArbSignal(
        rfq_id="rfq-cap", mve_collection_ticker="COMBO_AB",
        legs=[ComboLeg(leg_ticker="A"), ComboLeg(leg_ticker="B")],
        leg_prices=leg_prices, combo_quote_yes=0.001, fair_combo=0.008,
        fees_estimate=0.001, margin_threshold=0.001, arbitrage_margin=0.006,
        size=20, action=SignalAction.HEDGE_VIA_LEGS,
    )
    rm = RiskManager(cfg)
    dec = rm.evaluate(sig, leg_prices)
    assert not dec.approved
    assert "max_leg_price" in dec.reason and "A" in dec.reason


def test_max_leg_price_allows_legs_under_cap(cfg, legs, underpriced_rfq):
    # Default fixture legs (NO prices 0.51, 0.61) are both comfortably under 0.95.
    sig = _signal(cfg, legs, underpriced_rfq)
    rm = RiskManager(cfg)
    dec = rm.evaluate(sig, legs)
    assert dec.approved


def _n_leg_signal(n: int) -> ArbSignal:
    """A combo with n legs, each priced at 0.5 (mid-book, well under max_leg_price
    and with a large enough discount to fair to be clearly flagged) -- just enough
    to exercise the max_legs gate in isolation from the other gates."""
    legs = [ComboLeg(leg_ticker=f"L{i}") for i in range(n)]
    leg_prices = {f"L{i}": LegPrice(leg_ticker=f"L{i}", best_bid=0.49, best_ask=0.51,
                                    last_trade_price=0.5) for i in range(n)}
    fair = 0.5 ** n
    return ArbSignal(
        rfq_id=f"rfq-{n}legs", mve_collection_ticker="C", legs=legs, leg_prices=leg_prices,
        combo_quote_yes=fair * 0.5, fair_combo=fair, fees_estimate=0.001,
        margin_threshold=0.001, arbitrage_margin=fair * 0.5 - 0.001,
        size=100, action=SignalAction.HEDGE_VIA_LEGS,
    )


def test_max_legs_rejects_too_many_legs(cfg):
    sig = _n_leg_signal(4)  # default max_legs is 3
    rm = RiskManager(cfg)
    dec = rm.evaluate(sig, sig.leg_prices)
    assert not dec.approved
    assert "max_legs" in dec.reason


def test_max_legs_allows_legs_at_cap(cfg):
    sig = _n_leg_signal(3)  # exactly at the default cap
    rm = RiskManager(cfg)
    dec = rm.evaluate(sig, sig.leg_prices)
    assert dec.approved


def test_max_open_signals(cfg, legs, underpriced_rfq):
    cfg.risk.max_open_signals = 0
    rm = RiskManager(cfg)
    dec = rm.evaluate(_signal(cfg, legs, underpriced_rfq), legs)
    assert not dec.approved and "max_open_signals" in dec.reason


def test_exposure_limit(cfg, legs, underpriced_rfq):
    cfg.risk.max_total_exposure = 0.01
    rm = RiskManager(cfg)
    dec = rm.evaluate(_signal(cfg, legs, underpriced_rfq), legs)
    assert not dec.approved and "exposure" in dec.reason
