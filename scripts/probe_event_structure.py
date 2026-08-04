"""Read-only probe: what event/strike structure does Kalshi actually expose?

The engine's fair value is ``product(leg probabilities)`` — pure independence
(``pricing/model.py::combo_implied_by_legs``). That is blind to every logical
relationship between markets in the same event:

    A implies B  -> P(A and B) = P(A)   , model says pA*pB  (too LOW,  missed edge)
    A excludes B -> P(A and B) = 0      , model says pA*pB  (too HIGH, false arb!)
    MECE set     -> sum(p) = 1          , not modelled at all

Acting on any of that needs structural metadata we currently throw away:
``get_leg_price`` (kalshi/client.py) keeps only bid/ask/last/title and discards
``event_ticker``, ``strike_type``, ``floor_strike``/``cap_strike``, ``rules_primary``.

This script confirms which of those fields genuinely exist before any code depends
on them, and whether Kalshi exposes an explicit mutual-exclusivity flag. Every call
is a GET — it creates nothing and trades nothing.

Run from the repo root:  python scripts/probe_event_structure.py
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Optional

from combo_arb.config import AppConfig
from combo_arb.kalshi.client import KalshiClient

# The fields that would let us reason about implication / exclusivity structurally.
# Presence (not value) is what this probe is really measuring.
_STRUCTURE_FIELDS = (
    "event_ticker", "series_ticker", "market_type", "strike_type",
    "floor_strike", "cap_strike", "floor_strike_dollars", "cap_strike_dollars",
    "rules_primary", "rules_secondary", "category", "expiration_time",
)
# Any of these on an EVENT object would give us MECE membership for free, rather
# than having to prove it from strike geometry.
_MECE_HINT_FIELDS = (
    "mutually_exclusive", "mutually_exclusive_markets", "is_mutually_exclusive",
    "exclusive", "collateral_return_type", "price_level_structure",
)


def _try(label: str, fn) -> Optional[Any]:
    """Dump whatever an endpoint returns; never raise (this is a diagnostic)."""
    print(f"\n=== {label} ===")
    try:
        out = fn()
        print(json.dumps(out, indent=2, default=str)[:2500])
        return out
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        print(f"ERROR: {exc}")
        return None


def _report_fields(label: str, obj: dict, fields: tuple[str, ...]) -> None:
    """Print present/absent for the fields we care about, so the answer is one glance
    rather than hunting through a raw dump."""
    print(f"\n--- {label}: structural fields ---")
    for f in fields:
        if f in obj and obj[f] not in (None, ""):
            val = str(obj[f])
            print(f"  [x] {f:28} = {val[:70]}")
        else:
            print(f"  [ ] {f:28}   (absent)")


def main() -> None:
    cfg = AppConfig.load("config/config.example.yaml")
    client = KalshiClient(cfg)

    print("Discovering combos via the engine's own configured source "
          f"(discovery.source={cfg.discovery.source})...")
    try:
        combos = client.get_combo_rfqs(limit=25)
    except Exception as exc:  # noqa: BLE001
        print(f"combo discovery failed: {exc}")
        combos = []
    if not combos:
        print("No combos returned — cannot probe leg structure. "
              "Check discovery.source / series_tickers in the config.")
        return

    print(f"Got {len(combos)} combo(s). Probing the first few and their legs.\n")

    # 1) One raw leg market, in full: the ground truth for what fields exist.
    first_leg = combos[0].legs[0].leg_ticker
    raw_leg = _try(f"GET /markets/{first_leg}  (raw leg market)",
                   lambda: client.get_market(first_leg))
    if isinstance(raw_leg, dict) and raw_leg:
        _report_fields(f"leg {first_leg}", raw_leg, _STRUCTURE_FIELDS)

    # 2) THE GATING QUESTION: within a single combo, do any two legs share an event?
    #    If legs are always from different events (different games), then implication
    #    and exclusivity cannot arise and both strategies are moot for this universe.
    print("\n\n=== same-event legs WITHIN a combo (the gating question) ===")
    combos_with_shared_event = 0
    combos_checked = 0
    for combo in combos[:8]:                       # keep the request count modest
        event_of: dict[str, Optional[str]] = {}
        for leg in combo.legs:
            try:
                m = client.get_market(leg.leg_ticker)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {leg.leg_ticker}: {exc}")
                continue
            event_of[leg.leg_ticker] = m.get("event_ticker")
        if not event_of:
            continue
        combos_checked += 1
        groups: dict[str, list[str]] = defaultdict(list)
        for ticker, ev in event_of.items():
            groups[ev or "(no event_ticker)"].append(ticker)
        shared = {ev: ts for ev, ts in groups.items() if len(ts) > 1}
        flag = "  <-- SHARED EVENT" if shared else ""
        print(f"\n  combo {combo.mve_collection_ticker} "
              f"({len(combo.legs)} legs, {len(groups)} distinct events){flag}")
        for ev, tickers in groups.items():
            print(f"     {ev}: {', '.join(tickers)}")
        if shared:
            combos_with_shared_event += 1

    print(f"\n  => {combos_with_shared_event}/{combos_checked} probed combos have "
          f"two or more legs in the SAME event.")
    if combos_with_shared_event == 0:
        print("  => No same-event legs found. Redundant-leg detection has nothing to "
              "act on in this universe (MECE set arb may still be viable — it does "
              "not depend on combos at all).")

    # 3) The event behind a leg: does it enumerate sibling markets, and does it say
    #    anywhere that they are mutually exclusive? This is what MECE arb needs.
    event_ticker = (raw_leg or {}).get("event_ticker")
    if event_ticker:
        ev = _try(f"GET /events/{event_ticker}", lambda: client._get(f"/events/{event_ticker}"))
        if isinstance(ev, dict):
            body = ev.get("event", ev)
            if isinstance(body, dict):
                _report_fields(f"event {event_ticker}", body, _MECE_HINT_FIELDS)
        _try(f"GET /events/{event_ticker}?with_nested_markets=true  (sibling market set)",
             lambda: client._get(f"/events/{event_ticker}",
                                 params={"with_nested_markets": "true"}))
    else:
        print("\n(no event_ticker on the leg market -> cannot probe the event endpoint; "
              "MECE grouping would need a different key)")

    # 4) Schema discovery on the events listing (are the MECE hint fields ever set?).
    _try("GET /events?limit=3  (schema discovery)",
         lambda: client._get("/events", params={"limit": 3}))

    client.close()
    print("\nDone. Record the findings in NOTES-event-structure.md.")


if __name__ == "__main__":
    main()
