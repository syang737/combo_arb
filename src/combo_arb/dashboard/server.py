"""Read-only analytics dashboard served over the Python stdlib http.server.

Zero third-party deps. Every data path goes through :mod:`combo_arb.monitoring.queries`,
which opens the SQLite DB read-only (``file:...?mode=ro``), so the dashboard can never
write or trade. GET-only; anything else is 405. Designed to bind localhost and be viewed
through an SSH tunnel (``ssh -L 8080:localhost:8080 ...``).
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from combo_arb.monitoring import queries

log = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_MAX_LIMIT = 500
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
}


def build_overview(db_path: str) -> dict:
    """Everything the dashboard needs for one refresh, in a single call."""
    return {
        "status": queries.db_status(db_path),
        "pnl": queries.pnl_summary(db_path),
        "pnl_series": queries.pnl_series(db_path, limit=500),
        "open_trades": queries.open_trades_summary(db_path),
        "positions": queries.open_positions(db_path),
    }


def _clamp_limit(qs: dict, default: int) -> int:
    try:
        n = int(qs.get("limit", [default])[0])
    except (TypeError, ValueError):
        return default
    return max(1, min(n, _MAX_LIMIT))


def _dispatch_api(path: str, qs: dict, db_path: str):
    """Route an /api/* path to a query function. Returns a JSON-able object or None (404)."""
    if path == "/api/overview":
        return build_overview(db_path)
    if path == "/api/names":
        return queries.market_names_map(db_path)
    if path == "/api/trades-grouped":
        closed = qs.get("status", ["open"])[0] == "closed"
        return queries.trades_grouped(db_path, closed=closed, limit=_clamp_limit(qs, 50))
    if path == "/api/signals":
        return queries.recent_signals(db_path, _clamp_limit(qs, 25))
    if path == "/api/fills":
        return queries.recent_fills(db_path, _clamp_limit(qs, 25))
    if path == "/api/trades":
        return queries.recent_trades(db_path, _clamp_limit(qs, 50))
    if path == "/api/open-trades":
        return queries.open_trades_list(db_path, _clamp_limit(qs, 50))
    if path == "/api/near-misses":
        return queries.top_near_misses(db_path, _clamp_limit(qs, 25))
    if path == "/api/positions":
        return queries.open_positions(db_path)
    if path == "/api/evaluation":
        ticker = qs.get("ticker", [""])[0]
        if not ticker:
            return {"error": "evaluation requires ?ticker="}
        return queries.evaluation_history(db_path, ticker, _clamp_limit(qs, 50))
    return None


def _make_handler(db_path: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "combo-arb-dashboard"

        def log_message(self, fmt, *args):  # quieter than the default stderr spam
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, default=str).encode("utf-8"), _CONTENT_TYPES[".json"])

        def _send_static(self, rel: str) -> None:
            # Resolve within the static dir; reject traversal.
            target = (_STATIC_DIR / rel).resolve()
            if not str(target).startswith(str(_STATIC_DIR.resolve())) or not target.is_file():
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            ctype = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
            self._send(200, target.read_bytes(), ctype)

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path.startswith("/api/"):
                try:
                    result = _dispatch_api(path, parse_qs(parsed.query), db_path)
                except Exception as exc:  # never leak a stack trace to the browser
                    log.exception("dashboard api error on %s", path)
                    self._send_json({"error": f"query failed: {exc}"}, code=500)
                    return
                if result is None:
                    self._send_json({"error": "unknown endpoint"}, code=404)
                else:
                    self._send_json(result)
                return
            if path in ("/", "/index.html"):
                self._send_static("index.html")
                return
            if path.startswith("/static/"):
                self._send_static(path[len("/static/"):])
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self):  # noqa: N802 - read-only server, reject writes explicitly
            self._send(405, b"method not allowed (read-only)", "text/plain; charset=utf-8")

        do_PUT = do_DELETE = do_PATCH = do_POST

    return Handler


def make_server(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    """Build (but do not start) the server. Pass port=0 to get an OS-assigned port."""
    return ThreadingHTTPServer((host, port), _make_handler(db_path))


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    httpd = make_server(db_path, host, port)
    bound_host, bound_port = httpd.server_address[0], httpd.server_address[1]
    log.info("combo-arb dashboard serving %s on http://%s:%d", db_path, bound_host, bound_port)
    if bound_host in ("127.0.0.1", "localhost"):
        log.info("localhost-only; view remotely via: ssh -L %d:localhost:%d <user>@<host>",
                 bound_port, bound_port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    finally:
        httpd.server_close()
