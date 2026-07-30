"use strict";

const REFRESH_MS = 10000;
const $ = (id) => document.getElementById(id);

async function fetchJSON(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

// -- names (ticker -> display name) -------------------------------------------
let NAMES = {};
function nameFor(t) { return (t && NAMES[t]) || t || "—"; }
// A cell showing the readable name with the raw ticker as hover text.
function namedCell(t) {
  const n = nameFor(t);
  return n === t ? `<span class="mono">${t}</span>` : `<span title="${t}">${n}</span>`;
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
  const sameDay = d.toDateString() === new Date().toDateString();
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return sameDay ? t : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + t;
}
function axisTime(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
                 : d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function tile(k, v, cls = "") {
  return `<div class="tile"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
}

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

// -- equity curve with axes (hand-rolled inline SVG) --------------------------
function drawEquity(series) {
  const svg = $("equity-chart");
  const W = 600, H = 170, L = 46, R = 10, T = 10, B = 24;   // margins for axis labels
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const pts = (series || []).filter(p => typeof p.equity === "number");
  if (pts.length < 2) { svg.innerHTML = `<text x="${W/2}" y="${H/2}" fill="#8593a8" font-size="12" text-anchor="middle">not enough PnL points yet</text>`; return; }

  const eq = pts.map(p => p.equity);
  let lo = Math.min(...eq), hi = Math.max(...eq);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const x = (i) => L + (i / (pts.length - 1)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);

  // Y gridlines + labels (low / mid / high).
  const yvals = [lo, (lo + hi) / 2, hi];
  let grid = "", ylab = "";
  for (const v of yvals) {
    const yy = y(v).toFixed(1);
    grid += `<line x1="${L}" y1="${yy}" x2="${W - R}" y2="${yy}" stroke="#232b38"/>`;
    ylab += `<text x="${L - 6}" y="${yy}" fill="#8593a8" font-size="10" text-anchor="end" dominant-baseline="middle">${money(v)}</text>`;
  }
  // Zero line (if the range crosses zero).
  let zero = "";
  if (lo < 0 && hi > 0) { const zy = y(0).toFixed(1); zero = `<line x1="${L}" y1="${zy}" x2="${W - R}" y2="${zy}" stroke="#3a4457" stroke-dasharray="4 4"/>`; }

  // X ticks + time labels (~4 evenly spaced).
  let xlab = "";
  const nTicks = Math.min(4, pts.length);
  for (let k = 0; k < nTicks; k++) {
    const i = Math.round(k * (pts.length - 1) / (nTicks - 1));
    const anchor = k === 0 ? "start" : k === nTicks - 1 ? "end" : "middle";
    xlab += `<text x="${x(i).toFixed(1)}" y="${H - 6}" fill="#8593a8" font-size="10" text-anchor="${anchor}">${axisTime(pts[i].ts_iso)}</text>`;
  }

  const line = pts.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join(" ");
  const area = `${line} L${x(pts.length - 1).toFixed(1)},${y(lo).toFixed(1)} L${x(0).toFixed(1)},${y(lo).toFixed(1)} Z`;
  const col = eq[eq.length - 1] >= 0 ? "#3fb950" : "#f85149";
  svg.innerHTML =
    `<defs><linearGradient id="eqg" x1="0" y1="0" x2="0" y2="1">
       <stop offset="0" stop-color="${col}" stop-opacity="0.26"/>
       <stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>` +
    grid + zero +
    `<path d="${area}" fill="url(#eqg)"/>` +
    `<path d="${line}" fill="none" stroke="${col}" stroke-width="1.8" vector-effect="non-scaling-stroke"/>` +
    ylab + xlab;
}

// -- grouped trade cards ------------------------------------------------------
const statusBadge = (s) => `<span class="badge ${s}">${s}</span>`;

function resBadge(r) {
  if (r === true) return `<span class="res yes">YES</span>`;
  if (r === false) return `<span class="res no">NO</span>`;
  return "";
}

function legRow(l, opts = {}) {
  const cls = opts.combo ? "leg combo-fill" : "leg";
  const tag = opts.combo ? `<span class="tag">COMBO</span>` : "";
  return `<div class="${cls}">
    <span class="leg-name">${tag}${namedCell(l.instrument)}</span>
    <span class="leg-dir">${l.action}/${l.side}</span>
    <span class="leg-num">${l.qty} @ ${num(l.price, 3)}</span>
    <span class="leg-res">${resBadge(l.resolved_yes)}</span>
  </div>`;
}

function comboOutcome(t) {
  if (t.status === "open") return "";
  if (t.combo_resolved_yes === true) return `<span class="res yes">combo YES</span>`;
  if (t.combo_resolved_yes === false) return `<span class="res no">combo NO</span>`;
  return "";  // expired / unknown
}

function tradeCard(t) {
  const comboTicker = (t.combo && t.combo.instrument) || t.mve_collection_ticker;
  const closed = t.status !== "open";
  const pnl = closed ? t.realized_pnl : t.expected_pnl;
  const pnlLabel = closed ? "realized" : "est.";
  const when = closed ? ("closed " + fmtTime(t.settled_iso)) : ("opened " + fmtTime(t.opened_iso));
  // On settled cards, show the trade-time estimate next to the realized number.
  const est = (closed && t.expected_pnl !== null && t.expected_pnl !== undefined)
    ? `<span class="muted">est ${money(t.expected_pnl)}</span>` : "";
  // The combo's own fill (side is always YES -- combos are only ever bought/sold YES,
  // never NO; hedge legs below are the ones that go short via buy/no).
  const comboRow = t.combo
    ? legRow({ ...t.combo, resolved_yes: t.combo_resolved_yes }, { combo: true })
    : `<div class="leg combo-fill muted">no combo fill recorded</div>`;
  const legs = (t.legs || []).map((l) => legRow(l)).join("") || `<div class="leg muted">no leg fills recorded</div>`;
  return `<div class="trade-card">
    <div class="trade-head">
      <div class="trade-title">${namedCell(comboTicker)} ${statusBadge(t.status)} ${comboOutcome(t)}</div>
      <div class="trade-meta">
        <span class="${signClass(pnl)}">${pnl === null || pnl === undefined ? "—" : money(pnl)} <span class="muted">${pnlLabel}</span></span>
        ${est}
        <span class="muted">${when}</span>
      </div>
    </div>
    <div class="trade-legs">${comboRow}${legs}</div>
  </div>`;
}

function renderTradeCards(el, trades) {
  if (isErr(trades)) { el.innerHTML = `<div class="empty">${trades.error || trades[0].error}</div>`; return; }
  if (!trades || trades.length === 0) { el.innerHTML = `<div class="empty">no trades yet</div>`; return; }
  el.innerHTML = trades.map(tradeCard).join("");
}

// -- panels -------------------------------------------------------------------
function renderStatus(status) {
  const pill = $("engine-pill");
  if (isErr(status) || !status.exists) {
    pill.className = "pill down"; pill.textContent = "no data";
    $("db-meta").textContent = status.error || ""; return;
  }
  const ageS = status.last_update_ts ? (Date.now() / 1000 - status.last_update_ts) : Infinity;
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

function renderOpenTradeTiles(ot) {
  const el = $("ot-tiles");
  if (isErr(ot)) { el.innerHTML = `<div class="empty">${ot.error}</div>`; return; }
  el.innerHTML =
    tile("Open", ot.open ?? 0) +
    tile("Settled", ot.settled ?? 0, "pos") +
    tile("Expired", ot.expired ?? 0, ot.expired ? "neg" : "") +
    tile("Realized (settled)", money(ot.settled_realized_pnl), signClass(ot.settled_realized_pnl));
  $("ot-oldest").textContent = ot.oldest_open_iso ? "oldest open " + fmtTime(ot.oldest_open_iso) : "";
}

// -- refresh cycles -----------------------------------------------------------
async function loadNames() {
  try { const m = await fetchJSON("/api/names"); if (m && !m.error) NAMES = m; } catch (e) { /* keep last */ }
}

async function refreshOverview() {
  const o = await fetchJSON("/api/overview");
  renderStatus(o.status);
  renderPnl(o.pnl, o.pnl_series);
  renderOpenTradeTiles(o.open_trades);
  renderTable($("positions-tbl"), o.positions, [
    { key: "instrument", label: "instrument", fmt: (v) => namedCell(v) },
    { key: "instrument_type", label: "type" },
    { key: "net_qty", label: "net", num: true, cls: (v) => signClass(v) },
    { key: "avg_price", label: "avg", num: true, fmt: (v) => num(v, 3) },
    { key: "updated_iso", label: "updated", fmt: fmtTime },
  ]);
}

async function refreshTables() {
  const [openTrades, history, signals, fills, nearMiss] = await Promise.all([
    fetchJSON("/api/trades-grouped?status=open&limit=50"),
    fetchJSON("/api/trades-grouped?status=closed&limit=50"),
    fetchJSON("/api/signals?limit=25"),
    fetchJSON("/api/fills?limit=25"),
    fetchJSON("/api/near-misses?limit=25"),
  ]);

  renderTradeCards($("opentrades-cards"), openTrades);
  renderTradeCards($("trades-cards"), history);

  renderTable($("signals-tbl"), signals, [
    { key: "ts_iso", label: "time", fmt: fmtTime },
    { key: "mve_collection_ticker", label: "combo", fmt: (v) => namedCell(v) },
    { key: "combo_quote_yes", label: "quote", num: true, fmt: (v) => num(v, 3) },
    { key: "fair_combo", label: "fair", num: true, fmt: (v) => num(v, 3) },
    { key: "fees_estimate", label: "fees", num: true, fmt: (v) => num(v, 3) },
    { key: "arbitrage_margin", label: "edge", num: true, fmt: (v) => num(v, 3), cls: (v) => signClass(v) },
    { key: "size", label: "size", num: true },
    { key: "action", label: "action" },
  ]);

  renderTable($("fills-tbl"), fills, [
    { key: "ts_iso", label: "time", fmt: fmtTime },
    { key: "instrument", label: "instrument", fmt: (v) => namedCell(v) },
    { key: "instrument_type", label: "type" },
    { key: "side", label: "side" },
    { key: "action", label: "act" },
    { key: "price", label: "price", num: true, fmt: (v) => num(v, 3) },
    { key: "qty", label: "qty", num: true },
    { key: "fee", label: "fee", num: true, fmt: (v) => num(v, 2) },
  ]);

  renderTable($("nearmiss-tbl"), nearMiss, [
    { key: "ts_iso", label: "time", fmt: fmtTime },
    { key: "mve_collection_ticker", label: "combo", fmt: (v) => namedCell(v) },
    { key: "combo_quote_yes", label: "quote", num: true, fmt: (v) => num(v, 3) },
    { key: "fair_combo", label: "fair", num: true, fmt: (v) => num(v, 3) },
    { key: "arbitrage_margin", label: "edge", num: true, fmt: (v) => num(v, 3), cls: (v) => signClass(v) },
    { key: "gap_to_flag", label: "gap", num: true, fmt: (v) => num(v, 3) },
  ]);
}

async function refreshAll() {
  try {
    await loadNames();                       // names first so tables/cards can resolve them
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
