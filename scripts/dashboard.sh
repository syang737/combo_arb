#!/usr/bin/env bash
# Run the read-only analytics dashboard as a SECOND container beside the engine.
#
#   ./scripts/dashboard.sh
#
# It uses the same image, mounts the DB volume READ-ONLY, and publishes the port
# on the host's loopback only (127.0.0.1) -- so it is reachable exclusively via an
# SSH tunnel, never from the public internet. No Kalshi credentials are needed
# (the dashboard only reads the SQLite DB).
#
# View it from your laptop:
#   ssh -L 8080:localhost:8080 <user>@<instance-ip>
#   then open http://localhost:8080
#
# Overrides: IMAGE=, NAME=, BASE=, PORT=.
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/syang737/combo_arb:latest}"
NAME="${NAME:-combo-arb-dashboard}"
BASE="${BASE:-$HOME/combo_arb}"
PORT="${PORT:-8080}"

echo ">> pulling $IMAGE"
docker pull "$IMAGE"

echo ">> recreating container $NAME on 127.0.0.1:$PORT (localhost-only)"
docker rm -f "$NAME" 2>/dev/null || true
docker run -d --restart unless-stopped --name "$NAME" \
  -p "127.0.0.1:${PORT}:8080" \
  -v "$BASE/data:/data:ro" \
  "$IMAGE" \
  combo-arb dashboard --db /data/combo_arb.db --host 0.0.0.0 --port 8080

echo ">> recent logs:"
sleep 2
docker logs --tail 10 "$NAME"
echo
echo ">> view it:  ssh -L ${PORT}:localhost:${PORT} <user>@<instance-ip>   then http://localhost:${PORT}"
