import pytest

from combo_arb.config import AppConfig
from combo_arb.kalshi.client import KalshiClient
from combo_arb.kalshi.mock_client import MockKalshiClient
from combo_arb.models import ComboLeg, ComboRFQ, LegPrice
from combo_arb.scanner.scanner import Scanner


class _CountingClient(MockKalshiClient):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fetched: list[str] = []

    def get_leg_prices(self, tickers):
        self.fetched.extend(tickers)
        return super().get_leg_prices(tickers)


def test_leg_cache_fetches_each_leg_once(cfg):
    legs = {
        "A": LegPrice(leg_ticker="A", best_bid=0.49, best_ask=0.51),
        "B": LegPrice(leg_ticker="B", best_bid=0.39, best_ask=0.41),
        "C": LegPrice(leg_ticker="C", best_bid=0.29, best_ask=0.31),
    }
    rfqs = [
        ComboRFQ(rfq_id="1", mve_collection_ticker="C1",
                 legs=[ComboLeg(leg_ticker="A"), ComboLeg(leg_ticker="B")],
                 quote_yes=0.10, size=20),
        ComboRFQ(rfq_id="2", mve_collection_ticker="C2",
                 legs=[ComboLeg(leg_ticker="A"), ComboLeg(leg_ticker="C")],  # A shared
                 quote_yes=0.10, size=20),
    ]
    client = _CountingClient(leg_prices=legs, rfqs=rfqs)
    Scanner(client, cfg).scan()
    # A appears in both combos but must be fetched only once.
    assert sorted(client.fetched) == ["A", "B", "C"]


def test_markets_enumeration_builds_priced_combos(monkeypatch):
    cfg = AppConfig()
    cfg.discovery.source = "markets"  # default direction is buy -> uses yes_ask
    client = KalshiClient.__new__(KalshiClient)  # skip __init__ (no network)
    client.cfg = cfg

    fake = {
        "markets": [
            {
                "ticker": "KXMVE-COMBO1",
                "mve_collection_ticker": "KXMVE-R",
                "mve_selected_legs": [
                    {"market_ticker": "LEG-A", "side": "yes"},
                    {"market_ticker": "LEG-B", "side": "no"},
                ],
                "yes_ask_dollars": "0.3380",
            },
            {"ticker": "NOT-A-COMBO"},  # no mve_selected_legs -> skipped
        ],
        "cursor": None,
    }
    monkeypatch.setattr(client, "_get", lambda ep, params=None: fake)

    combos = client.get_combo_rfqs()
    assert len(combos) == 1
    c = combos[0]
    assert c.market_ticker == "KXMVE-COMBO1"
    assert c.quote_yes == pytest.approx(0.338)   # buying -> yes_ask
    assert [leg.side.value for leg in c.legs] == ["yes", "no"]
