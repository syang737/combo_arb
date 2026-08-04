"""Read-only: does mutually-exclusive-set arbitrage clear Kalshi's fees?

For a set of mutually exclusive AND exhaustive (MECE) markets, exactly one resolves
YES, so ``sum(p_i) = 1``. That gives a genuinely riskless trade — no hedge ratio, no
correlation estimate, none of the negative convexity that plagues the combo strategy:

    underround (sum of asks < 1): buy YES on all -> pay sum(ask), collect exactly $1
    overround  (sum of bids > 1): sell YES on all -> collect sum(bid), pay exactly $1

The catch is fees. With ``sum(p) = 1``, the total taker fee is

    0.07 * sum(p_i * (1 - p_i)) = 0.07 * (1 - sum(p_i^2))

so the required edge is driven entirely by **skew**:

    balanced (10 outcomes @ 0.10): sum(p^2)=0.10 -> fee 6.3c   (needs a 6.3c underround)
    skewed   (favourite @ 0.97):   sum(p^2)~0.94 -> fee 0.4c   (very tradeable)

Lopsided events are therefore the hunting ground — and their tail markets are also the
illiquid, stale ones. This script measures that directly: it ranks candidate sets by
edge NET of real fees, so we find out whether the trade exists here at all.

**Only structurally provable sets are marked tradeable.** A set that merely looks
exhaustive can leave every leg resolving NO, which loses the entire stake — so
"probably MECE" is treated as untradeable by design.

Every call is a GET. Run:  python scripts/scan_mece_candidates.py [--qty 100]
"""

from __future__ import annotations

import argparse
import math
from collections import defaultdict
from typing import Any, Optional

from combo_arb.config import AppConfig
from combo_arb.kalshi.client import KalshiClient, _price_field
from combo_arb.models import LegPrice
from combo_arb.pricing.fees import taker_fee
from combo_arb.pricing.model import implied_prob


def _strike_bounds(m: dict) -> tuple[float, float]:
    """(floor, cap) for a market's outcome interval, using -/+inf for open ends."""
    def _num(*keys: str) -> Optional[float]:
        for k in keys:
            v = m.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    floor = _num("floor_strike", "floor_strike_dollars")
    cap = _num("cap_strike", "cap_strike_dollars")
    return (floor if floor is not None else -math.inf,
            cap if cap is not None else math.inf)


def _prove_mece_by_strikes(markets: list[dict]) -> tuple[bool, str]:
    """Prove exhaustiveness from strike geometry: the outcome intervals must tile the
    whole line with no gap and no overlap. This is the only proof we accept without an
    explicit Kalshi flag — and it doubles as a guard against an event that mixes several
    unrelated market families (those never tile)."""
    if len(markets) < 2:
        return False, "fewer than 2 markets"
    bounds = sorted((_strike_bounds(m) for m in markets), key=lambda b: b[0])
    if not any(b[0] == -math.inf for b in bounds):
        return False, "no market covers the lower tail (-inf)"
    if not any(b[1] == math.inf for b in bounds):
        return False, "no market covers the upper tail (+inf)"
    for (f1, c1), (f2, c2) in zip(bounds, bounds[1:]):
        if c1 != f2:
            gap = "gap" if c1 < f2 else "overlap"
            return False, f"{gap} between {c1} and {f2}"
    return True, "intervals tile the line with no gap/overlap"


def _mece_flag(event: dict) -> Optional[bool]:
    """An explicit Kalshi mutual-exclusivity flag, if the API exposes one. Field name
    is unconfirmed -- probe_event_structure.py checks which (if any) actually exist."""
    for key in ("mutually_exclusive", "is_mutually_exclusive", "exclusive"):
        v = event.get(key)
        if isinstance(v, bool):
            return v
    return None


def _events_with_markets(client: KalshiClient, cfg: AppConfig, limit: int,
                         max_pages: int) -> list[tuple[dict, list[dict]]]:
    """Events paired with their market sets. Prefers the nested-markets listing; falls
    back to grouping /markets by event_ticker if that shape isn't available."""
    out: list[tuple[dict, list[dict]]] = []
    cursor: Optional[str] = None
    for _ in range(max_pages):
        params: dict[str, Any] = {"limit": limit, "with_nested_markets": "true",
                                  "status": cfg.discovery.market_status}
        if cursor:
            params["cursor"] = cursor
        try:
            data = client._get("/events", params=params)
        except Exception as exc:  # noqa: BLE001
            print(f"  /events listing unavailable ({exc}); falling back to /markets")
            return _group_markets_by_event(client, cfg, limit, max_pages)
        events = data.get("events", [])
        if not events:
            break
        for ev in events:
            markets = ev.get("markets") or []
            if len(markets) >= 2:
                out.append((ev, markets))
        cursor = data.get("cursor")
        if not cursor:
            break
    if not out:
        return _group_markets_by_event(client, cfg, limit, max_pages)
    return out


