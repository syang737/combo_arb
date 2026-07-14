import pytest

from combo_arb.kalshi.mock_client import MockKalshiClient
from combo_arb.models import Side
from combo_arb.risk.risk import DeltaHedgeModel, RiskManager, leg_deltas
from combo_arb.scanner.scanner import Scanner


def _signal(cfg, legs, rfq):
    client = MockKalshiClient(leg_prices=legs, rfqs=[rfq])
    return Scanner(client, cfg).scan()[0]


def test_delta_hedge_quantities(cfg, legs, arb_rfq):
    sig = _signal(cfg, legs, arb_rfq)
    deltas = leg_deltas(sig, legs, cfg)
    # delta_A = p_B = 0.40 ; delta_B = p_A = 0.50
    assert deltas["A"] == pytest.approx(0.40, abs=1e-9)
    assert deltas["B"] == pytest.approx(0.50, abs=1e-9)


def test_hedge_orders_built(cfg, legs, arb_rfq):
    sig = _signal(cfg, legs, arb_rfq)
    combo, hedges = DeltaHedgeModel().build(sig, qty=20, leg_prices=legs, cfg=cfg)
    assert combo.action == "sell" and combo.side == Side.YES
    qtys = {o.instrument: o.qty for o in hedges}
    assert qtys["A"] == round(20 * 0.40)  # 8
    assert qtys["B"] == round(20 * 0.50)  # 10


def test_kill_switch_blocks(cfg, legs, arb_rfq):
    cfg.risk.kill_switch = True
    rm = RiskManager(cfg)
    dec = rm.evaluate(_signal(cfg, legs, arb_rfq), legs)
    assert not dec.approved and "kill_switch" in dec.reason


def test_sizing_capped_by_capital(cfg, legs, arb_rfq):
    cfg.risk.capital_per_trade = 1.0  # tiny -> few contracts
    cfg.risk.max_contracts_per_trade = 1000
    rm = RiskManager(cfg)
    sig = _signal(cfg, legs, arb_rfq)
    dec = rm.evaluate(sig, legs)
    # per-contract hedge cost ~ 0.40 -> floor(1.0 / 0.40) = 2 contracts (rounds down)
    assert dec.approved
    assert dec.qty == 2


def test_max_open_signals(cfg, legs, arb_rfq):
    cfg.risk.max_open_signals = 0
    rm = RiskManager(cfg)
    dec = rm.evaluate(_signal(cfg, legs, arb_rfq), legs)
    assert not dec.approved and "max_open_signals" in dec.reason


def test_exposure_limit(cfg, legs, arb_rfq):
    cfg.risk.max_total_exposure = 0.01
    rm = RiskManager(cfg)
    dec = rm.evaluate(_signal(cfg, legs, arb_rfq), legs)
    assert not dec.approved and "exposure" in dec.reason
