"""Paper execution engine — simulates fills, places no real orders."""

from __future__ import annotations

import logging

from combo_arb.config import AppConfig
from combo_arb.execution.base import ExecutionEngine
from combo_arb.execution.fill_sim import simulate_fill
from combo_arb.models import Fill, LegPrice, Order

log = logging.getLogger(__name__)


class PaperExecutionEngine(ExecutionEngine):
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    def execute(self, orders: list[Order], leg_prices: dict[str, LegPrice]) -> list[Fill]:
        fills: list[Fill] = []
        for order in orders:
            fill = simulate_fill(order, leg_prices, self.cfg)
            log.debug(
                "paper fill %s %s %s x%d @ %.4f fee %.4f",
                order.action, order.side.value, order.instrument, fill.qty, fill.price, fill.fee,
            )
            fills.append(fill)
        return fills
