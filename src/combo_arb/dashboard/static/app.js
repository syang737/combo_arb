"use strict";

const REFRESH_MS = 10000;
const $ = (id) => document.getElementById(id);

async function fetchJSON(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

// -- formatting ---------------------------------------------------------------
const isErr = (d) => d && (d.error || (Array.isArray(d) && d[0] && d[0].error));
const num = (v, dp = 2) => (v === null || v === undefined || isNaN(v)) ? "—" : Number(v).toFixed(dp);
const money = (v, dp = 2) => (v === null || v === undefined || isNaN(v)) ? "—" : (v < 0 ? "-$" : "$") + Math.abs(v).toFixed(dp);
const signClass = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return sameDay ? t : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + t;
}

function tile(k, v, cls = "") {
  return `<div class="tile"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
}

// Build a table from rows given a column spec [{key,label,fmt?,cls?,mono?}].
function renderTable(el, rows, cols) {
  if (isErr(rows)) { el.innerHTML = `<tbody><tr><td class="empty">${rows.error || rows[0].error}</td></tr></tbody>`; return; }
  if (!rows || rows.length === 0) { el.innerHTML = `<tbody><tr><td class="empty">no rows yet</td></tr></tbody>`; return; }
  const head = "<thead><tr>" + cols.map(c => `<th class="${c.mono ? "mono" : ""} ${c.num ? "num" : ""}">${c.label}</th>`).join("") + "</tr></thead>";
  const body = "<tbody>" + rows.map(row => "<tr>" + cols.map(c => {
    let v = c.fmt ? c.fmt(row[c.key], row) : (row[c.key] ?? "—");
    const cls = (typeof c.cls === "function" ? c.cls(row[c.key], row) : (c.cls || "")) + (c.num ? " num" : "") + (c.mono ? " mono" : "");
    return `<td class="${cls.trim()}">${v}</td>`;
  }).join("") + "</tr>").join("") + "</tbody>";
  el.innerHTML = head + body;
}

// -- equity curve (hand-rolled inline SVG, no libs) ---------------------------
function drawEquity(series) {
  const svg = $("equity-chart");
  const W = 600, H = 140, pad = 6;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const pts = (series || []).filter(p => typeof p.equity === "number");
  if (pts.length < 2) { svg.innerHTML = `<text x="${W/2}" y="${H/2}" fill="#8593a8" font-size="12" text-anchor="middle">not enough PnL points yet</text>`; return; }
  const eq = pts.map(p => p.equity);
  let lo = Math.min(...eq), hi = Math.max(...eq);
  if (lo === hi) { lo -= 1; hi += 1; }
  const x = (i) => pad + (i / (pts.length - 1)) * (W - 2 * pad);
  const y = (v) => H - pad - ((v - lo) / (hi - lo)) * (H - 2 * pad);
  const line = pts.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join(" ");
  const area = `${line} L${x(pts.length - 1).toFixed(1)},${H - pad} L${x(0).toFixed(1)},${H - pad} Z`;
  const last = eq[eq.length - 1];
  const col = last >= 0 ? "#3fb950" : "#f85149";
  let zero = "";
  if (lo < 0 && hi > 0) { const zy = y(0).toFixed(1); zero = `<line x1="${pad}" y1="${zy}" x2="${W - pad}" y2="${zy}" stroke="#2a3240" stroke-dasharray="4 4"/>`; }
  svg.innerHTML =
    `<defs><linearGradient id="eqg" x1="0" y1="0" x2="0" y2="1">
       <stop offset="0" stop-color="${col}" stop-opacity="0.28"/>
       <stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>` +
    zero +
    `<path d="${area}" fill="url(#eqg)"/>` +
    `<path d="${line}" fill="none" stroke="${col}" stroke-width="1.8" vector-effect="non-scaling-stroke"/>`;
}

// -- panels -------------------------------------------------------------------
function renderStatus(status) {
  const pill = $("engine-pill");
  if (isErr(status) || !status.exists) {
    pill.className = "pill down"; pill.textContent = "no data";
    $("db-meta").textContent = status.error || "";
    return;
  }
  const ts = status.last_update_ts;
  const ageS = ts ? (Date.now() / 1000 - ts) : Infinity;
  let cls = "live", label = "live";
  if (ageS > 300) { cls = "down"; label = "stalled"; }
  else if (ageS > 90) { cls = "stale"; label = "quiet"; }
  pill.className = "pill " + cls;
  pill.textContent = label + (isFinite(ageS) ? ` · ${Math.round(ageS)}s ago` : "");
  const kb = status.size_bytes ? (status.size_bytes / 1024 / 1024).toFixed(1) + " MB" : "—";
  const evals = status.row_counts ? status.row_counts.combo_evaluations : null;
  $("db-meta").textContent = `db ${kb} · ${evals ?? "—"} evals · updated ${fmtTime(status.last_update_iso)}`;
  $("db-path").textContent = status.db_path || "";
}

function renderPnl(pnl, series) {
  const el = $("pnl-tiles");
  if (isErr(pnl)) { el.innerHTML = `<div class="empty">${pnl.error}</div>`; drawEquity([]); return; }
  el.innerHTML =
    tile("Equity", money(pnl.equity), signClass(pnl.equity)) +
    tile("Realized", money(pnl.realized), signClass(pnl.realized)) +
    tile("Unrealized (est.)", money(pnl.unrealized), signClass(pnl.unrealized)) +
    tile("PnL rows", pnl.trades ?? "—");
  $("pnl-asof").textContent = pnl.as_of_iso ? "as of " + fmtTime(pnl.as_of_iso) : "";
  drawEquity(series);
}

function renderOpenTrades(ot) {
  const el = $("ot-tiles");
  if (isErr(ot)) { el.innerHTML = `<div class="empty">${ot.error}</div>`; return; }
  el.innerHTML =
    tile("Open", ot.open ?? 0, ot.open ? "" : "") +
    tile("Settled", ot.settled ?? 0, "pos") +
    tile("Expired", ot.expired ?? 0, ot.expired ? "neg" : "") +
    tile("Realized (settled)", money(ot.settled_realized_pnl), signClass(ot.settled_realized_pnl));
  $("ot-oldest").textContent = ot.oldest_open_iso ? "oldest open " + fmtTime(ot.oldest_open_iso) : "";
}

const statusBadge = (s) => `<span class="badge ${s}">${s}</span>`;

// -- refresh cycles -----------------------------------------------------------
async function refreshOverview() {
  const o = await fetchJSON("/api/overview");
  renderStatus(o.status);
  renderPnl(o.pnl, o.pnl_series);
  renderOpenTrades(o.open_trades);
  renderTable($("positions-tbl"), o.positions, [
    { key: "instrument", label: "instrument", mono: true },
    { key: "instrument_type", label: "type" },
    { key: "net_qty", label: "net", num: true, cls: (v) => signClass(v) },
    { key: "avg_price", label: "avg", num: true, fmt: (v) => num(v, 3) },
    { key: "updated_iso", label: "updated", fmt: fmtTime },
  ]);
}

async function refreshTables() {
  const [signals, fills, trades, openTrades, nearMiss] = await Promise.all([
    fetchJSON("/api/signals?limit=25"),
    fetchJSON("/api/fills?limit=25"),
    fetchJSON("/api/trades?limit=50"),
    fetchJSON("/api/open-trades?limit=50"),
    fetchJSON("/api/near-misses?limit=25"),
  ]);

  renderTable($("signals-tbl"), signals, [
    { key: "ts_iso", label: "time", fmt: fmtTime },
    { key: "mve_collection_ticker", label: "combo", mono: true },
    { key: "combo_quote_yes", label: "quote", num: true, fmt: (v) => num(v, 3) },
    { key: "fair_combo", label: "fair", num: true, fmt: (v) => num(v, 3) },
    { key: "fees_estimate", label: "fees", num: true, fmt: (v) => num(v, 3) },
    { key: "arbitrage_margin", label: "edge", num: true, fmt: (v) => num(v, 3), cls: (v) => signClass(v) },
    { key: "size", label: "size", num: true },
    { key: "action", label: "action" },
  ]);

  renderTable($("fills-tbl"), fills, [
    { key: "ts_iso", label: "time", fmt: fmtTime },
    { key: "instrument", label: "instrument", mono: true },
    { key: "side", label: "side" },
    { key: "action", label: "act" },
    { key: "price", label: "price", num: true, fmt: (v) => num(v, 3) },
    { key: "qty", label: "qty", num: true },
    { key: "fee", label: "fee", num: true, fmt: (v) => num(v, 2) },
  ]);

  renderTable($("trades-tbl"), trades, [
    { key: "settled_iso", label: "closed", fmt: fmtTime },
    { key: "mve_collection_ticker", label: "combo", mono: true },
    { key: "status", label: "status", fmt: statusBadge },
    { key: "expected_pnl", label: "expected", num: true, fmt: (v) => money(v), cls: (v) => signClass(v) },
    { key: "realized_pnl", label: "realized", num: true, fmt: (v) => (v === null ? "—" : money(v)), cls: (v) => signClass(v) },
  ]);

  renderTable($("opentrades-tbl"), openTrades, [
    { key: "opened_iso", label: "opened", fmt: fmtTime },
    { key: "mve_collection_ticker", label: "combo", mono: true },
    { key: "signal_ref", label: "signal", mono: true },
    { key: "expected_pnl", label: "expected", num: true, fmt: (v) => money(v), cls: (v) => signClass(v) },
  ]);

  renderTable($("nearmiss-tbl"), nearMiss, [
    { key: "ts_iso", label: "time", fmt: fmtTime },
    { key: "mve_collection_ticker", label: "combo", mono: true },
    { key: "combo_quote_yes", label: "quote", num: true, fmt: (v) => num(v, 3) },
    { key: "fair_combo", label: "fair", num: true, fmt: (v) => num(v, 3) },
    { key: "arbitrage_margin", label: "edge", num: true, fmt: (v) => num(v, 3), cls: (v) => signClass(v) },
    { key: "gap_to_flag", label: "gap", num: true, fmt: (v) => num(v, 3) },
  ]);
}

async function refreshAll() {
  try {
    await Promise.all([refreshOverview(), refreshTables()]);
    $("last-refresh").textContent = "refreshed " + new Date().toLocaleTimeString();
  } catch (e) {
    $("engine-pill").className = "pill down";
    $("engine-pill").textContent = "unreachable";
    $("last-refresh").textContent = String(e);
  }
}

let timer = null;
function schedule() {
  if (timer) clearInterval(timer);
  if ($("autorefresh").checked) timer = setInterval(refreshAll, REFRESH_MS);
}

$("refresh-btn").addEventListener("click", refreshAll);
$("autorefresh").addEventListener("change", schedule);
refreshAll();
schedule();
