import pytest

from combo_arb.execution.paper import PaperExecutionEngine
from combo_arb.execution.settlement import (
    HedgedTrade,
    expected_pnl_for_orders,
    immediate_cash,
    simulate_pnl,
)
from combo_arb.kalshi.mock_client import MockKalshiClient
from combo_arb.models import Fill, InstrumentType, Order, Side
from combo_arb.pricing.fees import taker_fee
from combo_arb.pricing.model import implied_prob
from combo_arb.risk.risk import RiskManager
from combo_arb.scanner.scanner import Scanner


def test_leg_buy_fills_at_ask(cfg, legs):
    eng = PaperExecutionEngine(cfg)
    order = Order(
        instrument="A", instrument_type=InstrumentType.LEG, side=Side.YES,
        action="buy", price=0.51, qty=10,
    )
    fills = eng.execute([order], legs)
    assert fills[0].price == pytest.approx(0.51)  # crosses to ask
    assert fills[0].fee > 0


def test_combo_sell_fills_at_quote(cfg, legs):
    eng = PaperExecutionEngine(cfg)
    order = Order(
        instrument="COMBO_AB", instrument_type=InstrumentType.COMBO, side=Side.YES,
        action="sell", price=0.30, qty=20,
    )
    fills = eng.execute([order], legs)
    assert fills[0].price == pytest.approx(0.30)


def test_hedged_trade_positive_expected_pnl(cfg, legs, underpriced_rfq):
    """Underpriced combo -> buying it + short-leg hedge has positive expected PnL."""
    sig = Scanner(MockKalshiClient(leg_prices=legs, rfqs=[underpriced_rfq]), cfg).scan()[0]
    rm = RiskManager(cfg)
    dec = rm.evaluate(sig, legs)
    fills = PaperExecutionEngine(cfg).execute(dec.all_orders, legs)

    combo_fill = next(f for f in fills if f.instrument == "COMBO_AB")
    hedge_fills = [f for f in fills if f.instrument != "COMBO_AB"]
    leg_probs = {leg.leg_ticker: implied_prob(legs[leg.leg_ticker], cfg.pricing)
                 for leg in sig.legs}
    trade = HedgedTrade(sig, combo_fill, hedge_fills, leg_probs)

    stats = simulate_pnl(trade, n_scenarios=5000, seed=1)
    assert stats["expected_pnl"] > 0
    assert stats["pnl_std"] > 0  # imperfect hedge -> residual variance
    assert stats["immediate_cash"] == pytest.approx(immediate_cash(trade))


def test_settlement_all_yes_pays_out(cfg):
    """Deterministic settlement: combo pays $1 iff all legs YES."""
    from combo_arb.models import ArbSignal, ComboLeg, Fill, LegPrice, SignalAction

    sig = ArbSignal(
        rfq_id="r", mve_collection_ticker="C",
        legs=[ComboLeg(leg_ticker="A"), ComboLeg(leg_ticker="B")],
        leg_prices={"A": LegPrice(leg_ticker="A"), "B": LegPrice(leg_ticker="B")},
        combo_quote_yes=0.30, fair_combo=0.20, fees_estimate=0.0,
        margin_threshold=0.0, arbitrage_margin=0.1, size=1,
        action=SignalAction.HEDGE_VIA_LEGS,
    )
    combo_fill = Fill(order_id="o", instrument="C", side=Side.YES, action="sell",
                      price=0.30, qty=1, fee=0.0)
    trade = HedgedTrade(sig, combo_fill, hedge_fills=[], leg_probs={"A": 1.0, "B": 1.0})
    # Both legs certain YES -> combo resolves YES -> short pays 1, keeps 0.30 => -0.70
    stats = simulate_pnl(trade, n_scenarios=10, seed=1)
    assert stats["expected_pnl"] == pytest.approx(-0.70)


def test_expected_pnl_for_orders_matches_simulate_pnl(cfg, legs, underpriced_rfq):
    """The pre-trade EV estimate (built from intended ORDERS, before anything is
    placed) must agree with simulate_pnl on the equivalent already-filled trade --
    it's meant to be the same math, just computed one step earlier."""
    sig = Scanner(MockKalshiClient(leg_prices=legs, rfqs=[underpriced_rfq]), cfg).scan()[0]
    rm = RiskManager(cfg)
    dec = rm.evaluate(sig, legs)
    leg_probs = {leg.leg_ticker: implied_prob(legs[leg.leg_ticker], cfg.pricing) for leg in sig.legs}

    pre_trade_ev = expected_pnl_for_orders(
        sig, dec.all_orders, leg_probs, cfg,
        n_scenarios=cfg.settlement_sim.n_scenarios, seed=cfg.settlement_sim.seed,
    )

    combo_fill = Fill(
        order_id="x", instrument=dec.combo_order.instrument, instrument_type=InstrumentType.COMBO,
        side=dec.combo_order.side, action=dec.combo_order.action, price=dec.combo_order.price,
        qty=dec.combo_order.qty, fee=taker_fee(dec.combo_order.price, dec.combo_order.qty, cfg.fees),
    )
    hedge_fills = [
        Fill(order_id="y", instrument=o.instrument, instrument_type=o.instrument_type,
             side=o.side, action=o.action, price=o.price, qty=o.qty,
             fee=taker_fee(o.price, o.qty, cfg.fees))
        for o in dec.hedge_orders
    ]
    trade = HedgedTrade(sig, combo_fill, hedge_fills, leg_probs)
    expected = simulate_pnl(
        trade, n_scenarios=cfg.settlement_sim.n_scenarios, seed=cfg.settlement_sim.seed
    )["expected_pnl"]

    assert pre_trade_ev == pytest.approx(expected)


def test_expected_pnl_for_orders_no_combo_returns_zero(cfg, legs, underpriced_rfq):
    """A list of orders with no COMBO instrument_type (shouldn't happen in practice,
    but must not raise) returns 0.0 rather than crashing on a missing combo fill."""
    sig = Scanner(MockKalshiClient(leg_prices=legs, rfqs=[underpriced_rfq]), cfg).scan()[0]
    leg_only = Order(instrument="A", instrument_type=InstrumentType.LEG, side=Side.NO,
                     action="buy", price=0.5, qty=1)
    assert expected_pnl_for_orders(sig, [leg_only], {}, cfg) == 0.0
