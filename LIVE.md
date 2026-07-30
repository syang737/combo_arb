# Going live — hedged combo trading (staged runbook)

> ⚠️ **Real money.** Live mode places real orders on Kalshi. Work through this in
> order; do **not** skip the demo stage. The strategy is a *delta hedge*, not a
> riskless arb — and it rarely clears the full round-trip fee cost, so expect it to
> trade seldom.

## The triple guard

A real order is sent only when **all three** hold:

```yaml
# config.yaml
mode: live
execution:
  live_enabled: true
```
```bash
export CONFIRM_LIVE_TRADING=YES
```

Miss any one and `LiveExecutionEngine.execute` refuses. The safety buffer is also
**auto-forced on** whenever live is armed (regardless of `thresholds.apply_buffer`).

## What happens on a trade

Per flagged signal, the engine: pre-checks account balance → places the **combo +
each hedge leg** as IOC limit orders (fill-now-or-cancel) → reconciles **real
fills/fees** from `/portfolio/fills`. If the set only *partially* fills, it
**unwinds** the filled remainder back to flat so you're never left naked. All of it
is logged and written to `combo_arb.db` (`orders`, `fills`, `positions`, `pnl`).

## Stage 1 — Confirm the order schema on DEMO (do this first)

The `/portfolio/orders` price field/units for these deci-cent MVE markets are
**unconfirmed** — `_to_kalshi_order` (execution/live.py) currently sends integer
cents. Validate before real money:

```yaml
mode: live
environment: demo          # confirm the demo host in config.py API_BASE_URLS first
execution:
  live_enabled: true
  max_trades_per_run: 1     # place exactly ONE trade then stop
risk:
  capital_per_trade: 5.0    # tiny
  max_contracts_per_trade: 2
```
```bash
export CONFIRM_LIVE_TRADING=YES
combo-arb markets            # confirm auth on demo
combo-arb run --source live --iterations 1 --log-level INFO
```
Inspect the order/fill responses in the logs and DB. If demo **rejects the cent
price** (deci-cent tick), adjust `_to_kalshi_order` (e.g. a `*_price_dollars` field
or finer units) and re-run. Also force a partial (e.g. an unfillable leg limit) to
watch the **unwind** fire.

## Stage 2 — Tiny real money

Only once demo places + reconciles + unwinds correctly:

```yaml
mode: live
environment: prod
execution:
  live_enabled: true
  max_trades_per_run: 1     # keep low at first
risk:
  capital_per_trade: 10.0
  max_contracts_per_trade: 2
  max_total_exposure: 50.0
  kill_switch: false        # flip to true (+ restart) to halt instantly
```
Run it, confirm a real hedged trade + fills, then flatten and review before raising
caps. **Keep the kill switch one edit away.**

## Stage 3 — Scale

Raise `capital_per_trade` / `max_*` / `max_trades_per_run` gradually, watching PnL
and fills between steps.

## Kill switch

Set `risk.kill_switch: true` in config and `docker restart combo-arb` (or just
`docker stop combo-arb`). With the switch on, `RiskManager.evaluate` blocks every
new trade.

## Why the bot trades less now (negative convexity)

Measured against the repo's own settlement math: a combo pays only if **every** leg
hits, so a linear delta hedge against that nonlinear (AND) payoff has a specific weak
spot — "all but one leg hits" is usually the **worst** outcome, and often the
**second most likely** one. Hedge quality also degrades with leg count (each leg's
hedge ratio is the product of the *other* legs' probabilities, which shrinks fast):
hedging is +EV at 2 legs, roughly breakeven at 3, and EV-negative at 5+ (the fees on
N hedge legs exceed the protection they buy). Three guards now enforce this:

- `risk.max_legs` (default 3) — combos with more legs are signal-only.
- `risk.max_leg_price` (default 0.95) — a near-certain hedge leg buys almost no
  protection but still costs a fee.
- `thresholds.min_expected_pnl` (default 0.0) — the pre-trade Monte-Carlo full-package
  expected PnL (real prices, real fees, the AND-rule) must clear this, checked
  **before** executing, not just estimated after. Works together with
  `thresholds.apply_buffer` (now on by default) which requires the scanner's own edge
  to clear fees by a margin, not just by a fraction of a cent.

Expect a meaningfully lower trade frequency — that's the point; the filtered trades
are the ones that were losing.

## Known limitations (close before scaling real size)

- **Realized PnL at settlement isn't reconciled** — combos settle days later; the
  `pnl` table currently holds the Monte-Carlo *estimate* + real trade-time cash.
- **No daily-loss circuit breaker** and **no alerting** yet — add these before
  meaningful size.
- **Unwind is best-effort** (IOC at a small slippage). If it can't cross, it logs
  `CRITICAL` and leaves the residual for **manual** intervention — watch the logs.
- **Order schema is demo-confirmed only** until you've traded prod once.
