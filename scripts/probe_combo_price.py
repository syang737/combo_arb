"""Read-only probe: can a combo be priced WITHOUT creating an RFQ?

Takes the first open combo RFQ, then dumps whatever price-bearing endpoints exist
for its specific combo market and its collection. Every call here is a GET — this
creates nothing and trades nothing.

Run from the repo root:  python scripts/probe_combo_price.py
"""

from __future__ import annotations

import json

from combo_arb.config import AppConfig
from combo_arb.kalshi.client import KalshiClient


def _try(label: str, fn) -> None:
    print(f"\n=== {label} ===")
    try:
        print(json.dumps(fn(), indent=2)[:2500])
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        print(f"ERROR: {exc}")


def main() -> None:
    cfg = AppConfig.load("config/config.example.yaml")
    client = KalshiClient(cfg)

    rfqs = client._get("/communications/rfqs", {"limit": 5}).get("rfqs", [])
    if not rfqs:
        print("No open RFQs returned — nothing to probe.")
        return

    r = rfqs[0]
    market_ticker = r.get("market_ticker")
    collection = r.get("mve_collection_ticker")
    print(f"Probing combo market_ticker = {market_ticker}")
    print(f"        collection         = {collection}")

    # 1) The specific combo market — does it carry yes_bid/yes_ask/last_price?
    _try("GET /markets/{market_ticker}", lambda: client.get_market(market_ticker))
    # 2) Any resting liquidity on the combo?
    _try("GET /markets/{market_ticker}/orderbook", lambda: client.get_orderbook(market_ticker))
    # 3) Collection metadata (may describe a pricing/lookup mechanism).
    _try("GET /multivariate_event_collections/{collection}",
         lambda: client._get(f"/multivariate_event_collections/{collection}"))
    # 4) Collections list (schema discovery).
    _try("GET /multivariate_event_collections",
         lambda: client._get("/multivariate_event_collections", {"limit": 3}))


if __name__ == "__main__":
    main()
