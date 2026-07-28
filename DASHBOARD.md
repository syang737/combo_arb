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
| PnL tiles + equity curve | `pnl_summary`, `pnl_series` |
| Open-trade tiles (open/settled/expired, oldest) | `open_trades_summary` |
| Open trades awaiting settlement | `open_trades_list` |
| Trade history (settled + expired, realized vs expected) | `recent_trades` |
| Arb signals | `recent_signals` |
| Recent fills | `recent_fills` |
| Positions | `open_positions` |
| Near-misses (closest to an edge) | `top_near_misses` |

The page auto-refreshes every 10s (toggle in the header).

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
