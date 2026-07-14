"""Execution engine interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from combo_arb.models import Fill, LegPrice, Order


class ExecutionEngine(ABC):
    @abstractmethod
    def execute(self, orders: list[Order], leg_prices: dict[str, LegPrice]) -> list[Fill]:
        """Submit orders and return the resulting fills."""
