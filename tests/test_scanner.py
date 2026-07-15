from combo_arb.kalshi.mock_client import MockKalshiClient
from combo_arb.models import SignalAction
from combo_arb.scanner.scanner import Scanner


def test_scanner_flags_underpriced_by_default(cfg, legs, underpriced_rfq):
    # Default direction buy_underpriced -> flag the cheap combo.
    client = MockKalshiClient(leg_prices=legs, rfqs=[underpriced_rfq])
    signals = Scanner(client, cfg).scan()
    assert len(signals) == 1
    s = signals[0]
    assert s.action == SignalAction.HEDGE_VIA_LEGS
    assert s.combo_quote_yes < s.fair_combo   # underpriced


def test_scanner_skips_overpriced_in_buy_mode(cfg, legs, overpriced_rfq):
    client = MockKalshiClient(leg_prices=legs, rfqs=[overpriced_rfq])
    assert Scanner(client, cfg).scan() == []


def test_scanner_sell_mode_flags_overpriced(cfg, legs, overpriced_rfq):
    cfg.strategy.direction = "sell_overpriced"
    client = MockKalshiClient(leg_prices=legs, rfqs=[overpriced_rfq])
    signals = Scanner(client, cfg).scan()
    assert len(signals) == 1
    assert signals[0].combo_quote_yes > signals[0].fair_combo


def test_scanner_skips_fair(cfg, legs, fair_rfq):
    client = MockKalshiClient(leg_prices=legs, rfqs=[fair_rfq])
    assert Scanner(client, cfg).scan() == []


def test_scanner_records_last_rfqs(cfg, legs, underpriced_rfq, fair_rfq):
    client = MockKalshiClient(leg_prices=legs, rfqs=[underpriced_rfq, fair_rfq])
    scanner = Scanner(client, cfg)
    signals = scanner.scan()
    assert len(scanner.last_rfqs) == 2  # both seen
    assert len(signals) == 1            # only one flagged


def test_scanner_default_scenario(cfg):
    # Built-in synthetic scenario: COMBO_AB is buyable, COMBO_ABC is not.
    signals = Scanner(MockKalshiClient(), cfg).scan()
    tickers = {s.mve_collection_ticker for s in signals}
    assert "COMBO_AB" in tickers
    assert "COMBO_ABC" not in tickers
