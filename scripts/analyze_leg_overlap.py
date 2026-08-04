"""Read-only: do the combos we actually see ever contain related legs?

This is the cheap gating check for both event-structure strategies. It uses ONLY
data the engine has already persisted — no API calls, no writes — so it is safe to
run against a live DB (or, preferably, a copy) while the engine keeps trading.

Three questions, cheapest first:

1. **Shape.** Distribution of legs-per-combo from ``combo_rfqs.legs_json``.
2. **Same-event legs.** Kalshi tickers are conventionally ``SERIES-EVENT-OUTCOME``
   (e.g. ``KXMLBGAME-26JUL281840BALDET-DET``), so the first two dash-segments are a
   decent offline proxy for the event key we don't persist. If no combo ever has two
   legs sharing an event, redundant-leg detection has nothing to act on.
   NOTE: this is a *heuristic*; ``scripts/probe_event_structure.py`` confirms it
   against the real ``event_ticker`` field.
3. **Price-series equivalence.** For leg pairs that co-occur in a combo, correlate
   their ``implied_prob`` series from ``market_snapshots`` (already written every
   scan). Two legs that track at ~1.0 correlation with ~zero level offset are
   effectively the same contract; a steady offset suggests nesting. This detector
   needs no new API fields at all.

Run:  python scripts/analyze_leg_overlap.py [--db path/to/combo_arb.db]
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from typing import Optional

from combo_arb.monitoring.queries import _connect_ro, resolve_db_path

# Snapshot timestamps for two legs in the same scan are close but not identical;
# bucket them so the two series can be aligned before correlating.
_BUCKET_SECONDS = 60.0
# Below this many aligned points a correlation is noise, not evidence.
_MIN_POINTS = 20


def _event_key(ticker: str) -> str:
    """``SERIES-EVENT-OUTCOME`` -> ``SERIES-EVENT``. Falls back to the whole ticker."""
    parts = ticker.split("-")
    return "-".join(parts[:2]) if len(parts) >= 3 else ticker


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """Pearson correlation, pure stdlib. None if either series is constant (in which
    case correlation is undefined, not 1.0 — an important distinction here, since
    flat markets would otherwise look like perfect matches)."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def _combo_leg_sets(conn) -> list[list[str]]:
    """Every combo's leg-ticker list, from whichever tables have been populated."""
    out: list[list[str]] = []
    for table, col in (("combo_rfqs", "legs_json"), ("open_trades", "legs_json")):
        try:
            rows = conn.execute(f"SELECT {col} FROM {table}").fetchall()
        except Exception:  # noqa: BLE001 - table may not exist in an older DB
            continue
        for r in rows:
            try:
                legs = json.loads(r[col] or "[]")
            except (TypeError, ValueError):
                continue
            tickers = [leg.get("leg_ticker") for leg in legs if leg.get("leg_ticker")]
            if tickers:
                out.append(tickers)
    return out


def _snapshot_series(conn, tickers: set[str]) -> dict[str, dict[int, float]]:
    """{leg_ticker: {time_bucket: mean implied_prob}} for the given legs."""
    if not tickers:
        return {}
    marks = ",".join("?" * len(tickers))
    rows = conn.execute(
        f"SELECT leg_ticker, ts, implied_prob FROM market_snapshots "
        f"WHERE leg_ticker IN ({marks}) AND implied_prob IS NOT NULL",
        tuple(tickers),
    ).fetchall()
    acc: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        bucket = int(float(r["ts"]) // _BUCKET_SECONDS)
        acc[r["leg_ticker"]][bucket].append(float(r["implied_prob"]))
    return {
        t: {b: sum(v) / len(v) for b, v in buckets.items()}
        for t, buckets in acc.items()
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="Path to combo_arb.db")
    ap.add_argument("--top", type=int, default=25, help="Max leg pairs to print")
    args = ap.parse_args()

    path = resolve_db_path(args.db)
    conn = _connect_ro(path)
    if conn is None:
        print(f"database not found at {path}")
        return
    print(f"Reading (read-only) {path}\n")

    try:
        combos = _combo_leg_sets(conn)
        if not combos:
            print("No combos found in combo_rfqs / open_trades — nothing to analyse.")
            return

        # -- 1. shape ----------------------------------------------------------
        counts = Counter(len(c) for c in combos)
        print(f"=== combo shapes ({len(combos)} combo rows) ===")
        for n in sorted(counts):
            print(f"  {n} legs: {counts[n]}")

        # -- 2. same-event legs (the gating number) ---------------------------
        shared_total = 0
        shared_examples: list[tuple[str, list[str]]] = []
        for legs in combos:
            groups: dict[str, list[str]] = defaultdict(list)
            for t in legs:
                groups[_event_key(t)].append(t)
            shared = {ev: ts for ev, ts in groups.items() if len(ts) > 1}
            if shared:
                shared_total += 1
                if len(shared_examples) < 10:
                    ev, ts = next(iter(shared.items()))
                    shared_examples.append((ev, ts))
        pct = 100.0 * shared_total / len(combos)
        print(f"\n=== same-event legs (ticker-prefix heuristic) ===")
        print(f"  {shared_total}/{len(combos)} combos ({pct:.1f}%) have 2+ legs "
              f"sharing an event key")
        for ev, ts in shared_examples:
            print(f"    {ev}: {', '.join(ts)}")
        if shared_total == 0:
            print("  => GO/NO-GO: no same-event legs. Redundant-leg detection has "
                  "nothing to act on for this combo universe.")

        # -- 3. price-series equivalence --------------------------------------
        pairs: set[tuple[str, str]] = set()
        for legs in combos:
            uniq = sorted(set(legs))
            for i in range(len(uniq)):
                for j in range(i + 1, len(uniq)):
                    pairs.add((uniq[i], uniq[j]))
        series = _snapshot_series(conn, {t for p in pairs for t in p})

        results = []
        for a, b in pairs:
            sa, sb = series.get(a), series.get(b)
            if not sa or not sb:
                continue
            common = sorted(set(sa) & set(sb))
            if len(common) < _MIN_POINTS:
                continue
            xs = [sa[k] for k in common]
            ys = [sb[k] for k in common]
            r = _pearson(xs, ys)
            if r is None:
                continue
            offset = sum(x - y for x, y in zip(xs, ys)) / len(xs)
            results.append((r, offset, len(common), a, b))

        print(f"\n=== price-series equivalence ({len(results)} pairs with "
              f">={_MIN_POINTS} aligned points) ===")
        if not results:
            print("  Not enough overlapping snapshot history to judge. "
                  "(Legs must be snapshotted repeatedly over time.)")
        else:
            results.sort(key=lambda t: -abs(t[0]))
            print(f"  {'corr':>7} {'offset':>8} {'pts':>5}  legs")
            for r, off, n, a, b in results[:args.top]:
                note = ""
                if r > 0.98 and abs(off) < 0.02:
                    note = "  <-- near-identical (redundant?)"
                elif r > 0.98:
                    note = "  <-- tracks closely, level offset (nested?)"
                print(f"  {r:>7.3f} {off:>+8.3f} {n:>5}  {a} | {b}{note}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
