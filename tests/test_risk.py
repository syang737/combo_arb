import pytest

from combo_arb.kalshi.mock_client import MockKalshiClient
from combo_arb.models import Side
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


def test_sizing_capped_by_capital(cfg, legs, underpriced_rfq):
    cfg.risk.capital_per_trade = 1.0  # tiny -> few contracts
    cfg.risk.max_contracts_per_trade = 1000
    rm = RiskManager(cfg)
    dec = rm.evaluate(_signal(cfg, legs, underpriced_rfq), legs)
    # per-contract capital = leg-NO hedge (0.40*0.50 + 0.50*0.60 = 0.50) + combo 0.10 = 0.60
    # floor(1.0 / 0.60) = 1
    assert dec.approved
    assert dec.qty == 1


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
