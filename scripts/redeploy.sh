#!/usr/bin/env bash
# Pull the latest published image from GHCR and (re)create the container.
#
#   ./scripts/redeploy.sh
#
# Credentials come from an env file so you don't re-paste them each redeploy.
# Create $BASE/.env (default ~/combo_arb/.env), chmod 600, containing at least:
#   KALSHI_API_KEY_ID=<your-key-id>
# and, only when you deliberately go live:
#   CONFIRM_LIVE_TRADING=YES
#
# Assumes the standard layout on the host:
#   $BASE/.env                   (KALSHI_API_KEY_ID=..., optional CONFIRM_LIVE_TRADING)
#   $BASE/secrets/kalshi.pem     (your RSA private key)
#   $BASE/config/config.yaml     (db_path: /data/combo_arb.db)
#   $BASE/data/                  (persistent volume for the SQLite DB)
#
# Overrides: IMAGE=, NAME=, BASE=, ENV_FILE=. A one-off inline
# KALSHI_API_KEY_ID=... still works and takes precedence over the env file.
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/syang737/combo_arb:latest}"
NAME="${NAME:-combo-arb}"
BASE="${BASE:-$HOME/combo_arb}"
ENV_FILE="${ENV_FILE:-$BASE/.env}"

# Resolve credentials: inline env var wins; otherwise pass the env file to docker.
env_args=()
if [[ -n "${KALSHI_API_KEY_ID:-}" ]]; then
  echo ">> using KALSHI_API_KEY_ID from the environment"
  env_args+=(-e "KALSHI_API_KEY_ID=$KALSHI_API_KEY_ID")
  [[ -n "${CONFIRM_LIVE_TRADING:-}" ]] && env_args+=(-e "CONFIRM_LIVE_TRADING=$CONFIRM_LIVE_TRADING")
elif [[ -f "$ENV_FILE" ]]; then
  echo ">> using env file $ENV_FILE"
  env_args+=(--env-file "$ENV_FILE")
else
  echo "ERROR: no credentials. Create $ENV_FILE (chmod 600) with:" >&2
  echo "  KALSHI_API_KEY_ID=<your-key-id>" >&2
  echo "…or run: KALSHI_API_KEY_ID=<your-key-id> ./scripts/redeploy.sh" >&2
  exit 1
fi

echo ">> pulling $IMAGE"
docker pull "$IMAGE"

echo ">> recreating container $NAME"
docker rm -f "$NAME" 2>/dev/null || true
docker run -d --restart unless-stopped --name "$NAME" \
  "${env_args[@]}" \
  -e KALSHI_PRIVATE_KEY_PATH=/secrets/kalshi.pem \
  -v "$BASE/secrets/kalshi.pem:/secrets/kalshi.pem:ro" \
  -v "$BASE/data:/data" \
  -v "$BASE/config/config.yaml:/app/config/config.yaml:ro" \
  "$IMAGE"

echo ">> running commit:"
docker inspect "$NAME" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' || true
echo ">> recent logs:"
sleep 2
docker logs --tail 20 "$NAME"
