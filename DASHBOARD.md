# combo-arb analytics dashboard

A **read-only** browser dashboard for the engine: PnL + equity curve, open trades,
trade history, arb signals, fills, positions, and near-misses. Zero third-party
dependencies (Python stdlib `http.server` + vanilla JS), so it's light enough to run
beside the engine on the 512 MB box.

## Safety model

- **Read-only.** Every query opens the SQLite DB `file:…?mode=ro`; the server is
  GET-only (POST/PUT/DELETE → 405). It cannot write, place orders, or change config.
- **Localhost-only.** The container publishes on `127.0.0.1` and the engine's data
  volume is mounted `:ro`. Nothing is exposed to the public internet — you reach it
  through an SSH tunnel. No firewall port to open, no password to manage.

## What it shows

| Panel | Source (`monitoring/queries.py`) |
|-------|----------------------------------|
| Engine liveness (last write, DB size, row counts) | `db_status` |
| PnL tiles + equity curve (time + equity axes) | `pnl_summary`, `pnl_series` |
| Open-trade tiles (open/settled/expired, oldest) | `open_trades_summary` |
| Open trades — grouped as combo + hedge legs | `trades_grouped(closed=False)` |
| Trade history — grouped, settled + expired (windowed to 3/7/30 days) | `trades_grouped(closed=True, since_ts=...)` |
| Arb signals | `recent_signals` |
| Recent fills | `recent_fills` |
| Positions (flat net exposure per instrument) | `open_positions` |
| Near-misses (closest to an edge) | `top_near_misses` |

The page auto-refreshes every 10s (toggle in the header).

### Trade history window

Trade history grows without bound over time, so it defaults to the **last 3 days**;
switch to 7 or 30 days with the dropdown next to the Trade History header. Open trades
always show in full regardless (they don't have a settlement date to window on, and
there are far fewer of them at any given time).

### Contract names

The engine captures each market's Kalshi display title at scan time (`market_names`
table, populated by the scanner/controller) and the dashboard shows those instead of raw
tickers, with the ticker on hover. Names populate **going forward** — a contract shows its
raw ticker until the engine sees it again after this update is deployed.

### Combo side (always YES)

Kalshi only exposes a directly tradeable YES side for these combo markets — the engine
never buys or sells a combo's NO side (enforced in `risk/risk.py`'s `DeltaHedgeModel`,
which always builds the combo order with `side=YES`; only the *action* — buy vs. sell —
changes with strategy direction). Each trade card now shows the combo's own fill (marked
with a `COMBO` tag) above its hedge legs, so it's visually unambiguous: the combo row
always reads `buy/yes` (or `sell/yes`), while the hedge legs below it are the ones that
short via `buy/no` — that's the delta hedge, not a NO purchase on the combo. The **Recent
fills** table also now shows a `type` column (`combo`/`leg`) for the same reason.

### Trade states (shown as badges)

- **open** — executed (combo YES bought + hedge legs), awaiting *all* legs to *finalize* on Kalshi.
  The PnL shown is the trade-time Monte-Carlo **estimate** (`est.`), net of estimated fees for
  the whole hedged package (combo + every leg). It can be negative even for an executed trade —
  execution is gated on the *combo's* edge exceeding *estimated* fees, not on this full-package
  estimate, so a marginal trade can pencil out slightly negative once every leg's cost is included.
- **settled** — every leg finalized; **realized** PnL is known (shown next to the trade-time
  `est.` for comparison). Each leg shows a **YES/NO** badge for how its underlying actually
  resolved, and the card header shows the combo's own result (**combo YES** = it paid out,
  **combo NO** = it didn't — the combo resolves YES only if *every* leg resolved in its favor).
- **expired** — a leg became permanently un-fetchable (delisted); the trade was force-closed and
  its realized PnL is unknown (no outcome badges — the resolutions were never confirmed).

### Market result vs. WON/LOST

Each settled leg shows **two** distinct indicators, deliberately kept separate:

- `mkt yes` / `mkt no` (small, muted) — the raw fact of how the **underlying market**
  resolved. This is Kalshi's result, nothing more.
- **WON** / **LOST** (colored pill) — whether **this specific position** actually paid
  out. These are *not* the same thing: hedge legs can be held on either side (a leg's
  `buy/yes` vs `buy/no` depends on the sign of its hedge delta), so a market resolving
  YES means a `buy/yes` position **won** but a `buy/no` position on that same market
  **lost**. Always read the colored WON/LOST badge for profitability — the small `mkt`
  tag is just the underlying fact, shown for anyone who wants to verify the math.

### Fees (`fee $X.XX` per leg, `fees $X.XX` per trade)

Each leg/combo row and the trade card header now show fees. The qualifier next to the
trade total tells you where the number came from:

- **`estimated (paper)`** — paper mode never talks to Kalshi's fee schedule; the number
  is `pricing/fees.py`'s formula (`ceil(0.07·p·(1−p)·qty·100)/100` taker, 25% for maker).
- **`actual`** — live mode reconciles the *real* fee Kalshi charged from
  `/portfolio/fills` (`execution/live.py`'s `_reconcile`), falling back to the formula
  only if Kalshi's response omits a fee field. No code change is needed to get real fees
  in production — the live execution path already captures them; this just surfaces
  what was already being recorded.

### Fixing historical realized PnL (`combo-arb backfill-pnl`)

An earlier bug (fixed) made `get_trade_fills` misclassify the combo fill by comparing
tickers, which always failed for real Kalshi MVE combos — so every settled trade's
realized PnL was wrongly recorded as `$0.00` regardless of the true outcome. Trades that
settle after the fix compute correctly on their own, but already-settled rows keep the
wrong number until backfilled. Run this **once**, with the engine container stopped
(nothing else should write to the DB while it runs):

```bash
docker stop combo-arb   # avoid concurrent writes during the backfill
docker run --rm -v ~/combo_arb/data:/data ghcr.io/syang737/combo_arb:latest \
  combo-arb backfill-pnl --db /data/combo_arb.db
docker start combo-arb
```

It recomputes each settled trade's realized PnL from its stored leg outcomes (already
persisted — no Kalshi API calls needed) and **rebuilds the entire PnL event log**, so the
equity curve and PnL tiles reflect the corrections too, not just the Trade History cards.
Idempotent — safe to re-run.

## Run it on the instance (second container)

```bash
cd ~/combo_arb && git pull origin main
./scripts/dashboard.sh
```

This pulls the same image, starts a `combo-arb-dashboard` container that mounts
`~/combo_arb/data` read-only and serves on `127.0.0.1:8080`. It runs independently of
the engine (`--restart unless-stopped`), so restarting one never touches the other.

## View it from your laptop

```bash
ssh -L 8080:localhost:8080 <user>@<instance-ip>
# then open http://localhost:8080 in your browser
```

The tunnel forwards your local `localhost:8080` to the instance's `localhost:8080`,
where the container is listening. Close the SSH session and the dashboard is
unreachable again.

## Run it locally (dev)

```bash
combo-arb dashboard --db data/combo_arb.db          # http://localhost:8080
combo-arb dashboard --db data/combo_arb.db --port 9000
```

## Notes

- **Concurrency:** the dashboard reads while the engine writes; read-only connections
  use a busy timeout. For a low-traffic dashboard this is fine. If you ever see
  transient "database is locked", enabling WAL on the engine DB smooths it out.
- **Exposing publicly** is intentionally *not* wired up. If you later want that, bind
  `--host 0.0.0.0`, open the Lightsail firewall port, and put it behind auth + TLS
  first — the data (PnL, positions, history) shouldn't ride plain HTTP on the open web.
