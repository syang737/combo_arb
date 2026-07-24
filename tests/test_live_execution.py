import pytest

from combo_arb.config import AppConfig, Mode
from combo_arb.execution.live import InsufficientBalance, LiveExecutionEngine
from combo_arb.models import InstrumentType, Order, OrderStatus, Side


class FakeKalshiClient:
    """Records orders and returns configurable fills (per-ticker fill ratio)."""

    def __init__(self, balance_cents=1_000_000, fill_ratio=None):
        self.balance_cents = balance_cents
        self.fill_ratio = fill_ratio or {}
        self.orders: dict[str, dict] = {}
        self.created: list[dict] = []
        self._n = 0

    def get_balance(self):
        return {"balance": self.balance_cents}

    def create_order(self, payload):
        self._n += 1
        oid = f"o{self._n}"
        self.orders[oid] = payload
        self.created.append(payload)
        return {"order": {"order_id": oid}}

    def get_fills(self, order_id=None, ticker=None, limit=100):
        p = self.orders.get(order_id)
        if not p:
            return []
        filled = int(round(p["count"] * self.fill_ratio.get(p["ticker"], 1.0)))
        if filled <= 0:
            return []
        key = "yes_price" if p["side"] == "yes" else "no_price"
        return [{"count": filled, key: p.get(key, 50), "fee": 3}]  # fee 3c


def _armed_cfg() -> AppConfig:
    cfg = AppConfig()
    cfg.mode = Mode.LIVE
    cfg.execution.live_enabled = True
    cfg.secrets.confirm_live_trading = "YES"
    cfg.execution.fill_poll_timeout_s = 0.0  # no sleeps in tests
    assert cfg.live_trading_armed()
    return cfg


def _combo():
    return Order(instrument="KXMVE-COMBO", instrument_type=InstrumentType.COMBO,
                 side=Side.YES, action="buy", price=0.30, qty=10)


def _legs():
    return [
        Order(instrument="LEG-A", instrument_type=InstrumentType.LEG, side=Side.NO,
              action="buy", price=0.50, qty=4),
        Order(instrument="LEG-B", instrument_type=InstrumentType.LEG, side=Side.NO,
              action="buy", price=0.60, qty=5),
    ]


def test_places_combo_and_legs_and_reconciles_real_fees():
    client = FakeKalshiClient()
    eng = LiveExecutionEngine(_armed_cfg(), client)
    combo, (legA, legB) = _combo(), _legs()
    fills = eng.execute([combo, legA, legB], leg_prices={})

    tickers = [p["ticker"] for p in client.created]
    assert tickers.count("KXMVE-COMBO") == 1     # combo placed, NOT rejected
    assert len(client.created) == 3               # combo + 2 legs, no unwind
    assert combo.status == OrderStatus.FILLED
    assert all(f.fee > 0 for f in fills)          # real fee reconciled (not the 0 stub)
    assert sum(f.qty for f in fills if f.instrument == "KXMVE-COMBO") == 10
    # combo price sent as cents on the yes side
    combo_payload = next(p for p in client.created if p["ticker"] == "KXMVE-COMBO")
    assert combo_payload["yes_price"] == 30 and "client_order_id" in combo_payload


def test_partial_fill_triggers_unwind():
    client = FakeKalshiClient(fill_ratio={"LEG-B": 0.0})  # leg B doesn't fill
    eng = LiveExecutionEngine(_armed_cfg(), client)
    combo, (legA, legB) = _combo(), _legs()
    eng.execute([combo, legA, legB], leg_prices={})

    sells = [p for p in client.created if p["action"] == "sell"]
    unwound = {p["ticker"] for p in sells}
    assert "KXMVE-COMBO" in unwound and "LEG-A" in unwound  # filled legs flattened
    assert "LEG-B" not in unwound                           # nothing filled -> nothing to unwind


def test_balance_check_blocks_and_places_nothing():
    client = FakeKalshiClient(balance_cents=100)  # $1 available
    eng = LiveExecutionEngine(_armed_cfg(), client)
    big = Order(instrument="KXMVE-COMBO", instrument_type=InstrumentType.COMBO,
                side=Side.YES, action="buy", price=0.30, qty=100)  # ~$30 needed
    with pytest.raises(InsufficientBalance):
        eng.execute([big], leg_prices={})
    assert client.created == []


def test_max_trades_per_run_caps_executions():
    from combo_arb.kalshi.mock_client import MockKalshiClient
    from combo_arb.models import ComboLeg, ComboRFQ, LegPrice
    from combo_arb.orchestration.controller import Controller

    cfg = AppConfig()  # paper mode; cap the run
    cfg.execution.max_trades_per_run = 1
    legs = {"A": LegPrice(leg_ticker="A", best_bid=0.49, best_ask=0.51),
            "B": LegPrice(leg_ticker="B", best_bid=0.39, best_ask=0.41)}
    rfqs = [
        ComboRFQ(rfq_id="1", mve_collection_ticker="C1",
                 legs=[ComboLeg(leg_ticker="A"), ComboLeg(leg_ticker="B")], quote_yes=0.10, size=20),
        ComboRFQ(rfq_id="2", mve_collection_ticker="C2",
                 legs=[ComboLeg(leg_ticker="A"), ComboLeg(leg_ticker="B")], quote_yes=0.10, size=20),
    ]
    ctl = Controller(cfg, MockKalshiClient(leg_prices=legs, rfqs=rfqs))
    res = ctl.run_once()
    assert res.signals == 2 and res.executed == 1  # both flag, only one trades
