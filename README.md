# combo_arb — Kalshi Combo Arbitrage Engine

Detects arbitrage between **Kalshi combo quotes** (multivariate-event / parlay
markets) and the **joint probability implied by the underlying legs**, and
executes **hedged** trades in **paper mode**. Live order entry is scaffolded but
**disabled by default** behind hard guards.

## What it does

For each combo (multivariate event, "MVE") quote, the engine computes a fair
value as the **product of the legs' marginal probabilities** (binary settlement:
a combo pays $1 iff *all* legs resolve YES) and flags the combo when its YES
quote is overpriced relative to that fair value by more than fees + a safety
buffer:

```
fair_combo        = Π(leg probability)  · correlation_factor
arbitrage_margin  = combo_quote_yes − fair_combo − fees_estimate
flag when         combo_quote_yes > fair_combo + (fees_estimate + buffer)
```

Flagged signals are sized under risk limits, hedged through the legs, executed
(simulated in paper mode), and logged to SQLite with a Monte-Carlo settlement
PnL estimate.

### Honest framing of the "arbitrage"

A combo's payoff is the **product (AND)** of its legs — a nonlinear function — so
**no static portfolio of single-leg binaries replicates it exactly**. "Hedge via
legs" is therefore a **first-order delta hedge** (hold `∂combo/∂pᵢ` units of leg
`i`), not a riskless lock. Residual convexity / correlation risk remains and is
why we require an edge above fees **and** a buffer before trading. This is a
positive-expected-value strategy with real variance, not free money — validate
independence and settlement assumptions before deploying capital.

## Architecture

| Module | Responsibility |
| --- | --- |
| `kalshi/` | RSA-PSS auth, REST client (markets/orderbook/RFQs, guarded order entry), offline mock client |
| `pricing/` | Kalshi fee formula (`fees.py`) and fair-combo model with correlation + settlement hooks (`model.py`) |
| `scanner/` | Poll combos + legs, compute fair value, flag overpriced combo YES |
| `risk/` | Position/exposure limits, kill switch, sizing, delta-hedge model |
| `execution/` | Paper engine + fill simulator, guarded live engine, settlement PnL |
| `persistence/` | SQLite schema + writers (snapshots, RFQs, signals, orders, fills, positions, PnL, latency) |
| `orchestration/` | Controller loop wiring scanner → risk → execution |
| `backtest/` | Replay recorded frames → return / Sharpe / win-rate / fill-rate |
| `cli.py` | `scan`, `run`, `replay`, `gen-sample`, `markets` |

## Install

```bash
uv venv --python 3.11 .venv && . .venv/bin/activate
uv pip install -e ".[dev]"        # or: pip install -e ".[dev]"
```

## Quickstart (no credentials needed)

```bash
combo-arb gen-sample --out data/sample/frames.jsonl --n 200   # synthetic data
combo-arb scan                        # one-shot scan against the mock market
combo-arb run --iterations 5          # paper loop; writes data/combo_arb.db
combo-arb replay data/sample/frames.jsonl   # backtest + metrics
```

## Configuration

Copy `config/config.example.yaml` → `config/config.yaml` (git-ignored) and edit,
or point `COMBO_ARB_CONFIG` at a file. Key knobs: `pricing.prob_source`
(`mid`/`last`), `pricing.correlation_factor`, `pricing.settlement_model`,
`thresholds.*`, `risk.*` (limits + `kill_switch`), `execution.fill_model`,
`persistence.db_path`.

Secrets come **only** from the environment / `.env` (never the YAML):

```bash
cp .env.example .env      # then fill in:
# KALSHI_API_KEY_ID=...
# KALSHI_PRIVATE_KEY_PATH=/abs/path/to/kalshi_private_key.pem
```

### Live market data (read-only)

With credentials set, confirm auth against the live API without placing anything:

```bash
combo-arb markets         # RSA-PSS signed, read-only listing
combo-arb scan --source live
```

### Live trading is triple-guarded (off by default)

A real order is sent only when **all three** hold:
`execution.live_enabled: true` **and** `mode: live` **and** env
`CONFIRM_LIVE_TRADING=YES`. Otherwise the live engine refuses and raises.

## Kalshi API notes

- Combos are **multivariate events** traded via **RFQ**; an RFQ references
  `mve_collection_ticker` + `mve_selected_legs`. Single-market data endpoints
  (`/markets`) are stable; the combo/RFQ endpoint path is newer — `get_combo_rfqs`
  degrades gracefully (warns, returns `[]`) if it 404s. **Confirm the RFQ and
  combo order-entry paths against the live docs for your account** before live use.
- **Fees:** taker = `ceil(0.07·P·(1−P)·qty·100)/100`; maker ≈ 25% of taker.
  Implemented in `pricing/fees.py` (single source of truth). Threshold estimates
  use the *unrounded* marginal rate; actual fills apply the rounded fee.
- **Auth:** RSA-PSS over `{ts}{METHOD}{path}`, headers
  `KALSHI-ACCESS-KEY/SIGNATURE/TIMESTAMP`.

## Testing

```bash
pytest -q                                   # 43 tests
pytest -q --cov=combo_arb --cov-report=term-missing
```

## Out of scope / follow-ups

WebSocket/FIX real-time streaming (the loop currently polls REST); acting as a
real RFQ maker/quoter; empirical correlation & settlement-model calibration from
historical joint outcomes; exact hedge optimization; migration to a time-series
DB; dashboards.
