# Deploying combo-arb to the cloud

The engine is a **long-running loop** (`run --iterations 0`), not a web service.
So you want an **always-on container host** with a small persistent disk — *not*
a request-driven serverless platform (Cloud Run / Lambda idle-stop and would kill
the loop). Below: containerize once, then pick a host.

## 0. Two rules before anything

1. **Keep it in PAPER mode.** The default `CMD` runs `--source live` (real data)
   in paper mode (simulated fills). Live order entry stays triple-guarded and off.
   So even if the box is compromised, it cannot place real trades.
2. **Secrets never go in the image.** `.dockerignore` excludes `.env` and `*.pem`.
   Provide the API key id as an env var and the private key as a **mounted file**
   at runtime.

## 1. Build the image

```bash
docker build -t combo-arb:latest .
```

## 2. Run it locally (smoke test)

Provide credentials at runtime, mount the private key read-only, and mount a
volume for the SQLite DB. Point `persistence.db_path` at `/data` in your config.

```bash
docker run --rm \
  -e KALSHI_API_KEY_ID=fca0b293-06f5-410e-b81a-fb21f198ccdc \
  -e KALSHI_PRIVATE_KEY_PATH=/secrets/kalshi.pem \
  -v /ABS/PATH/kalshi_private_key.pem:/secrets/kalshi.pem:ro \
  -v combo_arb_data:/data \
  -v /ABS/PATH/config.yaml:/app/config/config.yaml:ro \
  combo-arb:latest
```

In your mounted `config.yaml` set:
```yaml
persistence:
  db_path: /data/combo_arb.db     # lands on the persistent volume
```

Inspect the DB anytime:
```bash
docker run --rm -v combo_arb_data:/data -it python:3.11-slim \
  sqlite3 /data/combo_arb.db "SELECT COUNT(*) FROM combo_evaluations;"
```

## 3. Pick a host

### Option A — a small always-on VM (recommended, cheapest, most control)
Any $5–6/mo instance (Hetzner, DigitalOcean, Lightsail, GCE e2-micro):
1. Install Docker.
2. Copy the repo (or `git clone`), the `.pem`, and your `config.yaml` to the box.
3. Run detached with auto-restart:
   ```bash
   docker run -d --name combo-arb --restart unless-stopped \
     -e KALSHI_API_KEY_ID=... \
     -e KALSHI_PRIVATE_KEY_PATH=/secrets/kalshi.pem \
     -v /opt/combo-arb/kalshi.pem:/secrets/kalshi.pem:ro \
     -v combo_arb_data:/data \
     -v /opt/combo-arb/config.yaml:/app/config/config.yaml:ro \
     combo-arb:latest
   ```
4. `docker logs -f combo-arb` to watch; `--restart unless-stopped` survives reboots.

### Option B — Fly.io (managed, persistent volume, always-on)
Good balance of managed + always-on with a real disk.
- `fly launch` (no build step needed — it uses the Dockerfile), create a volume
  (`fly volumes create combo_arb_data --size 1`), mount it at `/data`.
- Set secrets: `fly secrets set KALSHI_API_KEY_ID=...`. For the key file, base64 it
  into a secret and write it to disk on boot, or bake a read-only secret mount.
- Ensure `min_machines_running = 1` so it never idles off.

### Option C — Railway / Render (easiest, managed)
Point the service at this repo; both build the Dockerfile automatically. Add a
persistent disk mounted at `/data`, set env vars in the dashboard, and upload the
key as a secret file. Use a **Background Worker**/always-on service type (not a web
service that scales to zero).

### Why not Cloud Run / Lambda?
They're request-driven and stop when idle, which kills a persistent polling loop.
Usable only if you restructure into a scheduled job (e.g. run one finite scan per
invocation on a cron) — a reasonable alternative, but a different shape than the
continuous `run` loop.

## 4. Operating notes

- **Rate limits:** `polling.max_requests_per_sec` and `polling.max_combos_per_scan`
  bound API load. Start conservative in the cloud.
- **DB growth:** with `apply_buffer: false` many combos flag; the DB grows. Rotate
  or prune `data/combo_arb.db` periodically, or raise thresholds.
- **API key scope:** create a Kalshi key with the **minimum** permissions needed
  (read + paper) so a leaked key can't move money.
- **Going live later:** only then set `mode: live`, `execution.live_enabled: true`,
  and `CONFIRM_LIVE_TRADING=YES` — and review the risk limits first.
