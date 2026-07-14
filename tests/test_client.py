"""Unit tests for the live client's pure logic (no network)."""

from combo_arb.config import AppConfig
from combo_arb.kalshi.client import KalshiClient, _cents_to_dollars
from combo_arb.models import Side


def test_cents_to_dollars():
    assert _cents_to_dollars(50) == 0.50
    assert _cents_to_dollars(1) == 0.01
    assert _cents_to_dollars(0) is None      # 0 = no quote
    assert _cents_to_dollars(None) is None
    assert _cents_to_dollars("bad") is None


def test_get_leg_price_converts_cents(monkeypatch):
    cfg = AppConfig()
    client = KalshiClient.__new__(KalshiClient)  # skip __init__ (no network/keys)
    client.cfg = cfg
    monkeypatch.setattr(
        client, "get_market",
        lambda ticker: {"yes_bid": 49, "yes_ask": 51, "last_price": 50},
    )
    lp = client.get_leg_price("A")
    assert lp.best_bid == 0.49 and lp.best_ask == 0.51 and lp.last_trade_price == 0.50
    assert lp.mid == 0.50


def test_parse_rfq_mve_legs():
    raw = {
        "rfq_id": "r1",
        "mve_collection_ticker": "COMBO_X",
        "mve_selected_legs": [
            {"ticker": "A", "side": "yes", "ratio": 1},
            {"ticker": "B", "side": "no", "ratio": 1},
        ],
        "yes_bid": 30,
        "size": 10,
    }
    rfq = KalshiClient._parse_rfq(raw)
    assert rfq is not None
    assert rfq.rfq_id == "r1"
    assert rfq.mve_collection_ticker == "COMBO_X"
    assert rfq.quote_yes == 0.30
    assert [leg.side for leg in rfq.legs] == [Side.YES, Side.NO]


def test_parse_rfq_no_legs_returns_none():
    assert KalshiClient._parse_rfq({"rfq_id": "r", "legs": []}) is None