def _group_markets_by_event(client: KalshiClient, cfg: AppConfig, limit: int,
                            max_pages: int) -> list[tuple[dict, list[dict]]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    cursor: Optional[str] = None
    for _ in range(max_pages):
        params: dict[str, Any] = {"limit": limit, "status": cfg.discovery.market_status}
        if cursor:
            params["cursor"] = cursor
        try:
            data = client._get("/markets", params=params)
        except Exception as exc:  # noqa: BLE001
            print(f"  /markets enumeration failed: {exc}")
            break
        for m in data.get("markets", []):
            ev = m.get("event_ticker")
            if ev:
                groups[ev].append(m)
        cursor = data.get("cursor")
        if not cursor:
            break
    return [({"event_ticker": ev}, ms) for ev, ms in groups.items() if len(ms) >= 2]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--qty", type=int, default=100,
                    help="Basket size used for the fee calc (cent-rounding means "
                         "small sizes are dominated by the >=1c-per-leg floor)")
    ap.add_argument("--limit", type=int, default=100, help="Page size for listings")
    ap.add_argument("--max-pages", type=int, default=5)
    ap.add_argument("--top", type=int, default=25, help="Rows to print")
    args = ap.parse_args()

    cfg = AppConfig.load("config/config.example.yaml")
    client = KalshiClient(cfg)
    print(f"Scanning events for MECE baskets (qty={args.qty} for fee amortisation)...\n")

    try:
        candidates = _events_with_markets(client, cfg, args.limit, args.max_pages)
        print(f"Found {len(candidates)} event(s) with 2+ markets.\n")

        rows = []
        for ev, markets in candidates:
            probs, asks, bids = [], [], []
            for m in markets:
                lp = LegPrice(
                    leg_ticker=m.get("ticker", "?"),
                    best_bid=_price_field(m, "yes_bid"),
                    best_ask=_price_field(m, "yes_ask"),
                    last_trade_price=_price_field(m, "last_price"),
                )
                p = implied_prob(lp, cfg.pricing)
                if p is None or lp.best_ask is None or lp.best_bid is None:
                    probs = []           # incomplete book -> can't evaluate the basket
                    break
                probs.append(p)
                asks.append(lp.best_ask)
                bids.append(lp.best_bid)
            if not probs:
                continue

            n = len(probs)
            sum_p = sum(probs)
            sum_p2 = sum(p * p for p in probs)
            # Continuous-limit fee per contract, the skew statistic from the docstring.
            fee_rate = cfg.fees.taker_rate * (1.0 - sum_p2)
            # Real fee at the requested size, including per-leg cent rounding.
            fee_buy = sum(taker_fee(a, args.qty, cfg.fees) for a in asks) / args.qty
            fee_sell = sum(taker_fee(b, args.qty, cfg.fees) for b in bids) / args.qty

            proven, reason = _prove_mece_by_strikes(markets)
            flag = _mece_flag(ev)
            if flag is True:
                proven, reason = True, "explicit Kalshi mutual-exclusivity flag"
            elif flag is False:
                proven, reason = False, "Kalshi flag says NOT mutually exclusive"

            rows.append({
                "event": ev.get("event_ticker", "?"),
                "n": n, "sum_p": sum_p, "sum_p2": sum_p2, "fee_rate": fee_rate,
                "under_net": 1.0 - sum(asks) - fee_buy,
                "over_net": sum(bids) - 1.0 - fee_sell,
                "proven": proven, "reason": reason,
            })

        if not rows:
            print("No evaluable baskets (every candidate had an incomplete book).")
            return

        rows.sort(key=lambda r: -max(r["under_net"], r["over_net"]))
        print(f"{'event':<34} {'N':>3} {'sum_p':>7} {'sum_p2':>7} {'fee':>7} "
              f"{'under':>8} {'over':>8}  MECE")
        for r in rows[:args.top]:
            best = max(r["under_net"], r["over_net"])
            mark = "TRADEABLE" if (r["proven"] and best > 0) else \
                   ("proven" if r["proven"] else "unproven")
            print(f"{r['event'][:34]:<34} {r['n']:>3} {r['sum_p']:>7.3f} "
                  f"{r['sum_p2']:>7.3f} {r['fee_rate']:>7.4f} "
                  f"{r['under_net']:>+8.4f} {r['over_net']:>+8.4f}  {mark}")

        tradeable = [r for r in rows if r["proven"] and max(r["under_net"], r["over_net"]) > 0]
        proven = [r for r in rows if r["proven"]]
        print(f"\n  {len(proven)}/{len(rows)} baskets structurally provable MECE; "
              f"{len(tradeable)} of those show a POSITIVE edge net of fees at qty={args.qty}.")
        if not tradeable:
            print("  => No riskless basket clears fees right now. Re-run at other times "
                  "(and at larger --qty) before concluding the trade doesn't exist.")
        for r in tradeable[:5]:
            print(f"     {r['event']}: {r['reason']}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
