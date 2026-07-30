"""Controller: the pre-trade expected-PnL gate downgrades a modelled-negative
signal to signal-only BEFORE executing, and a genuinely positive-EV signal still
executes normally."""

from __future__ import annotations

from combo_arb.kalshi.mock_client import MockKalshiClient
from combo_arb.models import SignalAction
from combo_arb.orchestration.controller import Controller
from combo_arb.persistence.db import Database


def test_ev_gate_executes_a_real_positive_edge(cfg, legs, underpriced_rfq):
    """Sanity check for the wiring itself, no monkeypatching: the shared
    underpriced_rfq fixture has a large enough edge (already proven positive-EV in
    test_paper_execution.py) to clear max_legs, max_leg_price, apply_buffer=True,
    AND the new pre-trade EV gate -- so it must still execute end to end."""
    db = Database(":memory:")
    client = MockKalshiClient(leg_prices=legs, rfqs=[underpriced_rfq])
    controller = Controller(cfg, client, db=db)

    result = controller.run_once()

    assert result.executed == 1
    outcome = result.outcomes[0]
    assert outcome.executed is True
    assert outcome.signal.action == SignalAction.HEDGE_VIA_LEGS
    assert db.count_open_trades() == 1


def test_ev_gate_rejects_modelled_negative_signal(cfg, legs, underpriced_rfq, monkeypatch):
    """Force the pre-trade EV estimate negative (independent of the real scenario, so
    this isolates the CONTROLLER's wiring -- does it actually check the gate and skip
    execution -- from whether any particular fixture happens to be EV-negative) and
    confirm nothing gets executed or persisted as an open trade."""
    monkeypatch.setattr(
        "combo_arb.orchestration.controller.expected_pnl_for_orders",
        lambda *a, **k: -1.0,
    )
    db = Database(":memory:")
    client = MockKalshiClient(leg_prices=legs, rfqs=[underpriced_rfq])
    controller = Controller(cfg, client, db=db)

    result = controller.run_once()

    assert result.executed == 0
    outcome = result.outcomes[0]
    assert outcome.executed is False
    assert outcome.signal.action == SignalAction.SIGNAL_ONLY
    assert "expected PnL" in outcome.reason
    assert db.count_open_trades() == 0
