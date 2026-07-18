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


def _shared_leg_setup():
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
    return legs, rfqs


def test_legs_fresh_by_default_no_reuse(cfg):
    # ttl 0 (default): a shared leg is re-fetched for each combo (no stale reuse).
    assert cfg.polling.leg_cache_ttl_ms == 0
    legs, rfqs = _shared_leg_setup()
    client = _CountingClient(leg_prices=legs, rfqs=rfqs)
    Scanner(client, cfg).scan()
    assert client.fetched.count("A") == 2   # fetched fresh per combo


def test_leg_reuse_within_ttl(cfg):
    cfg.polling.leg_cache_ttl_ms = 60_000    # 60s window -> reuse
    legs, rfqs = _shared_leg_setup()
    client = _CountingClient(leg_prices=legs, rfqs=rfqs)
    Scanner(client, cfg).scan()
    assert client.fetched.count("A") == 1   # reused within TTL


def test_markets_enumeration_builds_combo_identities(monkeypatch):
    cfg = AppConfig()
    cfg.discovery.source = "markets"
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
    assert c.quote_yes is None                     # priced later, fresh, in the loop
    assert [leg.side.value for leg in c.legs] == ["yes", "no"]


def test_get_combo_quote_reads_fresh_price(monkeypatch):
    cfg = AppConfig()  # buy direction -> uses yes_ask
    client = KalshiClient.__new__(KalshiClient)
    client.cfg = cfg
    monkeypatch.setattr(client, "get_market",
                        lambda ticker: {"yes_ask_dollars": "0.3380", "yes_bid_dollars": "0.3270"})
    rfq = ComboRFQ(rfq_id="x", mve_collection_ticker="K", market_ticker="KXMVE-COMBO1",
                   legs=[ComboLeg(leg_ticker="LEG-A")])
    assert client.get_combo_quote(rfq) == pytest.approx(0.338)
