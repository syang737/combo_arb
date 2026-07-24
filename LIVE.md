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

## Known limitations (close before scaling real size)

- **Realized PnL at settlement isn't reconciled** — combos settle days later; the
  `pnl` table currently holds the Monte-Carlo *estimate* + real trade-time cash.
- **No daily-loss circuit breaker** and **no alerting** yet — add these before
  meaningful size.
- **Unwind is best-effort** (IOC at a small slippage). If it can't cross, it logs
  `CRITICAL` and leaves the residual for **manual** intervention — watch the logs.
- **Order schema is demo-confirmed only** until you've traded prod once.
