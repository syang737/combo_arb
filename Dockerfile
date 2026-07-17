# Container for the combo-arb engine (a long-running loop, not a web service).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    COMBO_ARB_CONFIG=/app/config/config.yaml

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Bundle the example config as a fallback; mount your own config.yaml over it.
COPY config ./config

# Non-root user; /data is where the SQLite DB should live (mount a volume there).
RUN mkdir -p /data && useradd -m app && chown -R app /app /data
USER app
VOLUME ["/data"]

# Default: continuous scan on live data in PAPER mode (no real orders).
# Requires KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH at runtime (never baked in).
CMD ["combo-arb", "run", "--source", "live", "--iterations", "0", "--log-level", "INFO"]
