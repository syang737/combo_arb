#!/usr/bin/env bash
# Pull the latest published image from GHCR and (re)create the container.
#
#   KALSHI_API_KEY_ID=<your-key-id> ./scripts/redeploy.sh
#
# Assumes the standard layout on the host:
#   $BASE/secrets/kalshi.pem     (your RSA private key)
#   $BASE/config/config.yaml     (db_path: /data/combo_arb.db)
#   $BASE/data/                  (persistent volume for the SQLite DB)
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/syang737/combo_arb:latest}"
NAME="${NAME:-combo-arb}"
BASE="${BASE:-$HOME/combo_arb}"

: "${KALSHI_API_KEY_ID:?set KALSHI_API_KEY_ID (e.g. KALSHI_API_KEY_ID=... ./scripts/redeploy.sh)}"

echo ">> pulling $IMAGE"
docker pull "$IMAGE"

echo ">> recreating container $NAME"
docker rm -f "$NAME" 2>/dev/null || true
docker run -d --restart unless-stopped --name "$NAME" \
  -e KALSHI_API_KEY_ID="$KALSHI_API_KEY_ID" \
  -e KALSHI_PRIVATE_KEY_PATH=/secrets/kalshi.pem \
  -v "$BASE/secrets/kalshi.pem:/secrets/kalshi.pem:ro" \
  -v "$BASE/data:/data" \
  -v "$BASE/config/config.yaml:/app/config/config.yaml:ro" \
  "$IMAGE"

echo ">> recent logs:"
sleep 2
docker logs --tail 20 "$NAME"
