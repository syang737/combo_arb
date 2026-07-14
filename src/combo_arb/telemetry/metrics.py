"""Lightweight telemetry: a latency timer context manager and simple counters."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Callable, Iterator, Optional


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


class Metrics:
    """In-memory counters + optional latency sink (e.g. Database.insert_latency)."""

    def __init__(self, latency_sink: Optional[Callable[[str, float], None]] = None):
        self.counters: dict[str, int] = defaultdict(int)
        self._latency_sink = latency_sink

    def incr(self, name: str, by: int = 1) -> None:
        self.counters[name] += by

    @contextmanager
    def timer(self, stage: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            ms = (time.perf_counter() - start) * 1000.0
            if self._latency_sink is not None:
                self._latency_sink(stage, ms)

    def snapshot(self) -> dict[str, int]:
        return dict(self.counters)
