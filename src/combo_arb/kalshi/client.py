"""Live Kalshi REST client.

Covers the read endpoints the scanner needs (markets / orderbook) plus a guarded
raw order-entry method used only by the live execution engine. Authentication is
RSA-PSS per :mod:`combo_arb.kalshi.auth`; a simple client-side rate limiter keeps
us under Kalshi's request ceiling.

Endpoint note: single-market data endpoints (``/markets``) are stable and well
documented. The combo/RFQ endpoint path is newer and should be confirmed against
the live API docs for your account — ``get_combo_rfqs`` degrades gracefully
(logs a warning and returns ``[]``) rather than crashing the loop if it 404s.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from combo_arb.config import AppConfig
from combo_arb.kalshi.auth import auth_headers, load_private_key
from combo_arb.kalshi.base import MarketDataClient
from combo_arb.models import ComboLeg, ComboRFQ, LegPrice, Side

log = logging.getLogger(__name__)


def _cents_to_dollars(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        c = float(v)
    except (TypeError, ValueError):
        return None
    if c <= 0:
        return None
    return c / 100.0


class _RateLimiter:
    """Minimum-interval limiter, thread-safe."""

    def __init__(self, max_per_sec: int):
        self._min_interval = 1.0 / max(1, max_per_sec)
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


class KalshiClient(MarketDataClient):
    def __init__(self, cfg: AppConfig, timeout: float = 10.0):
        self.cfg = cfg
        self.base_url = cfg.api_base_url.rstrip("/")
        self._base_path = urlparse(self.base_url).path  # e.g. /trade-api/v2
        self._limiter = _RateLimiter(cfg.polling.max_requests_per_sec)
        self._http = httpx.Client(timeout=timeout)

        self._key_id = cfg.secrets.kalshi_api_key_id
        self._private_key = None
        if cfg.secrets.kalshi_private_key_path:
            self._private_key = load_private_key(cfg.secrets.kalshi_private_key_path)

    # -- low level ---------------------------------------------------------
    def _headers(self, method: str, endpoint: str) -> dict[str, str]:
        if not (self._key_id and self._private_key):
            return {}
        path = f"{self._base_path}{endpoint}"
        return auth_headers(self._key_id, self._private_key, method, path)

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
        retries: int = 3,
    ) -> dict:
        url = f"{self.base_url}{endpoint}"
        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            self._limiter.acquire()
            try:
                resp = self._http.request(
                    method, url, params=params, json=json,
                    headers=self._headers(method, endpoint),
                )
                if resp.status_code == 429:  # rate limited -> back off
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                return resp.json() if resp.content else {}
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"Kalshi request failed: {method} {endpoint}: {last_exc}")

    def _get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        return self._request("GET", endpoint, params=params)

    def _post(self, endpoint: str, json: dict) -> dict:
        return self._request("POST", endpoint, json=json)

    # -- market data -------------------------------------------------------
    def get_markets(self, limit: int = 10, **params: Any) -> list[dict]:
        """Read-only listing; used by the CLI auth smoke test."""
        data = self._get("/markets", params={"limit": limit, **params})
        return data.get("markets", [])

    def get_market(self, ticker: str) -> dict:
        return self._get(f"/markets/{ticker}").get("market", {})

    def get_leg_price(self, ticker: str) -> LegPrice:
        m = self.get_market(ticker)
        return LegPrice(
            leg_ticker=ticker,
            best_bid=_cents_to_dollars(m.get("yes_bid")),
            best_ask=_cents_to_dollars(m.get("yes_ask")),
            last_trade_price=_cents_to_dollars(m.get("last_price")),
        )

    def get_orderbook(self, ticker: str) -> dict:
        return self._get(f"/markets/{ticker}/orderbook").get("orderbook", {})

    def get_combo_rfqs(self, limit: int = 100, max_pages: int = 10) -> list[ComboRFQ]:
        """Fetch open RFQs from GET /communications/rfqs (cursor-paginated).

        Endpoint + response shape (``rfqs`` list, ``cursor``) confirmed against the
        Kalshi REST docs. RFQs are returned for all markets; combo (multivariate
        event) RFQs carry ``mve_collection_ticker`` + ``mve_selected_legs``, which
        :meth:`_parse_rfq` extracts. Non-combo RFQs (no legs) are skipped.
        """
        rfqs: list[ComboRFQ] = []
        cursor: Optional[str] = None
        for _ in range(max_pages):
            params: dict = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            try:
                data = self._get("/communications/rfqs", params=params)
            except RuntimeError as exc:
                log.warning("RFQ fetch failed (%s); returning %d so far", exc, len(rfqs))
                break
            for raw in data.get("rfqs", []):
                parsed = self._parse_rfq(raw)
                if parsed is not None:
                    rfqs.append(parsed)
            cursor = data.get("cursor")
            if not cursor:
                break
        return rfqs

    @staticmethod
    def _parse_rfq(raw: dict) -> Optional[ComboRFQ]:
        try:
            legs = [
                ComboLeg(
                    leg_ticker=leg.get("ticker") or leg.get("market_ticker"),
                    side=Side(leg.get("side", "yes")),
                    ratio=int(leg.get("ratio", 1)),
                )
                for leg in raw.get("mve_selected_legs", raw.get("legs", []))
            ]
            if not legs:
                return None
            return ComboRFQ(
                rfq_id=str(raw.get("rfq_id") or raw.get("id")),
                mve_collection_ticker=raw.get("mve_collection_ticker", ""),
                legs=legs,
                quote_yes=_cents_to_dollars(raw.get("yes_bid") or raw.get("quote_yes")) or 0.0,
                quote_no=_cents_to_dollars(raw.get("no_bid") or raw.get("quote_no")),
                size=int(raw.get("size", 1)),
            )
        except (ValueError, TypeError) as exc:  # pragma: no cover - defensive
            log.warning("could not parse RFQ %s: %s", raw, exc)
            return None

    # -- order entry (live only; called by LiveExecutionEngine) ------------
    def create_order(self, order: dict) -> dict:
        """Raw order entry. The live engine is responsible for the safety guards."""
        return self._post("/portfolio/orders", json=order)

    def close(self) -> None:
        self._http.close()
