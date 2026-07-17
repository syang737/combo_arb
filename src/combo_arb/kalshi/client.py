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


def _price_field(m: dict, base: str) -> Optional[float]:
    """Read a market price that may be a ``{base}_dollars`` string (e.g. "0.3380")
    or a legacy integer-cents ``{base}`` field. Returns dollars, or None if absent
    or zero (0 = no quote)."""
    dollars = m.get(f"{base}_dollars")
    if dollars is not None:
        try:
            v = float(dollars)
            return v if v > 0 else None
        except (TypeError, ValueError):
            pass
    return _cents_to_dollars(m.get(base))


def _client_error_message(method: str, endpoint: str, resp: "httpx.Response") -> str:
    body = (resp.text or "")[:300]
    msg = f"Kalshi {resp.status_code} on {method} {endpoint}: {body}"
    if resp.status_code == 401:
        msg += (
            " | 401 = authentication rejected. Check that KALSHI_API_KEY_ID matches "
            "the loaded private key, the system clock is in sync, and the key belongs "
            "to this environment (prod vs demo)."
        )
    return msg


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
            except httpx.HTTPError as exc:  # transport/network error -> retry
                last_exc = exc
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                continue

            if resp.status_code == 429:  # rate limited -> back off and retry
                last_exc = RuntimeError("429 rate limited")
                time.sleep(2 ** attempt)
                continue
            if 400 <= resp.status_code < 500:
                # Client errors are not transient; do not retry.
                raise RuntimeError(_client_error_message(method, endpoint, resp))
            if resp.status_code >= 500:  # server error -> retry
                last_exc = RuntimeError(f"{resp.status_code} server error")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                continue
            return resp.json() if resp.content else {}
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
            best_bid=_price_field(m, "yes_bid"),
            best_ask=_price_field(m, "yes_ask"),
            last_trade_price=_price_field(m, "last_price"),
        )

    def get_orderbook(self, ticker: str) -> dict:
        return self._get(f"/markets/{ticker}/orderbook").get("orderbook", {})

    def get_balance(self) -> dict:
        """Authenticated endpoint used to genuinely validate credentials.

        Unlike /markets (public), this fails with 401 if auth is wrong, so it is a
        real auth smoke test.
        """
        return self._get("/portfolio/balance")

    def get_combo_rfqs(self, limit: int = 100, max_pages: int = 10) -> list[ComboRFQ]:
        """Return combo candidates (with prices) from the configured discovery source.

        ``rfq``     -> combos that currently have open RFQs (/communications/rfqs).
        ``markets`` -> enumerate combo markets directly (/markets), covering combos
                       that have no open RFQ. Both return priced ComboRFQ objects.
        """
        if self.cfg.discovery.source == "markets":
            return self._combos_from_markets(limit, max_pages)
        return self._combos_from_rfqs(limit, max_pages)

    def _combos_from_rfqs(self, limit: int, max_pages: int) -> list[ComboRFQ]:
        cap = self.cfg.polling.max_combos_per_scan
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
                if parsed is None:
                    continue
                self._set_combo_quote(parsed)  # price the combo from its own market
                rfqs.append(parsed)
                if len(rfqs) >= cap:
                    return rfqs
            cursor = data.get("cursor")
            if not cursor:
                break
        return rfqs

    def _combos_from_markets(self, limit: int, max_pages: int) -> list[ComboRFQ]:
        """Enumerate combo markets from /markets. Each combo market carries its own
        price and legs, so no extra per-combo fetch is needed."""
        cap = self.cfg.polling.max_combos_per_scan
        buying = self.cfg.strategy.direction != "sell_overpriced"
        series_list = self.cfg.discovery.series_tickers or [None]
        combos: list[ComboRFQ] = []
        for series in series_list:
            cursor: Optional[str] = None
            for _ in range(max_pages):
                params: dict = {"limit": limit, "status": self.cfg.discovery.market_status}
                if series:
                    params["series_ticker"] = series
                if cursor:
                    params["cursor"] = cursor
                try:
                    data = self._get("/markets", params=params)
                except RuntimeError as exc:
                    log.warning("market enumeration failed (%s): %s", series, exc)
                    break
                for m in data.get("markets", []):
                    combo = self._combo_from_market(m, buying)
                    if combo is not None:
                        combos.append(combo)
                        if len(combos) >= cap:
                            return combos
                cursor = data.get("cursor")
                if not cursor:
                    break
        return combos

    @staticmethod
    def _combo_from_market(m: dict, buying: bool) -> Optional[ComboRFQ]:
        """Build a priced ComboRFQ from a combo-market object (has legs + price)."""
        legs_raw = m.get("mve_selected_legs")
        if not legs_raw:
            return None  # not a combo market
        try:
            legs = [
                ComboLeg(
                    leg_ticker=leg.get("market_ticker") or leg.get("ticker"),
                    side=Side(leg.get("side", "yes")),
                    ratio=int(leg.get("ratio", 1)),
                )
                for leg in legs_raw
            ]
        except (ValueError, TypeError):
            return None
        if not legs or not all(leg.leg_ticker for leg in legs):
            return None
        return ComboRFQ(
            rfq_id=str(m.get("ticker")),
            mve_collection_ticker=m.get("mve_collection_ticker", ""),
            market_ticker=m.get("ticker"),
            legs=legs,
            quote_yes=_price_field(m, "yes_ask" if buying else "yes_bid"),
            quote_no=_price_field(m, "no_ask" if buying else "no_bid"),
            size=1,
        )

    def _set_combo_quote(self, rfq: ComboRFQ) -> None:
        """Read the combo's tradeable price from its own market ticker.

        Combos are real markets with a book; the executable price is the ask (when
        buying) or the bid (when selling the combo YES).
        """
        if not rfq.market_ticker:
            return
        try:
            m = self.get_market(rfq.market_ticker)
        except RuntimeError as exc:
            log.debug("combo market %s unavailable: %s", rfq.market_ticker, exc)
            return
        buying = self.cfg.strategy.direction != "sell_overpriced"
        rfq.quote_yes = _price_field(m, "yes_ask" if buying else "yes_bid")
        rfq.quote_no = _price_field(m, "no_ask" if buying else "no_bid")

    @staticmethod
    def _parse_rfq(raw: dict) -> Optional[ComboRFQ]:
        """Parse a /communications/rfqs object.

        NOTE: an RFQ is a *request* and carries no price (only mve_selected_legs,
        target_cost_dollars, status). ``quote_yes`` stays None here; the tradeable
        combo price comes from maker QUOTES (/communications/quotes), which are
        only visible to the RFQ's creator.
        """
        try:
            legs = [
                ComboLeg(
                    leg_ticker=leg.get("market_ticker") or leg.get("ticker"),
                    side=Side(leg.get("side", "yes")),
                    ratio=int(leg.get("ratio", 1)),
                )
                for leg in raw.get("mve_selected_legs", raw.get("legs", []))
            ]
            if not legs or not all(leg.leg_ticker for leg in legs):
                return None
            return ComboRFQ(
                rfq_id=str(raw.get("id") or raw.get("rfq_id")),
                mve_collection_ticker=raw.get("mve_collection_ticker", ""),
                market_ticker=raw.get("market_ticker"),
                legs=legs,
                # None unless a price is present (it is not on the RFQ itself).
                quote_yes=_cents_to_dollars(raw.get("yes_bid") or raw.get("quote_yes")),
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
