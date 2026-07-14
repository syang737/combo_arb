from combo_arb.kalshi.mock_client import MockKalshiClient
from combo_arb.models import SignalAction
from combo_arb.scanner.scanner import Scanner


def test_scanner_flags_overpriced(cfg, legs, arb_rfq):
    client = MockKalshiClient(leg_prices=legs, rfqs=[arb_rfq])
    signals = Scanner(client, cfg).scan()
    assert len(signals) == 1
    s = signals[0]
    assert s.rfq_id == "rfq-ab"
    assert s.action == SignalAction.HEDGE_VIA_LEGS
    assert s.combo_quote_yes > s.fair_combo


def test_scanner_skips_fair(cfg, legs, fair_rfq):
    client = MockKalshiClient(leg_prices=legs, rfqs=[fair_rfq])
    assert Scanner(client, cfg).scan() == []


def test_scanner_records_last_rfqs(cfg, legs, arb_rfq, fair_rfq):
    client = MockKalshiClient(leg_prices=legs, rfqs=[arb_rfq, fair_rfq])
    scanner = Scanner(client, cfg)
    signals = scanner.scan()
    assert len(scanner.last_rfqs) == 2  # both seen
    assert len(signals) == 1            # only one flagged


def test_scanner_default_scenario(cfg):
    # Built-in synthetic scenario: COMBO_AB is the arb, COMBO_ABC is not.
    signals = Scanner(MockKalshiClient(), cfg).scan()
    tickers = {s.mve_collection_ticker for s in signals}
    assert "COMBO_AB" in tickers
    assert "COMBO_ABC" not in tickers
