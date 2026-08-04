# Event-structure strategies — evidence notes

Working notes for two candidate strategies that share one root cause. **Nothing here is
wired into the engine**; these are read-only probes plus their findings, so the build
decision rests on measurements rather than assumptions.

## The root cause

`pricing/model.py::combo_implied_by_legs` computes `fair = ∏ pᵢ` — pure independence. It is
blind to every logical relationship between markets in the same event:

| Relationship | Truth | Independence model says | Consequence |
|---|---|---|---|
| A ⊆ B (redundant leg) | P(A∩B) = P(A) | pₐ·p_b — **too low** | we miss real edge |
| A ∩ B = ∅ (exclusive) | P(A∩B) = **0** | pₐ·p_b > 0 — **too high** | ⚠️ engine could buy a worthless combo |
| MECE set | Σpᵢ = **1** | not modelled at all | dutch-book arb invisible |

The middle row is a latent risk in the engine *today*, not just a missed opportunity: a combo
whose legs cannot all hit gets a positive `fair`, so `arb_margin = fair − quote − fees` looks
attractive and the engine would buy something worth exactly zero.

## Verified arithmetic

Confirmed against the repo's own `pricing/fees.py` (not hand arithmetic):

**Fee drag on a MECE basket is driven entirely by skew.** With Σpᵢ = 1, total taker fee is
`0.07·Σpᵢ(1−pᵢ) = 0.07·(1 − Σpᵢ²)`:

| set | Σp² | fee/contract |
|---|---|---|
| balanced, 10 @ 0.10 | 0.100 | **6.30¢** |
| balanced, 4 @ 0.25 | 0.250 | 5.25¢ |
| balanced, 2 @ 0.50 | 0.500 | 3.50¢ |
| skewed, 0.90 + 10×0.01 | 0.811 | 1.32¢ |
| skewed, 0.97 + 3×0.01 | 0.941 | **0.41¢** |

So **lopsided events are the hunting ground** — a balanced 10-way set needs a 6.3¢ underround
(essentially never occurs), while a set with a strong favourite needs under half a cent. Their
tail markets are also the illiquid, stale ones most likely to drift.

**Cent-rounding punishes small size.** `taker_fee` rounds up per leg, so a 10-leg basket costs
≥10¢ at qty=1 — a 59% surcharge over the continuous limit:

| qty | fee/contract (10 legs @ 0.10) |
|---|---|
| 1 | 10.00¢ |
| 10 | 7.00¢ |
| 100 | 6.30¢ (= continuous limit) |

**The payoff really is riskless.** Skewed set, Σask = 0.948, qty=100: cost 94.80 + fees 0.99 =
95.79, payout exactly 100.00 whichever leg wins → **+$4.21 in every outcome**. No hedge ratio,
no correlation estimate, none of the negative convexity that forced `max_legs = 3`.

**Redundant legs give a perfect hedge.** If A ⊆ B the combo is economically just A, so the
correct hedge is **1.0 contracts of the binding leg and 0.0 of the redundant one** — zero
variance, unlike the fractional delta the engine computes today. It also yields an *effective
leg count* (materially distinct legs after collapsing implications), which would let a 5-leg
combo with 3 redundant legs legitimately pass the leg cap.

## The probes

| script | needs API? | answers |
|---|---|---|
| `scripts/probe_event_structure.py` | yes (GET only) | Which structural fields exist (`event_ticker`, `strike_type`, `floor_strike`/`cap_strike`, …)? Is there an explicit mutual-exclusivity flag? Do a combo's legs ever share an event? |
| `scripts/analyze_leg_overlap.py` | **no** — existing DB only | Combo shapes; same-event leg frequency (ticker-prefix heuristic); price-series equivalence from `market_snapshots` |
| `scripts/scan_mece_candidates.py` | yes (GET only) | Ranks candidate baskets by edge **net of real fees**; marks only *structurally provable* sets tradeable |

All three are read-only and safe to run beside the live engine.

## Findings so far

- **Detectors verified against synthetic data.** Same-event grouping correctly flagged 2/3
  seeded combos and correctly excluded the cross-event control; price-series equivalence
  flagged the near-identical pair at corr 1.000 / offset −0.004.
- **MECE structural proof passes all 7 edge cases**: proper partition proves; gap, overlap,
  missing lower tail, missing upper tail, single market, and *categorical markets with no
  strike geometry* all correctly refuse to prove. The categorical case failing closed is the
  important one — an unprovable set is never marked tradeable.
- **Local DB is not representative.** The checked-in `data/combo_arb.db` has only 17 combos
  (2–3 legs each) and reports 0% same-event legs, but it is a stale dev snapshot from July and
  has no usable snapshot history. **The gating number must be re-measured against the
  production DB.**

## Open questions — run these on the box

```bash
python scripts/analyze_leg_overlap.py --db /data/combo_arb.db   # go/no-go: same-event leg %
python scripts/probe_event_structure.py                          # confirm real field names
python scripts/scan_mece_candidates.py --qty 100                 # does any basket clear fees?
```

1. Do listed combos ever contain same-event legs? `MULTIGAMEEXTENDED` / `CROSSCATEGORY` are by
   name combos *across* different games/categories, where legs are near-independent — if the
   answer is ~0%, redundant-leg detection is moot for this universe (MECE arb is unaffected;
   it does not involve combos at all).
2. Does Kalshi expose an explicit mutual-exclusivity flag, or must every set be proven from
   strike geometry?
3. Does any provable basket show a positive edge net of fees — and at what size?

## Decisions already made

- **Evidence before building.** No pricing-model, strategy, or execution changes until the
  above is measured.
- **Structural proof only for MECE.** Only sets provable from strike geometry (or an explicit
  Kalshi flag) are tradeable. "Probably exhaustive" is rejected: if a set turns out not to be
  exhaustive, every leg can resolve NO and the entire stake is lost.
