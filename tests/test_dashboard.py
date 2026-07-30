"""Dashboard: read-only query additions, overview assembly, and the HTTP handler."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from combo_arb.dashboard.server import _since_ts, build_overview, make_server
from combo_arb.models import Fill, InstrumentType, Order, PnL, Position, Side
from combo_arb.monitoring import queries
from combo_arb.persistence.db import Database


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "dash.db")
    db = Database(path)
    db.insert_pnl(PnL(realized=-1.0, unrealized=2.0, equity=1.0, timestamp=100.0))
    db.insert_pnl(PnL(realized=0.5, unrealized=1.0, equity=1.5, timestamp=200.0))
    db.insert_fill(Fill(order_id="o1", instrument="C", instrument_type=InstrumentType.COMBO,
                        side=Side.YES, action="buy", price=0.1, qty=10, fee=0.02))
    db.upsert_position(Position(instrument="A", instrument_type=InstrumentType.LEG,
                                net_qty=5, avg_price=0.5))
    db.upsert_market_name("C1", "Combo one")
    db.insert_order(Order(instrument="C1", instrument_type=InstrumentType.COMBO, side=Side.YES,
                          action="buy", price=0.1, qty=10, signal_ref="t-open", order_id="oc1"))
    db.insert_fill(Fill(order_id="oc1", instrument="C1", instrument_type=InstrumentType.COMBO,
                        side=Side.YES, action="buy", price=0.1, qty=10, fee=0.02))
    db.insert_open_trade(signal_ref="t-open", mve_collection_ticker="C1",
                         legs_json="[]", opened_ts=100.0, expected_pnl=1.0)
    db.insert_open_trade(signal_ref="t-set", mve_collection_ticker="C2",
                         legs_json="[]", opened_ts=50.0, expected_pnl=1.0)
    db.settle_open_trade("t-set", settled_ts=300.0, realized_pnl=2.5)
    db.insert_open_trade(signal_ref="t-exp", mve_collection_ticker="C3",
                         legs_json="[]", opened_ts=60.0, expected_pnl=1.0)
    db.expire_open_trade("t-exp", settled_ts=310.0)
    db.commit()
    db.close()
    return path


# -- query additions ----------------------------------------------------------
def test_pnl_series_ascending(db_path):
    s = queries.pnl_series(db_path)
    assert [r["equity"] for r in s] == [1.0, 1.5]      # oldest -> newest
    assert s[0]["ts_iso"] is not None


def test_open_trades_list(db_path):
    rows = queries.open_trades_list(db_path)
    assert len(rows) == 1 and rows[0]["signal_ref"] == "t-open"
    assert rows[0]["opened_iso"] is not None


def test_recent_trades_history(db_path):
    rows = queries.recent_trades(db_path)
    refs = {r["signal_ref"]: r for r in rows}
    assert set(refs) == {"t-set", "t-exp"}
    assert refs["t-set"]["status"] == "settled" and refs["t-set"]["realized_pnl"] == 2.5
    assert refs["t-exp"]["status"] == "expired" and refs["t-exp"]["realized_pnl"] is None


# -- overview assembly --------------------------------------------------------
def test_build_overview_keys(db_path):
    o = build_overview(db_path)
    assert set(o) == {"status", "pnl", "pnl_series", "open_trades", "positions"}
    assert o["pnl"]["equity"] == pytest.approx(1.5)
    assert o["open_trades"]["settled"] == 1 and o["open_trades"]["expired"] == 1
    assert len(o["pnl_series"]) == 2


# -- HTTP handler (real socket, ephemeral port) -------------------------------
@pytest.fixture
def server(db_path):
    httpd = make_server(db_path, "127.0.0.1", 0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=2)


def _get(url):
    with urllib.request.urlopen(url, timeout=3) as r:
        return r.status, r.read(), r.headers.get("Content-Type", "")


def test_api_overview_endpoint(server):
    status, body, ctype = _get(server + "/api/overview")
    assert status == 200 and "application/json" in ctype
    data = json.loads(body)
    assert "pnl" in data and "open_trades" in data


def test_index_served(server):
    status, body, ctype = _get(server + "/")
    assert status == 200 and "text/html" in ctype
    assert b"combo-arb" in body and b"/static/app.js" in body


def test_static_asset_served(server):
    status, _, ctype = _get(server + "/static/app.js")
    assert status == 200 and "javascript" in ctype


def test_api_names_endpoint(server):
    status, body, ctype = _get(server + "/api/names")
    assert status == 200 and "application/json" in ctype
    assert json.loads(body).get("C1") == "Combo one"


def test_api_trades_grouped_endpoint(server):
    status, body, _ = _get(server + "/api/trades-grouped?status=open")
    assert status == 200
    data = json.loads(body)
    t = next(t for t in data if t["signal_ref"] == "t-open")
    assert t["combo"]["instrument"] == "C1" and t["combo"]["qty"] == 10


def test_since_ts_converts_days_to_cutoff():
    now = time.time()
    cutoff = _since_ts({"days": ["3"]})
    assert cutoff == pytest.approx(now - 3 * 86400.0, abs=2.0)


def test_since_ts_absent_or_invalid_returns_none():
    assert _since_ts({}) is None
    assert _since_ts({"days": ["not-a-number"]}) is None
    assert _since_ts({"days": ["0"]}) is None
    assert _since_ts({"days": ["-5"]}) is None


def test_unknown_api_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server + "/api/nope")
    assert exc.value.code == 404


def test_static_traversal_blocked(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server + "/static/../server.py")
    assert exc.value.code == 404


def test_post_is_rejected(server):
    req = urllib.request.Request(server + "/api/overview", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=3)
    assert exc.value.code == 405
