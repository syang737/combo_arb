import pytest

from combo_arb.config import AppConfig, Mode
from combo_arb.execution.live import LiveExecutionEngine, LiveTradingNotArmed


class _FakeClient:
    def __init__(self):
        self.calls = []

    def create_order(self, payload):
        self.calls.append(payload)
        return {"order": {"order_id": "x", "status": "executed", "count": payload.get("count", 1)}}


def _armed_cfg() -> AppConfig:
    cfg = AppConfig()
    cfg.mode = Mode.LIVE
    cfg.execution.live_enabled = True
    cfg.secrets.confirm_live_trading = "YES"
    return cfg


def test_not_armed_by_default_raises():
    cfg = AppConfig()  # paper, live_enabled False, no confirm
    assert cfg.live_trading_armed() is False
    eng = LiveExecutionEngine(cfg, client=_FakeClient())
    with pytest.raises(LiveTradingNotArmed):
        eng.execute([], leg_prices={})


def test_partial_guards_still_blocked():
    cfg = AppConfig()
    cfg.execution.live_enabled = True   # only one of three guards
    assert cfg.live_trading_armed() is False


def test_all_three_guards_arms():
    cfg = _armed_cfg()
    assert cfg.live_trading_armed() is True


def test_armed_sends_leg_order():
    cfg = _armed_cfg()
    client = _FakeClient()
    eng = LiveExecutionEngine(cfg, client=client)
    from combo_arb.models import InstrumentType, Order, Side

    order = Order(
        instrument="A", instrument_type=InstrumentType.LEG, side=Side.YES,
        action="buy", price=0.51, qty=3,
    )
    fills = eng.execute([order], leg_prices={})
    assert len(client.calls) == 1
    assert client.calls[0]["yes_price"] == 51  # dollars -> cents
    assert fills[0].qty == 3
