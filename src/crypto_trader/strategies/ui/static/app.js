"use strict";
/* CryptoTrader engine UI (D30). Presentation only: every action goes through the local API (server.py). */
const $ = (s, r = document) => r.querySelector(s);
const main = $("#main");
const isNum = (x) => x !== null && x !== undefined && x !== "" && !isNaN(x);
const fmtPct = (x, d = 1) => isNum(x) ? (x * 100).toFixed(d) + "%" : "—";
const fmtSPct = (x, d = 1) => isNum(x) ? (x > 0 ? "+" : "") + (x * 100).toFixed(d) + "%" : "—";
const fmtNum = (x, d = 2) => isNum(x) ? Number(x).toLocaleString("en-US", { maximumFractionDigits: d }) : "—";
const fmtUsd = (x, d = 2) => isNum(x) ? (+x < 0 ? "-$" : "$") + Math.abs(Number(x)).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }) : "—";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const sign = (x) => (isNum(x) && +x < 0 ? "bad" : "good");

/* ---------- theme + clock ---------- */
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  $("#theme-name").textContent = t === "light" ? "فاتح" : "داكن";
  try { localStorage.setItem("ct-theme", t); } catch { /* storage may be blocked */ }
}
let savedTheme = "dark";
try { savedTheme = localStorage.getItem("ct-theme") || "dark"; } catch { /* default */ }
applyTheme(savedTheme);
$("#theme-btn").onclick = () => { applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light"); if (current) nav(current); };
const tick = () => { $("#clock").textContent = new Date().toISOString().slice(0, 19).replace("T", " ") + " غرينتش"; };
tick(); setInterval(tick, 1000);

async function api(path, body, timeoutMs) {
  // reads are idempotent: on this machine a larger local response is sometimes cut (reset after ~19 s),
  // so a read that stalls is aborted after 8 s and retried; writes are never retried
  if (body !== undefined) return apiOnce(path, body, timeoutMs || 180000);
  let last;
  for (let attempt = 0; attempt < 3; attempt++) {
    try { return await apiOnce(path, undefined, timeoutMs || 8000); }
    catch (e) { last = e; if (e.status) throw e; }   // an HTTP error answer is final; only network failures retry
  }
  throw last;
}
async function apiOnce(path, body, timeoutMs) {
  // no-store: never wait behind another in-flight request for the same URL; abort: never hang the page
  const ctl = new AbortController(), to = setTimeout(() => ctl.abort(), timeoutMs);
  const opt = { cache: "no-store", signal: ctl.signal };
  if (body !== undefined) Object.assign(opt, { method: "POST", headers: { "Content-Type": "application/json", "X-CT-UI": "1" }, body: JSON.stringify(body) });
  try {
    const r = await fetch(path, opt);
    const j = await r.json();
    if (!r.ok) throw Object.assign(new Error(j.error || r.statusText), { status: r.status });
    return j;
  } catch (e) {
    if (e.name === "AbortError") throw new Error("الخادم ما رد خلال " + Math.round(timeoutMs / 1000) + " ثانية. تأكد إن الواجهة شغالة وحدّث الصفحة.");
    throw e;
  } finally { clearTimeout(to); }
}
function errBox(e) {   // Arabic message; the original text stays available under «تفاصيل تقنية»
  const { ar, raw } = trErr(e && e.message !== undefined ? e.message : e);
  return `<div class="err">${esc(ar || "حدث خطأ.")}${raw ? `<details class="tech"><summary>تفاصيل تقنية</summary><pre>${esc(raw)}</pre></details>` : ""}</div>`;
}
function uiConfirm(text, okLabel = "تأكيد") {   // in-page dialog instead of the browser's English confirm()
  return new Promise((resolve) => {
    const d = document.createElement("div"); d.className = "modal";
    d.innerHTML = `<div class="modal-box"><div class="modal-text">${esc(text)}</div><div class="actions"><button class="danger" data-ok>${esc(okLabel)}</button><button data-no>إلغاء</button></div></div>`;
    const done = (v) => { d.remove(); resolve(v); };
    d.onclick = (e) => { if (e.target.closest("[data-ok]")) done(true); else if (e.target.closest("[data-no]") || e.target === d) done(false); };
    document.body.appendChild(d); d.querySelector("[data-no]").focus();
  });
}
const loading = (t) => `<div class="loading"><div class="spin"></div>${esc(t)}</div>`;
let timer = null, current = null;
function nav(page) {
  clearInterval(timer);
  current = page;
  document.querySelectorAll(".side a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
  (PAGES[page] || PAGES.home)().catch((e) => { main.innerHTML = errBox(e); });
  if (location.hash.slice(1) !== page) location.hash = page;
}
document.querySelectorAll(".side a").forEach((a) => a.addEventListener("click", () => nav(a.dataset.page)));

/* ---------- building blocks ---------- */
const IC = {
  wallet: '<path d="M3 7h16a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/><path d="M3 7l12-4 2 4"/><circle cx="16.5" cy="13" r="1.2"/>',
  trend: '<path d="M3 17l6-6 4 4 8-8"/><path d="M14 7h7v7"/>',
  down: '<path d="M3 7l6 6 4-4 8 8"/><path d="M14 17h7v-7"/>',
  play: '<circle cx="12" cy="12" r="9"/><path d="M10 8l6 4-6 4V8z"/>',
  flask: '<path d="M9 3h6"/><path d="M10 3v6l-5 9a2 2 0 0 0 2 3h10a2 2 0 0 0 2-3l-5-9V3"/>',
  box: '<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/>',
  pct: '<path d="M19 5L5 19"/><circle cx="7" cy="7" r="2.5"/><circle cx="17" cy="17" r="2.5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  coins: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
  shield: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z"/>',
  gauge: '<path d="M12 14l4-4"/><path d="M3.5 18a9 9 0 1 1 17 0"/>',
  db: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  check: '<path d="M20 6L9 17l-5-5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/>',
  layers: '<path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M3 13l9 5 9-5"/>',
};
const svg = (k) => `<svg viewBox="0 0 24 24">${IC[k] || IC.info}</svg>`;
function kpi({ k, v, cls = "", s = "", ic = "info", tone = "", spark = "", small = false }) {
  return `<div class="kpi ${tone}"><div class="top"><div class="ic">${svg(ic)}</div><div class="k">${k}</div></div>
    <div class="v ${cls} ${small ? "sm" : ""} ltr">${v}</div>${s ? `<div class="s">${s}</div>` : ""}${spark ? `<canvas class="spark" id="${spark}"></canvas>` : ""}</div>`;
}
const header = (title, sub, chips = "") => `<div class="ph"><div><h1>${title}</h1>${sub ? `<p>${sub}</p>` : ""}</div><div class="chips">${chips}</div></div>`;
const chip = (text, tone = "") => `<span class="chip ${tone}"><i></i>${text}</span>`;
const empty = (text, ic = "layers") => `<div class="empty">${svg(ic)}<div>${text}</div></div>`;
const verdictPill = (v) => v === "promising" ? '<span class="pill good">واعدة</span>' : '<span class="pill warn">غير مثبتة</span>';
function opDetails(o) {   // operations.jsonl entry -> «label: value» pairs (stored keys unchanged)
  const fmt = (k, v) => k === "mode" ? arOf(AR.mode, v) : v && typeof v === "object" ? Object.entries(v).map(([a, b]) => `${arOf(AR.opKeys, a)}: ${typeof b === "object" ? JSON.stringify(b) : b}`).join("، ") : v;
  return Object.entries(o).filter(([k]) => !["at", "kind"].includes(k)).map(([k, v]) => `${esc(arOf(AR.opKeys, k))}: <span class="ltr">${esc(fmt(k, v))}</span>`).join(" · ");
}
const note = (t) => `<div class="note">${svg("info")}<div>${t}</div></div>`;

/* ---------- charts (canvas, no library) ----------
   Time-based x axis. A shared "view" {t0, t1} lets several charts zoom and pan together:
   mouse wheel = zoom at the cursor, drag = pan, double-click = show all. */
function setupCanvas(c, h) {
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth || 800;
  c.width = w * dpr; c.height = h * dpr; c.style.height = h + "px";
  const g = c.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.font = "11px Segoe UI, sans-serif"; g.textBaseline = "middle"; g.direction = "ltr";   // numbers like -20% stay -20%
  return { g, w, h };
}
const yScale = (lo, hi, top, bot) => { const span = (hi - lo) || 1; return (v) => bot - (v - lo) / span * (bot - top); };
function niceTicks(lo, hi, n = 5) {
  const span = (hi - lo) || Math.abs(hi) || 1, raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
  const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
  return out;
}
function grid(g, L, R, T, B, lo, hi, fmt, ticks) {
  const y = yScale(lo, hi, T, B);
  g.strokeStyle = css("--line"); g.lineWidth = 1; g.fillStyle = css("--faint"); g.textAlign = "left";
  (ticks || niceTicks(lo, hi)).forEach((v) => { const yy = Math.round(y(v)) + 0.5; if (yy < T - 1 || yy > B + 1) return; g.beginPath(); g.setLineDash([3, 4]); g.moveTo(L, yy); g.lineTo(R, yy); g.stroke(); g.setLineDash([]); g.fillText(fmt(v), R + 8, yy); });
  return y;
}
function timeAxis(g, L, R, B, t0, t1) {
  g.fillStyle = css("--faint"); g.textAlign = "center";
  const span = t1 - t0, n = Math.max(2, Math.min(6, Math.floor((R - L) / 95) + 1)), longSpan = span > 86400e3 * 3;   // labels never overlap
  for (let k = 0; k < n; k++) { const t = t0 + span * k / (n - 1), x = L + (R - L) * k / (n - 1);
    const s = new Date(t).toISOString(); g.fillText(longSpan ? s.slice(0, 10) : s.slice(5, 16).replace("T", " "), Math.min(R - 32, Math.max(L + 32, x)), B + 14); }
}
const msOf = (t) => (typeof t === "number" ? t : Date.parse(t));
function makeView(tmin, tmax) {
  const v = { tmin, tmax, t0: tmin, t1: tmax, subs: [] };
  v.set = (a, b) => { const minSpan = Math.max(3 * 3600e3, (tmax - tmin) / 400); if (b - a < minSpan) { const m = (a + b) / 2; a = m - minSpan / 2; b = m + minSpan / 2; }
    if (a < tmin) { b += tmin - a; a = tmin; } if (b > tmax) { a -= b - tmax; b = tmax; } v.t0 = Math.max(tmin, a); v.t1 = Math.min(tmax, b); v.subs.forEach((f) => f()); };
  v.last = (span) => v.set(tmax - span, tmax);
  v.reset = () => v.set(tmin, tmax);
  return v;
}
function interact(c, view, L, R) {
  if (!view || c._interact) return; c._interact = true;
  c.style.cursor = "crosshair"; c.style.touchAction = "none";
  c.addEventListener("wheel", (e) => { e.preventDefault(); const r = c.getBoundingClientRect(), f = Math.min(1, Math.max(0, (e.clientX - r.left - L) / (R() - L)));
    const span = view.t1 - view.t0, ns = span * (e.deltaY > 0 ? 1.18 : 0.85), at = view.t0 + span * f; view.set(at - ns * f, at + ns * (1 - f)); }, { passive: false });
  let drag = null;
  c.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, t0: view.t0, t1: view.t1 }; c.setPointerCapture(e.pointerId); c.style.cursor = "grabbing"; });
  c.addEventListener("pointermove", (e) => { if (!drag) return; const dt = (e.clientX - drag.x) / (R() - L) * (drag.t1 - drag.t0); view.set(drag.t0 - dt, drag.t1 - dt);
    const tip = c.parentElement.querySelector(".tip"); if (tip) tip.style.display = "none"; });
  const end = () => { drag = null; c.style.cursor = "crosshair"; };
  c.addEventListener("pointerup", end); c.addEventListener("pointercancel", end);
  c.addEventListener("dblclick", () => view.reset());
}
function hover(c, count, xOf, html) {
  const box = c.parentElement; box.classList.add("chart");
  let tip = box.querySelector(".tip"), xh = box.querySelector(".xh");
  if (!tip) { tip = document.createElement("div"); tip.className = "tip"; box.appendChild(tip); }
  if (!xh) { xh = document.createElement("div"); xh.className = "xh"; box.appendChild(xh); }
  c.onmousemove = (e) => {
    if (e.buttons) return;
    const r = c.getBoundingClientRect(), px = e.clientX - r.left;
    let i = 0, best = Infinity; for (let k = 0; k < count; k++) { const d = Math.abs(xOf(k) - px); if (d < best) { best = d; i = k; } }
    xh.style.display = "block"; xh.style.left = (c.offsetLeft + xOf(i)) + "px"; xh.style.top = c.offsetTop + "px"; xh.style.height = r.height + "px";
    tip.innerHTML = html(i, e.clientY - r.top); tip.style.display = "block";
    const tw = tip.offsetWidth; let left = c.offsetLeft + xOf(i) + 14; if (left + tw > box.clientWidth) left = c.offsetLeft + xOf(i) - tw - 14;
    tip.style.left = Math.max(0, left) + "px"; tip.style.top = (c.offsetTop + Math.max(4, e.clientY - r.top - 30)) + "px";
  };
  c.onmouseleave = () => { tip.style.display = "none"; xh.style.display = "none"; };
}
function candles(c, bars, trades, overlays = [], h = 360, view = null) {
  const draw = () => {
    const { g, w } = setupCanvas(c, h);
    if (!bars.length) { g.fillStyle = css("--muted"); g.textAlign = "center"; g.fillText("لا توجد بيانات بعد", w / 2, h / 2); return; }
    bars._ms = bars._ms || bars.map((b) => msOf(b.t));
    const ms = bars._ms, L = 8, R = w - 72, T = 12, B = h - 26;
    let i0 = 0, i1 = bars.length;
    if (view) { while (i0 < bars.length - 1 && ms[i0] < view.t0) i0++; i1 = i0; while (i1 < bars.length && ms[i1] <= view.t1) i1++; if (i1 - i0 < 2) { i0 = Math.max(0, i1 - 2); } }
    const vb = bars.slice(i0, i1);
    let lo = Math.min(...vb.map((b) => b.l)), hi = Math.max(...vb.map((b) => b.h)); const pad = (hi - lo) * 0.05 || 1; lo -= pad; hi += pad;
    const y = grid(g, L, R, T, B, lo, hi, (v) => fmtNum(v, v > 100 ? 0 : 2));
    const bw = (R - L) / vb.length, x = (k) => L + k * bw + bw / 2;
    const up = css("--up"), dn = css("--down");
    vb.forEach((b, k) => { const isUp = b.c >= b.o; g.strokeStyle = g.fillStyle = isUp ? up : dn;
      g.beginPath(); g.moveTo(Math.round(x(k)) + 0.5, y(b.h)); g.lineTo(Math.round(x(k)) + 0.5, y(b.l)); g.stroke();
      const top = y(Math.max(b.o, b.c)), bot = y(Math.min(b.o, b.c)), bwid = Math.max(1, Math.min(14, bw * 0.7)); g.fillRect(x(k) - bwid / 2, top, bwid, Math.max(1, bot - top)); });
    overlays.forEach((o) => { g.strokeStyle = o.color; g.lineWidth = 1.6; g.beginPath(); let s = false;
      o.values.slice(i0, i1).forEach((v, k) => { if (v === null || isNaN(v)) return; s ? g.lineTo(x(k), y(v)) : g.moveTo(x(k), y(v)); s = true; }); g.stroke(); g.lineWidth = 1; });
    const idx = (t) => { const ts = msOf(t); let a = 0, b = ms.length - 1; while (a < b) { const m = (a + b + 1) >> 1; if (ms[m] <= ts) a = m; else b = m - 1; } return a; };
    const marks = [];
    (trades || []).forEach((t) => { if (t.entry_time) marks.push({ i: idx(t.entry_time) - i0, p: +t.entry_price, buy: true, t }); if (t.exit_time) marks.push({ i: idx(t.exit_time) - i0, p: +t.exit_price, buy: false, t }); });
    const vis = marks.filter((m) => m.i >= 0 && m.i < vb.length);
    const big = vb.length < 260;
    vis.forEach((m) => { const xx = x(m.i), yy = y(m.p), s = big ? 7 : 5; g.fillStyle = m.buy ? up : dn; g.strokeStyle = css("--panel"); g.lineWidth = 1.5; g.beginPath();
      if (m.buy) { g.moveTo(xx, yy + 4); g.lineTo(xx - s, yy + 4 + s * 1.7); g.lineTo(xx + s, yy + 4 + s * 1.7); } else { g.moveTo(xx, yy - 4); g.lineTo(xx - s, yy - 4 - s * 1.7); g.lineTo(xx + s, yy - 4 - s * 1.7); }
      g.closePath(); g.fill(); g.stroke(); g.lineWidth = 1; });
    if (big) vis.filter((m) => !m.buy && isNum(m.t.pnl_quote)).forEach((m) => { g.fillStyle = +m.t.pnl_quote >= 0 ? up : dn; g.textAlign = "center"; g.fillText((+m.t.pnl_quote >= 0 ? "+" : "") + fmtNum(m.t.pnl_quote, 0), x(m.i), y(m.p) - 24); });
    const last = vb[vb.length - 1], ly = y(last.c);
    g.strokeStyle = last.c >= last.o ? up : dn; g.setLineDash([2, 3]); g.beginPath(); g.moveTo(L, ly); g.lineTo(R, ly); g.stroke(); g.setLineDash([]);
    g.fillStyle = last.c >= last.o ? up : dn; g.fillRect(R + 2, ly - 9, 68, 18); g.fillStyle = "#fff"; g.textAlign = "left"; g.fillText(fmtNum(last.c, last.c > 100 ? 0 : 2), R + 8, ly);
    timeAxis(g, L, R, B, ms[i0], ms[Math.max(i0, i1 - 1)]);
    hover(c, vb.length, x, (k, py) => { const b = vb[k], prev = vb[k - 1] || b, ch = prev.c ? b.c / prev.c - 1 : 0, here = vis.filter((m) => m.i === k);
      const price = lo + (hi - lo) * (B - py) / (B - T);
      return `<div class="tt">${esc(b.t.slice(0, 16).replace("T", " "))} · ${fmtNum(price, price > 100 ? 0 : 2)}</div>افتتاح ${fmtNum(b.o)} · أعلى ${fmtNum(b.h)}<br>أدنى ${fmtNum(b.l)} · إغلاق <b>${fmtNum(b.c)}</b> <span class="${sign(ch)}">${fmtSPct(ch, 2)}</span>` +
        here.map((m) => `<br><b class="${m.buy ? "good" : "bad"}">${m.buy ? "▲ دخول" : "▼ خروج"} عند ${fmtNum(m.p)}</b>${!m.buy && isNum(m.t.pnl_quote) ? ` <span class="${sign(m.t.pnl_quote)}">${fmtNum(m.t.pnl_quote)}</span>` : ""}`).join(""); });
    interact(c, view, L, () => c.clientWidth - 72);
  };
  draw(); if (view) view.subs.push(draw);
  return draw;
}
function lines(c, series, h = 240, opt = {}, view = null) {
  const draw = () => {
    const { g, w } = setupCanvas(c, h);
    series.forEach((s) => { s._ms = s._ms || s.points.map((p) => msOf(p[0])); });
    const L = 8, R = w - 72, T = 10, B = h - 26;
    const t0 = view ? view.t0 : Math.min(...series.map((s) => s._ms[0])), t1 = view ? view.t1 : Math.max(...series.map((s) => s._ms[s._ms.length - 1]));
    const tx = (t) => L + (t - t0) / ((t1 - t0) || 1) * (R - L);
    const vis = series.map((s) => { const out = []; for (let i = 0; i < s.points.length; i++) { const t = s._ms[i];
      if (t >= t0 && t <= t1) out.push([t, s.points[i][1]]); else if (t < t0 && s._ms[i + 1] > t0) out.push([t0, s.points[i][1]]); } return out; });
    if (opt.rebase && vis[0].length) {   // zoomed in: every line starts where the first one does, so the window compares fairly
      const base = vis[0][0][1];
      vis.forEach((pts, si) => { if (si && pts.length && pts[0][1]) { const f = base / pts[0][1]; pts.forEach((p) => { p[1] *= f; }); } });
    }
    const useLog = opt.log && vis.flat().every((p) => p[1] > 0), tf = useLog ? Math.log : (v) => v, inv = useLog ? Math.exp : (v) => v;
    const all = vis.flat().map((p) => tf(p[1])).filter(isNum); if (!all.length) return;
    let lo = Math.min(...all), hi = Math.max(...all); if (opt.zeroTop) hi = Math.max(0, hi); const pad = (hi - lo) * 0.06 || 0.01; lo -= pad; hi += opt.zeroTop ? 0 : pad;
    const fmt = opt.pct ? (v) => fmtPct(v, 0) : (v) => fmtNum(v, 0);
    const ticks = useLog ? niceTicks(inv(lo), inv(hi)).map(tf) : null;
    const y = grid(g, L, R, T, B, lo, hi, (v) => fmt(inv(v)), ticks);
    vis.forEach((pts, si) => { const s = series[si]; if (!pts.length) return;
      const path = () => { g.beginPath(); pts.forEach((p, i) => { i ? g.lineTo(tx(p[0]), y(tf(p[1]))) : g.moveTo(tx(p[0]), y(tf(p[1]))); }); };
      if (s.area) { const base = opt.zeroTop ? y(0) : B, gr = g.createLinearGradient(0, T, 0, B); gr.addColorStop(0, s.area[0]); gr.addColorStop(1, s.area[1]);
        path(); g.lineTo(tx(pts[pts.length - 1][0]), base); g.lineTo(tx(pts[0][0]), base); g.closePath(); g.fillStyle = gr; g.fill(); }
      path(); g.strokeStyle = s.color; g.lineWidth = s.width || 2; g.setLineDash(s.dash ? [5, 4] : []); g.stroke(); g.setLineDash([]); g.lineWidth = 1; });
    const p0 = vis[0]; if (!p0.length) return;
    const lastP = p0[p0.length - 1][1], ly = y(tf(lastP));
    g.fillStyle = series[0].color; g.fillRect(R + 2, ly - 9, 68, 18); g.fillStyle = "#fff"; g.textAlign = "left"; g.fillText(fmt(lastP), R + 8, ly);
    timeAxis(g, L, R, B, t0, t1);
    const near = (pts, t) => { let b = 0, bd = Infinity; for (let i = 0; i < pts.length; i++) { const d = Math.abs(pts[i][0] - t); if (d < bd) { bd = d; b = i; } } return pts[b]; };
    hover(c, p0.length, (i) => tx(p0[i][0]), (i) => { const t = p0[i][0];
      return `<div class="tt">${esc(new Date(t).toISOString().slice(0, 16).replace("T", " "))}</div>` +
        vis.map((pts, si) => { const p = near(pts, t); return p ? `<span style="color:${series[si].color}">●</span> ${esc(series[si].name || "")} <b>${opt.pct ? fmtPct(p[1], 1) : fmtNum(p[1])}</b>` : ""; }).join("<br>"); });
    interact(c, view, L, () => c.clientWidth - 72);
  };
  draw(); if (view) view.subs.push(draw);
  return draw;
}
function rangeButtons(view, id) {
  const D = 86400e3, opts = [["الكل", 0], ["سنتين", 730 * D], ["سنة", 365 * D], ["6 شهور", 182 * D], ["3 شهور", 91 * D], ["شهر", 30 * D]].filter(([, s]) => !s || s < view.tmax - view.tmin);
  const el = $("#" + id); if (!el) return;
  el.innerHTML = opts.map(([n, s]) => `<button data-span="${s}" class="${s ? "" : "on"}">${n}</button>`).join("");
  el.onclick = (e) => { const b = e.target.closest("button"); if (!b) return; el.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); if (+b.dataset.span) view.last(+b.dataset.span); else view.reset(); };
}
function spark(c, values, color) {
  if (!c || values.length < 2) return;
  const dpr = window.devicePixelRatio || 1, w = c.clientWidth, h = c.clientHeight; c.width = w * dpr; c.height = h * dpr;
  const g = c.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0);
  const lo = Math.min(...values), hi = Math.max(...values), y = yScale(lo, hi, 3, h - 3), x = (i) => i / (values.length - 1) * w;
  const gr = g.createLinearGradient(0, 0, 0, h); gr.addColorStop(0, color + "55"); gr.addColorStop(1, color + "00");
  g.beginPath(); values.forEach((v, i) => i ? g.lineTo(x(i), y(v)) : g.moveTo(0, y(v))); g.lineTo(w, h); g.lineTo(0, h); g.closePath(); g.fillStyle = gr; g.fill();
  g.beginPath(); values.forEach((v, i) => i ? g.lineTo(x(i), y(v)) : g.moveTo(0, y(v))); g.strokeStyle = color; g.lineWidth = 1.6; g.stroke();
}
const thin = (arr, n = 80) => { const step = Math.max(1, Math.ceil(arr.length / n)); return arr.filter((_, i) => i % step === 0 || i === arr.length - 1); };
function heat(monthly) {
  if (!monthly.length) return empty("ما فيه بيانات شهرية");
  const years = {}; monthly.forEach((m) => { const [yy, mm] = m.month.split("-"); (years[yy] ||= {})[+mm] = m.return; });
  const names = AR.months;
  const col = (v) => { const a = Math.min(1, Math.abs(v) / 0.25); return v >= 0 ? `rgba(22,199,132,${0.14 + a * 0.75})` : `rgba(234,57,67,${0.14 + a * 0.75})`; };
  let html = `<div class="heatw"><div class="heat"><div></div>${names.map((n) => `<div class="h">${n}</div>`).join("")}<div class="h">السنة</div>`;
  Object.keys(years).sort().forEach((yy) => { const row = years[yy]; let tot = 1;
    html += `<div class="y">${yy}</div>`;
    for (let m = 1; m <= 12; m++) { const v = row[m]; if (v === undefined) { html += `<div class="c e"></div>`; continue; } tot *= 1 + v;
      html += `<div class="c" style="background:${col(v)}" title="${AR.months[m - 1]} ${yy}: ${fmtSPct(v)}">${(v * 100).toFixed(1)}</div>`; }
    html += `<div class="t ${sign(tot - 1)}">${fmtSPct(tot - 1, 0)}</div>`; });
  return html + "</div></div>";
}
function sma(bars, n) { const out = []; let s = 0; bars.forEach((b, i) => { s += b.c; if (i >= n) s -= bars[i - n].c; out.push(i >= n - 1 ? s / n : null); }); return out; }
const posText = (p) => { if (!p) return "—"; const e = Object.entries(p).filter(([, v]) => isNum(v) && +v !== 0); return e.length ? e.map(([k, v]) => `${fmtNum(v, 6)} ${esc(k)}`).join(" · ") : "بدون مركز"; };

/* ---------- pages ---------- */
const PAGES = {};
PAGES.home = async () => {
  const d = await api("/api/home");
  // a session with no live decision yet still holds its starting capital (its equity is not reported yet)
  const equity = d.sessions.reduce((a, s) => a + (isNum(s.equity) ? +s.equity : +s.capital), 0);
  const pnl = equity - d.capital_total, ret = d.capital_total ? pnl / d.capital_total : null;
  const live = d.sessions.find((s) => s.running) || d.sessions[0];
  main.innerHTML = header("الرئيسية", "نظرة سريعة على جلساتك الورقية وآخر اختباراتك",
    chip(`${d.running} جلسة تعمل`, d.running ? "good" : "") + chip("محاكاة فقط · بدون مفاتيح", ""))
  + `<div class="card demo-cta"><div><h2>الوضع التجريبي</h2><div class="muted" style="font-size:13px;margin-top:4px">جرّب أي استراتيجية الحين على أسعار تاريخية حقيقية، بدون فلوس وبدون إنترنت، وشوف أسباب كل قرار.</div></div>
    <button class="primary" data-nav="demo">▶ ابدأ الوضع التجريبي</button></div>
  <div class="kpis">
    ${kpi({ k: "الرصيد الورقي", v: d.capital_total ? fmtUsd(equity) : "—", ic: "wallet", s: `رأس المال ${fmtUsd(d.capital_total, 0)}` })}
    ${kpi({ k: "الربح / الخسارة", v: d.capital_total ? (pnl > 0 ? "+" : "") + fmtUsd(pnl) : "—", cls: sign(pnl), ic: pnl >= 0 ? "trend" : "down", tone: pnl >= 0 ? "g" : "r", s: fmtSPct(ret, 2) })}
    ${kpi({ k: "جلسات تعمل", v: `${d.running} / ${d.sessions.length}`, ic: "play", tone: d.running ? "g" : "" })}
    ${kpi({ k: "مراكز مفتوحة", v: String(d.open_positions.length), ic: "box", tone: d.open_positions.length ? "w" : "" })}
    ${kpi({ k: "تجارب الاختبار", v: String(d.experiments), ic: "flask", s: "كل تجربة ترفع عتبة حكم المتانة" })}
  </div>
  <div class="cols">
    <div class="card"><div class="card-h"><h2>${live ? `السوق الآن <small>${esc(live.symbol)} · ${esc(tfAr(live.timeframe))} · من جلسة ${esc(live.name)}</small>` : "السوق الآن"}</h2></div>
      <div id="hmk">${live ? loading("تحميل الشموع…") : empty("شغّل جلسة ورقية عشان تشوف السوق هنا", "play")}</div></div>
    <div class="card"><div class="card-h"><h2>آخر اختبار</h2></div>${d.last_run ? runSummary(d.last_run) : empty("ما فيه اختبارات بعد", "flask")}</div>
  </div>
  <div class="card"><div class="card-h"><h2>الجلسات الورقية</h2><button class="sm" data-nav="paper">إدارة الجلسات</button></div>${sessionTable(d.sessions)}</div>
  ${note("كل الأرقام هنا ورقية (محاكاة على أسعار Binance العامة). ما فيه اتصال بحساب حقيقي، والتداول الحقيقي معطّل برمجيًا.")}`;
  if (live) api("/api/paper/session?name=" + encodeURIComponent(live.name)).then((s) => {
    const el = $("#hmk"); if (!el) return; el.innerHTML = `<canvas id="hc"></canvas>`;
    candles($("#hc"), s.bars.slice(-120), [], [{ values: sma(s.bars.slice(-120), 20), color: css("--sma1") }], 280); }).catch(() => {});
  timer = setInterval(() => { if (current === "home") PAGES.home().catch(() => {}); }, 30000);
};
function sessionTable(ss) {
  if (!ss.length) return empty("ما فيه جلسات. أنشئ وحدة من صفحة التداول الورقي.", "play");
  return `<div class="tw"><table><thead><tr><th>الجلسة</th><th>الاستراتيجية</th><th>الحالة</th><th>الرصيد</th><th>العائد</th><th>المركز</th><th>عمليات التنفيذ</th><th>فائتة</th><th>آخر دورة</th></tr></thead><tbody>
  ${ss.map((s) => `<tr><td><a href="#" data-session="${esc(s.name)}"><b>${esc(s.name)}</b></a></td><td>${esc(s.strategy)} <span class="muted">· ${esc(s.symbol)} · ${esc(tfAr(s.timeframe))}</span></td>
  <td>${s.running && s.stopping ? '<span class="pill warn">يتوقف…</span>' : s.running ? '<span class="pill good">تعمل</span>' : '<span class="pill">متوقفة</span>'}${s.error ? ' <span class="pill bad" title="' + esc(trErr(s.error.error).ar || "خطأ في الجلسة") + '">خطأ</span>' : ""}</td>
  <td class="ltr">${isNum(s.equity) ? fmtUsd(s.equity) : `<span class="muted">${fmtUsd(s.capital)}</span>`}</td><td class="ltr ${sign(s.net_return)}">${fmtSPct(s.net_return, 2)}</td>
  <td>${posText(s.position)}</td><td>${s.fills ?? "—"}</td><td>${s.missed_bars ?? "—"}</td><td class="muted ltr">${esc((s.last_cycle || "—").slice(11, 19))}</td></tr>`).join("")}</tbody></table></div>`;
}
document.addEventListener("click", (e) => { const a = e.target.closest("[data-session]"); if (a) { e.preventDefault(); paperDetail(a.dataset.session); } });
document.addEventListener("click", (e) => { const a = e.target.closest("[data-nav]"); if (a) { e.preventDefault(); nav(a.dataset.nav); } });
document.addEventListener("click", (e) => { const a = e.target.closest("[data-dc-source]"); if (!a || typeof dcState === "undefined") return;
  e.preventDefault(); Object.assign(dcState.filters, { source: a.dataset.dcSource, strategy: "", symbol: "", kinds: [] }); dcState.selected = null; nav("decisions"); });
function runSummary(r) {
  const edge = r.net_return - r.hold_return;
  return `<div class="muted" style="margin-bottom:10px">${esc(r.strategy)} · ${r.mode === "engine" ? "كامل عبر المحرك" : "سريع"} · <span class="ltr">${esc(r.at.slice(0, 16).replace("T", " "))}</span></div>
  <div class="big ${sign(r.net_return)} ltr" style="text-align:right">${fmtSPct(r.net_return)}</div>
  <div class="kv" style="margin-top:10px"><div>الاحتفاظ بنفس الفترة</div><div>${fmtSPct(r.hold_return)}</div>
  <div>الفرق عن الاحتفاظ</div><div class="${sign(edge)}">${fmtSPct(edge)}</div><div>أقصى تراجع</div><div class="bad">${fmtPct(r.max_drawdown)}</div>
  <div>نسبة شارب</div><div>${fmtNum(r.sharpe)}</div><div>حكم المتانة</div><div>${verdictPill(r.verdict)}</div></div>
  <div class="actions"><button class="primary sm" data-run="${esc(r.id)}">عرض النتيجة كاملة</button></div>`;
}
document.addEventListener("click", (e) => { const b = e.target.closest("[data-run]"); if (b) { nav("backtest"); setTimeout(() => api("/api/run?id=" + b.dataset.run).then(showResult), 300); } });

PAGES.strategies = async () => {
  const d = await api("/api/strategies");
  const ok = d.strategies.filter((s) => s.ok).length;
  main.innerHTML = header("الاستراتيجيات", "أضف استراتيجيتك وافحصها قبل الاختبار", chip(`${ok} تعمل`, "good") + chip(`${d.strategies.length - ok} مرفوضة`, d.strategies.length - ok ? "bad" : ""))
  + `<div class="cols"><div class="card"><div class="card-h"><h2>إضافة استراتيجية</h2></div>
    <p class="muted" style="margin-top:0">ملف Python فيه صنف يرث من <code>Strategy</code>. يُفحص قبل الحفظ، والملف المرفوض ما ينحفظ.</p>
    <div class="drop">${svg("layers")}<label class="filebtn"><input type="file" id="up" accept=".py" hidden>اختر ملف .py</label><span id="upname" class="muted">ما اخترت ملف</span><button class="primary" id="upb">رفع وفحص</button></div><div id="upr"></div></div>
  <div class="card"><div class="card-h"><h2>فحص بمسار <small>Freqtrade أو ملف</small></h2></div>
    <label>المسار</label><input id="spec" class="ltr" placeholder="freqtrade:C:\\path\\MyStrategy.py:MyStrategy">
    <div class="actions"><button id="chk">فحص</button></div><div id="chr"></div></div></div>
  <div class="card"><div class="card-h"><h2>الاستراتيجيات المركّبة <small>${d.strategies.length}</small></h2></div>
  <div class="tw"><table><thead><tr><th>الاسم</th><th>الإطار</th><th>التهيئة</th><th>المصدر</th><th>الحالة</th><th></th></tr></thead><tbody>
  ${d.strategies.map((s) => `<tr><td><b>${esc(s.name || s.file)}</b><div class="muted" style="font-size:11.5px">${esc(s.file)}</div></td><td><span class="pill">${esc(tfAr(s.timeframe))}</span></td><td>${s.warmup_bars ?? "—"} شمعة</td><td>${s.folder === "workspace" ? "مضافة" : "أمثلة"}</td>
  <td>${s.ok ? '<span class="pill good">تعمل</span>' : '<span class="pill bad" title="' + esc(s.error) + '">مرفوضة</span>'}</td>
  <td class="wrap">${s.ok ? `<button class="sm primary" data-bt="${esc(s.spec)}">اختبار</button> <button class="sm" data-pp="${esc(s.spec)}">ورقي</button>` : `<span class="muted">${esc(s.error || "")}</span>`}
  ${s.removable ? ` <button class="sm danger" data-rm="${esc(s.file)}">حذف</button>` : ""}</td></tr>`).join("")}</tbody></table></div></div>`;
  $("#up").onchange = () => { $("#upname").textContent = $("#up").files[0]?.name || "ما اخترت ملف"; };
  $("#upb").onclick = async () => { const f = $("#up").files[0]; if (!f) { $("#upr").innerHTML = errBox("اختر ملف .py"); return; }
    try { const r = await api("/api/strategies/upload", { filename: f.name, content: await f.text() }); await PAGES.strategies(); $("#upr").innerHTML = `<div class="okmsg">انضافت: ${esc(r.file)}</div>`; }
    catch (e) { $("#upr").innerHTML = errBox(e); } };
  $("#chk").onclick = async () => { try { const r = await api("/api/strategies/check", { spec: $("#spec").value });
    $("#chr").innerHTML = `<div class="okmsg">تعمل: ${esc(r.name)} · ${esc(tfAr(r.timeframe))} · ${esc(countAr(r.warmup_bars, "شمعة تهيئة واحدة", "شمعتا تهيئة", "شموع تهيئة", "شمعة تهيئة"))}</div>`; } catch (e) { $("#chr").innerHTML = errBox(e); } };
  document.querySelectorAll("[data-rm]").forEach((b) => b.onclick = async () => { if (!(await uiConfirm("تحذف " + b.dataset.rm + " من مساحة العمل؟", "حذف"))) return;
    await api("/api/strategies/remove", { filename: b.dataset.rm }); PAGES.strategies(); });
  document.querySelectorAll("[data-bt]").forEach((b) => b.onclick = () => { sessionStorage.setItem("spec", b.dataset.bt); nav("backtest"); });
  document.querySelectorAll("[data-pp]").forEach((b) => b.onclick = () => { sessionStorage.setItem("spec", b.dataset.pp); nav("paper"); });
};

PAGES.backtest = async () => {
  window.__last = null;
  const [st, df, risk] = await Promise.all([api("/api/strategies"), api("/api/data"), api("/api/risk")]);
  const pre = sessionStorage.getItem("spec") || "";
  const okS = st.strategies.filter((s) => s.ok);
  main.innerHTML = header("الاختبار التاريخي", "جرّب الاستراتيجية على بيانات الماضي مع الرسوم والانزلاق، وقارنها بالاحتفاظ",
    chip("الفترة المختومة من 2025-09-07 ما تنقرأ", ""))
  + `<div class="card"><div class="form">
    <div><label>الاستراتيجية</label><select id="bs">${okS.map((s) => `<option value="${esc(s.spec)}" data-tf="${esc(s.timeframe)}" ${s.spec === pre ? "selected" : ""}>${esc(s.name)} (${esc(tfAr(s.timeframe))})</option>`).join("")}</select></div>
    <div><label>البيانات</label><select id="bd">${df.files.map((f) => `<option value="${esc(f.path)}" data-tf="${esc(f.timeframe || "")}">${esc(f.name)}</option>`).join("")}</select></div>
    <div><label>من</label><input id="bfrom" type="date" value="2020-01-01"></div>
    <div><label>إلى (قبل 2025-09-07)</label><input id="bto" type="date" value="2025-09-06" max="2025-09-06"></div>
    <div><label>رأس المال</label><input id="bcap" class="ltr" value="${esc(risk.editable.capital)}"></div>
    <div><label>الرسوم لكل جانب</label><input id="bfee" class="ltr" value="${esc(risk.editable.fee)}"></div>
    <div><label>الانزلاق (نقطة أساس، 1 = 0.01%)</label><input id="bslip" class="ltr" value="${esc(risk.editable.slippage_bps)}"></div>
    <div><label>معاملات (JSON)</label><input id="bpar" class="ltr" placeholder='{"fast": 20, "slow": 50}'></div>
  </div><div class="actions"><button class="primary" id="bfast">▶ اختبار سريع</button><button id="beng">كامل عبر المحرك</button><span class="muted" style="font-size:12.5px">السريع يطابق المحرك، والكامل يمر على كل طبقات الأمان (أبطأ)</span></div>
  <div id="bmsg"></div></div><div id="bres"></div>`;
  const go = async (engine) => {
    let params = {}; try { params = $("#bpar").value ? JSON.parse($("#bpar").value) : {}; if (!params || typeof params !== "object" || Array.isArray(params)) throw 0; }
    catch { $("#bmsg").innerHTML = errBox('المعاملات لازم تكون بصيغة JSON بين قوسين { }، مثل: {"fast": 20, "slow": 50}'); return; }
    const body = { spec: $("#bs").value, data: $("#bd").value, start: $("#bfrom").value || null, end: $("#bto").value || null,
      capital: $("#bcap").value, fee: $("#bfee").value, slippage_bps: $("#bslip").value, params, engine };
    $("#bfast").disabled = $("#beng").disabled = true;
    $("#bmsg").innerHTML = loading(engine ? "جاري الاختبار الكامل عبر المحرك (قد ياخذ دقائق)…" : "جاري الاختبار السريع…");
    try { const { job } = await api("/api/backtest", body);
      for (;;) { await new Promise((r) => setTimeout(r, 1000)); const j = await api("/api/job?id=" + job);
        if (j.state === "done") { $("#bmsg").innerHTML = ""; showResult(j.result); break; }
        if (j.state === "error") { $("#bmsg").innerHTML = errBox(j.error); break; } }
    } catch (e) { $("#bmsg").innerHTML = errBox(e); }
    $("#bfast").disabled = $("#beng").disabled = false; };
  const matchData = () => { const tf = $("#bs").selectedOptions[0]?.dataset.tf;
    const opt = [...$("#bd").options].find((o) => o.dataset.tf === tf); if (opt) $("#bd").value = opt.value;
    $("#bmsg").innerHTML = opt || !tf ? "" : `<div class="err">ما فيه ملف بيانات بإطار ${esc(tf)}. أضف ملف في مجلد data بمساحة العمل.</div>`; };
  $("#bs").onchange = matchData; matchData();
  $("#bfast").onclick = () => go(false); $("#beng").onclick = () => go(true);
};
function showResult(r, scroll = true) {
  window.__last = r;
  const m = r.metrics, b = r.benchmark, v = r.verdict;
  const passed = v.checks.filter((c) => c.ok === true).length, total = v.checks.length;
  const edge = m.net_return - b.net_return, good = v.verdict === "promising";
  const holdCap = r.equity.length ? r.equity[0][1] : 1, first = r.bars.length ? r.bars[0].c : 1;
  const trades = r.trades.map((t, i) => ({ ...t, _i: i, _pct: isNum(t.exit_price) && +t.entry_price ? t.exit_price / t.entry_price - 1 : null }));
  const wins = trades.filter((t) => +t.pnl_quote > 0), losses = trades.filter((t) => isNum(t.pnl_quote) && +t.pnl_quote <= 0);
  const best = trades.reduce((a, t) => (isNum(t.pnl_quote) && (!a || +t.pnl_quote > +a.pnl_quote) ? t : a), null);
  const worst = trades.reduce((a, t) => (isNum(t.pnl_quote) && (!a || +t.pnl_quote < +a.pnl_quote) ? t : a), null);
  $("#bres").innerHTML = `
  <div class="card-h" style="margin:6px 0 12px"><h2 style="font-size:18px">${esc(r.strategy)} <small>${esc(tfAr(r.timeframe))} · ${r.mode === "engine" ? "كامل عبر المحرك" : "سريع"} · <span class="ltr">${esc(r.start.slice(0, 10))} → ${esc(r.end.slice(0, 10))}</span> · ${fmtNum(r.bars_count, 0)} شمعة</small></h2>${verdictPill(v.verdict)}</div>
  <div class="kpis">
    ${kpi({ k: "العائد الصافي", v: fmtSPct(m.net_return), cls: sign(m.net_return), ic: m.net_return >= 0 ? "trend" : "down", tone: m.net_return >= 0 ? "g" : "r", s: `الاحتفاظ ${fmtSPct(b.net_return)}`, spark: "sp1" })}
    ${kpi({ k: "الفرق عن الاحتفاظ", v: fmtSPct(edge), cls: sign(edge), ic: "pct", tone: edge >= 0 ? "g" : "r", s: edge >= 0 ? "أحسن من الشراء والاحتفاظ" : "أقل من الشراء والاحتفاظ" })}
    ${kpi({ k: "نسبة شارب", v: fmtNum(m.sharpe), ic: "gauge", s: `الاحتفاظ ${fmtNum(b.sharpe)}`, tone: (m.sharpe || 0) > (b.sharpe || 0) ? "g" : "w" })}
    ${kpi({ k: "أقصى تراجع", v: fmtPct(m.max_drawdown), cls: "bad", ic: "down", tone: "r", s: `الاحتفاظ ${fmtPct(b.max_drawdown)}`, spark: "sp2" })}
    ${kpi({ k: "الصفقات · نسبة الربح", v: `${m.round_trips} · ${fmtPct(m.win_rate, 0)}`, ic: "box", s: `وقت في السوق ${fmtPct(m.time_in_market, 0)}` })}
    ${kpi({ k: "الرسوم + الانزلاق", v: fmtUsd(m.fees_quote + m.slippage_cost_quote_est), ic: "coins", tone: "w", s: `من رأس مال ${fmtUsd(r.settings.capital_quote, 0)}` })}
  </div>
  <div class="card"><div class="card-h"><h2>الشموع والصفقات</h2><div class="tabs" id="rng"></div></div>
    <div class="card-h" style="margin-bottom:8px"><div class="legend"><span><i class="tri-up"></i>دخول</span><span><i class="tri-dn"></i>خروج</span><span><i style="background:var(--sma1)"></i>متوسط متحرك 20</span><span><i style="background:var(--sma2)"></i>متوسط متحرك 50</span></div>
    <span class="muted" style="font-size:12px">🖱 العجلة للتكبير · اسحب للتنقّل · نقرتين للكل · الرسوم تحت تتحرك معه</span></div>
    <canvas id="cc"></canvas></div>
  <div class="cols">
    <div class="card"><div class="card-h"><h2>رأس المال <small>الاستراتيجية مقابل الاحتفاظ</small></h2><div class="tabs" id="scale"><button class="on" data-log="0">عادي</button><button data-log="1">لوغاريتمي</button></div></div>
      <div class="legend" style="margin-bottom:8px"><span><i style="background:var(--accent)"></i>الاستراتيجية</span><span><i class="dash"></i>الاحتفاظ</span><span class="muted">· الخطّين يبدون من نفس النقطة في أي فترة تختارها</span></div><canvas id="ce"></canvas>
      <div class="card-h" style="margin:14px 0 6px"><h2>التراجع</h2></div><canvas id="cd"></canvas></div>
    <div class="stack">
    <div class="card"><div class="card-h"><h2>حكم المتانة</h2><small class="muted">هل الربح حقيقي أو حظ؟</small></div>
      <div class="verdict"><div class="ring" style="--p:${passed / total * 100};--ringc:${good ? "var(--good)" : "var(--warn)"}"><div><b>${passed}/${total}</b><small>فحوص ناجحة</small></div></div>
      <div><div class="vlabel ${good ? "good" : "warn"}">${esc(v.label)}</div><div class="muted" style="font-size:12.5px">${esc(v.note)}</div></div></div>
      <div class="checks">${v.checks.map((c) => `<details class="check"><summary><div class="ok ${c.ok === null ? "u" : c.ok ? "y" : "n"}">${c.ok === null ? "؟" : c.ok ? "✓" : "✕"}</div><div>${esc(c.label)}</div><div class="d">${esc(c.detail)}</div></summary><div class="why">${esc(CHECK_HELP[c.key] || "")}</div></details>`).join("")}</div></div>
    <div class="card"><div class="card-h"><h2>الصفقات بالأرقام</h2></div><div class="kv">
      <div>رابحة / خاسرة</div><div><span class="good">${wins.length}</span> / <span class="bad">${losses.length}</span></div>
      <div>متوسط الربح</div><div class="good">${fmtNum(wins.reduce((a, t) => a + +t.pnl_quote, 0) / (wins.length || 1))}</div>
      <div>متوسط الخسارة</div><div class="bad">${fmtNum(losses.reduce((a, t) => a + +t.pnl_quote, 0) / (losses.length || 1))}</div>
      <div>أفضل صفقة</div><div>${best ? `<a href="#" data-jump="${best._i}" class="good">${fmtNum(best.pnl_quote)}</a>` : "—"}</div>
      <div>أسوأ صفقة</div><div>${worst ? `<a href="#" data-jump="${worst._i}" class="bad">${fmtNum(worst.pnl_quote)}</a>` : "—"}</div>
      <div>رأس المال · الرسوم · الانزلاق</div><div>${fmtUsd(r.settings.capital_quote, 0)} · ${fmtPct(+r.settings.taker_fee, 2)} · ${fmtPct(+r.settings.slippage_bps / 10000, 2)}</div>
      <div>المعاملات</div><div>${esc(JSON.stringify(r.params || {}))}</div></div></div>
    </div>
  </div>
    <div class="card"><div class="card-h"><h2>العائد الشهري <small>مرّر على أي شهر · العمود الأخير للسنة</small></h2></div>${heat(r.monthly)}</div>
  <div class="card"><div class="card-h"><h2>الصفقات <small>${trades.length} · اضغط على صفقة تشوفها على الرسم</small></h2>
    <div class="tabs" id="tf"><button class="on" data-f="all">الكل</button><button data-f="win">رابحة</button><button data-f="loss">خاسرة</button><button data-f="open">مفتوحة</button></div></div>
    <div class="tw"><table class="sortable"><thead><tr><th data-k="entry_time">دخول ⇅</th><th>سعر</th><th data-k="exit_time">خروج ⇅</th><th>سعر</th><th>كمية</th><th data-k="pnl_quote">ربح/خسارة ⇅</th><th data-k="_pct">% ⇅</th></tr></thead><tbody id="tb"></tbody></table></div></div>`;
  // one time window for the three charts
  const ms = r.bars.map((x) => msOf(x.t)), view = makeView(ms[0], ms[ms.length - 1] || ms[0] + 1);
  rangeButtons(view, "rng");
  candles($("#cc"), r.bars, r.trades, [{ values: sma(r.bars, 20), color: css("--sma1") }, { values: sma(r.bars, 50), color: css("--sma2") }], 440, view);
  const accent = css("--accent"), bad = css("--bad");
  const eqSeries = [{ name: "الاستراتيجية", points: r.equity, color: accent, area: [accent + "55", accent + "00"] },
    { name: "الاحتفاظ", points: r.bars.map((x) => [x.t, holdCap * x.c / first]), color: css("--hold"), dash: true, width: 1.5 }];
  const eqOpt = { log: false, rebase: true };
  lines($("#ce"), eqSeries, 260, eqOpt, view);
  lines($("#cd"), [{ name: "التراجع", points: r.drawdown, color: bad, area: [bad + "10", bad + "66"], width: 1.5 }], 150, { pct: true, zeroTop: true }, view);
  $("#scale").onclick = (e) => { const bt = e.target.closest("button"); if (!bt) return; $("#scale").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === bt)); eqOpt.log = bt.dataset.log === "1"; view.subs.forEach((f) => f()); };
  spark($("#sp1"), thin(r.equity).map((p) => p[1]), m.net_return >= 0 ? css("--good") : bad);
  spark($("#sp2"), thin(r.drawdown).map((p) => p[1]), bad);
  // trades table: filter, sort, click to jump
  let filter = "all", sortK = "entry_time", sortDir = -1;
  const renderTrades = () => {
    const rows = trades.filter((t) => filter === "all" || (filter === "win" ? +t.pnl_quote > 0 : filter === "loss" ? isNum(t.pnl_quote) && +t.pnl_quote <= 0 : !t.exit_time))
      .sort((a, b2) => { const x = a[sortK], y2 = b2[sortK]; const nx = isNum(x) ? +x : x || "", ny = isNum(y2) ? +y2 : y2 || ""; return (nx > ny ? 1 : nx < ny ? -1 : 0) * sortDir; }).slice(0, 400);
    $("#tb").innerHTML = rows.map((t) => `<tr data-jump="${t._i}" style="cursor:pointer"><td class="ltr">${esc((t.entry_time || "").slice(0, 16).replace("T", " "))}</td><td class="ltr">${fmtNum(t.entry_price)}</td><td class="ltr">${t.exit_time ? esc(t.exit_time.slice(0, 16).replace("T", " ")) : '<span class="pill warn">مفتوحة</span>'}</td><td class="ltr">${fmtNum(t.exit_price)}</td><td class="ltr">${fmtNum(t.quantity, 5)}</td>
      <td class="ltr ${sign(t.pnl_quote)}">${isNum(t.pnl_quote) ? fmtNum(t.pnl_quote) : "—"}</td><td class="ltr ${sign(t._pct)}">${fmtSPct(t._pct, 2)}</td></tr>`).join("") || `<tr><td colspan="7" class="muted">ما فيه صفقات بهالتصفية</td></tr>`;
  };
  renderTrades();
  $("#tf").onclick = (e) => { const bt = e.target.closest("button"); if (!bt) return; filter = bt.dataset.f; $("#tf").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === bt)); renderTrades(); };
  $("#bres").querySelector("table.sortable thead").onclick = (e) => { const th = e.target.closest("th[data-k]"); if (!th) return; sortDir = sortK === th.dataset.k ? -sortDir : -1; sortK = th.dataset.k; renderTrades(); };
  $("#bres").addEventListener("click", (e) => { const el = e.target.closest("[data-jump]"); if (!el) return; e.preventDefault();
    const t = trades[+el.dataset.jump], a = msOf(t.entry_time), z = t.exit_time ? msOf(t.exit_time) : view.tmax, pad = Math.max((z - a) * 1.5, 86400e3 * 10);
    view.set(a - pad, z + pad); $("#rng").querySelectorAll("button").forEach((x) => x.classList.remove("on"));
    $("#cc").scrollIntoView({ behavior: "smooth", block: "center" });
    document.querySelectorAll("#tb tr").forEach((tr) => tr.classList.toggle("sel", tr.dataset.jump === el.dataset.jump)); });
  if (scroll) $("#bres").scrollIntoView({ behavior: "smooth", block: "start" });
}
const CHECK_HELP = {
  beats_hold: "لازم تكون نسبة شارب للاستراتيجية أعلى من مجرد الشراء والاحتفاظ بنفس الفترة والتكاليف. لو مو أعلى، الاحتفاظ أبسط وأحسن.",
  both_halves: "نقسم الفترة نصين: لازم الربح يكون موجب في الاثنين. لو كل الربح جاء من نص واحد، غالبًا صدفة فترة معيّنة.",
  fees_x2: "نعيد الاختبار بضعف الرسوم. لو الربح اختفى، الاستراتيجية هشّة وتعتمد على تكاليف مثالية.",
  out_of_sample: "أول 70% للحكم، وآخر 30% كأنها مستقبل ما شافته. لازم تنجح في الاثنين.",
  dsr: "نسبة شارب المصحَّحة: تعاقب كثرة التجارب. كل ما جرّبت أكثر، صار لازم الأداء أقوى عشان ما يكون حظ.",
};

PAGES.paper = async () => {
  const [d, st] = await Promise.all([api("/api/paper"), api("/api/strategies")]);
  const pre = sessionStorage.getItem("spec") || "";
  const run = d.sessions.filter((s) => s.running).length;
  main.innerHTML = header("التداول الورقي", "تشغيل الاستراتيجية على الأسعار الحية بدون فلوس حقيقية", chip(`${run} تعمل`, run ? "good" : "") + chip("Binance عامة · بدون مفاتيح", ""))
  + `<div class="card"><div class="card-h"><h2>الجلسات</h2></div>${sessionTable(d.sessions)}
  ${d.sessions.length ? `<div class="actions">${d.sessions.map((s) => s.running
    ? `<button class="sm danger" data-stop="${esc(s.name)}">■ إيقاف ${esc(s.name)}</button>` : `<button class="sm ok" data-start="${esc(s.name)}">▶ تشغيل ${esc(s.name)}</button>`).join("")}</div>` : ""}</div>
  <div id="pdet"></div>
  <div class="card"><div class="card-h"><h2>جلسة جديدة</h2></div>
  <div class="form"><div><label>الاسم</label><input id="pn" class="ltr" placeholder="btc-sma-1"></div>
  <div><label>الاستراتيجية</label><select id="ps">${st.strategies.filter((s) => s.ok).map((s) => `<option value="${esc(s.spec)}" ${s.spec === pre ? "selected" : ""}>${esc(s.name)} (${esc(tfAr(s.timeframe))})</option>`).join("")}</select></div>
  <div><label>الزوج</label><input id="psym" class="ltr" value="BTCUSDT"></div><div><label>رأس المال الورقي</label><input id="pcap" class="ltr" value="1000"></div></div>
  <div class="actions"><button class="primary" id="pcreate">إنشاء وتشغيل</button><span class="muted" style="font-size:12.5px">تشغيل الجلسة يعتبر تفويضًا منك لهالجلسة. ما ينرسل أي أمر لأي منصة.</span></div><div id="pmsg"></div></div>`;
  $("#pcreate").onclick = async () => { try { const name = $("#pn").value.trim();
    $("#pmsg").innerHTML = loading("جاري تحميل البيانات وإنشاء الجلسة…");
    await api("/api/paper/new", { name, spec: $("#ps").value, symbol: $("#psym").value.trim().toUpperCase(), base: $("#psym").value.trim().toUpperCase().replace(/USDT$/, ""), capital: $("#pcap").value });
    await api("/api/paper/start", { name }); PAGES.paper(); } catch (e) { $("#pmsg").innerHTML = errBox(e); } };
  document.querySelectorAll("[data-start]").forEach((b) => b.onclick = async () => { try { await api("/api/paper/start", { name: b.dataset.start }); }
    catch (e) { await PAGES.paper(); if ($("#pmsg")) $("#pmsg").innerHTML = errBox(e); return; } PAGES.paper(); });
  document.querySelectorAll("[data-stop]").forEach((b) => b.onclick = async () => { await api("/api/paper/stop", { name: b.dataset.stop }); PAGES.paper(); });
  const pick = d.sessions.find((s) => s.name === shownSession) || d.sessions.find((s) => s.running) || d.sessions[0];
  if (pick) paperDetail(pick.name);
};
let shownSession = null;
const paperZoom = {};
async function paperDetail(name) {
  shownSession = name;
  if (current !== "paper" || !$("#pdet")) { nav("paper"); return; }   // the paper page then shows this session
  const draw = async () => { const d = await api("/api/paper/session?name=" + encodeURIComponent(name)); const s = d.status || {}; const el = $("#pdet");
    if (!el || shownSession !== name) return;
    const lastBar = d.bars[d.bars.length - 1], prev = d.bars[d.bars.length - 2], ch = lastBar && prev ? lastBar.c / prev.c - 1 : null;
    const nextOpen = s.next_open ? s.next_open[0] : null;
    el.innerHTML = `<div class="card"><div class="card-h"><h2>${esc(name)} <small>${esc(d.session.strategy_name)} · ${esc(d.session.market.symbol)} · ${esc(tfAr(d.session.timeframe))}</small></h2>
      <div class="actions" style="margin:0">${d.running ? '<span class="pill good">تعمل · تحدّث كل 30 ثانية</span>' : '<span class="pill">متوقفة</span>'}
      <button class="sm" data-dc-source="paper:${esc(name)}">قراراتها في مركز القرارات ←</button></div></div>
    ${d.error ? `<div class="err"><b>الجلسة ما تقدر تجيب الأسعار</b> · آخر محاولة <span class="ltr">${esc(String(d.error.at).slice(0, 16).replace("T", " "))} غرينتش</span> · ${esc(d.error.consecutive)} محاولة متتالية فشلت.
      <br>السبب: ${esc(trErr(d.error.error).ar || "خطأ غير معروف")}. الشموع اللي تفوت وقتها تنسجّل «فائتة» وما تتداول.
      <details class="tech"><summary>تفاصيل تقنية</summary><pre>${esc(d.error.error)}</pre></details></div>` : ""}
    <div class="kpis">
      ${kpi({ k: "آخر سعر", v: lastBar ? fmtUsd(lastBar.c) : "—", ic: "trend", s: ch === null ? "" : `<span class="${sign(ch)}">${fmtSPct(ch, 2)}</span> عن الشمعة السابقة` })}
      ${kpi({ k: "الرصيد", v: isNum(s.equity_quote) ? fmtUsd(s.equity_quote) : fmtUsd(d.session.settings.capital_quote), ic: "wallet", s: isNum(s.equity_quote) ? "" : "قبل أول قرار حي" })}
      ${kpi({ k: "العائد", v: fmtSPct(s.net_return, 2), cls: sign(s.net_return), ic: "pct", tone: isNum(s.net_return) ? (s.net_return >= 0 ? "g" : "r") : "" })}
      ${kpi({ k: "المركز", v: posText(s.position), ic: "box", small: true })}
      ${kpi({ k: "شموع حيّة", v: String(s.live_bars ?? 0), ic: "clock", s: nextOpen ? `الشمعة الجاية تفتح <span class="ltr">${esc(nextOpen.slice(11, 16))} غرينتش</span>` : "", small: false })}
    </div>
    <div class="card-h"><h2>السعر <small>${d.bars.length} شمعة · العجلة للتكبير، اسحب للتنقّل</small></h2><div class="tabs" id="prng"></div></div>
    <canvas id="pc"></canvas>
    <div class="cols" style="margin-top:16px;margin-bottom:0"><div><h2 style="margin-bottom:10px">القرارات</h2>${d.decisions.length ? `<div class="tw" style="max-height:300px"><table><thead><tr><th>الوقت</th><th>القرار</th><th>السبب</th><th>المخاطر</th></tr></thead><tbody>
    ${d.decisions.map((x) => `<tr><td class="ltr">${esc(x.at.slice(0, 16).replace("T", " "))}</td><td>${x.action === "buy" ? '<span class="pill good">شراء</span>' : x.action === "sell" ? '<span class="pill bad">بيع</span>' : '<span class="pill">انتظار</span>'}</td>
    <td class="${x.rationale.startsWith("missed") ? "warn" : ""}">${esc(rationaleText(x.rationale))}</td><td>${esc(x.risk ? arOf(AR.riskDecision, x.risk) : "—")}</td></tr>`).join("")}</tbody></table></div>`
      : empty("ما فيه قرارات حيّة بعد. أول قرار بعد ما تقفل الشمعة الجاية.", "clock")}</div>
    <div><h2 style="margin-bottom:10px">السجل</h2><pre class="rtl-log">${esc(d.log.slice(-25).map(logLineAr).join("\n"))}</pre></div></div></div>`;
    const bars = d.bars; if (!bars.length) { candles($("#pc"), bars, [], [], 340); return; }
    const ms = bars.map((x) => msOf(x.t)), view = makeView(ms[0], ms[ms.length - 1]);
    const keep = paperZoom[name];                       // keep the user's zoom across the 30 s refresh
    rangeButtons(view, "prng");
    candles($("#pc"), bars, s.trades || [], [{ values: sma(bars, 20), color: css("--sma1") }], 360, view);
    if (keep) view.set(view.tmax - keep[0], view.tmax - keep[1]); else view.set(ms[Math.max(0, ms.length - 120)], view.tmax);
    view.subs.push(() => { paperZoom[name] = [view.tmax - view.t0, view.tmax - view.t1]; }); };
  await draw(); clearInterval(timer); timer = setInterval(() => { if (current === "paper") draw().catch(() => {}); }, 30000);
}

PAGES.risk = async () => {
  const d = await api("/api/risk");
  main.innerHTML = header("المخاطر والإعدادات", "إعدادات الاختبار والتداول الورقي، والحدود اللي يطبقها المحرك", chip("الإنتاج للعرض فقط", "bad"))
  + `<div class="cols"><div class="card"><div class="card-h"><h2>إعدادات الاختبار والورقي <small>قابلة للتعديل</small></h2></div><div class="form">
  <div><label>رأس المال الافتراضي</label><input id="rcap" class="ltr" value="${esc(d.editable.capital)}"></div>
  <div><label>الرسوم لكل جانب</label><input id="rfee" class="ltr" value="${esc(d.editable.fee)}"></div>
  <div><label>الانزلاق (نقطة أساس، 1 = 0.01%)</label><input id="rslip" class="ltr" value="${esc(d.editable.slippage_bps)}"></div>
  <div><label>حد أقصى للمركز (اختياري)</label><input id="rmax" class="ltr" value="${esc(d.editable.max_position ?? "")}"></div></div>
  <label style="margin-top:12px">حدود مخاطر إضافية للمحرك (JSON، مثل {"max_order_notional_quote": "500"})</label>
  <textarea id="rov" rows="3" class="ltr">${esc(JSON.stringify(d.editable.risk_overrides || {}))}</textarea>
  <div class="actions"><button class="primary" id="rsave">حفظ</button></div><div id="rmsg"></div></div>
  <div class="card"><div class="card-h"><h2>قواعد ثابتة</h2></div><ul class="rules">${d.rules.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
  <div class="card-h" style="margin-top:18px"><h2>المسار الإنتاجي</h2></div>
  <p style="margin:0 0 6px">${d.production.mainnet_enabled ? '<span class="pill bad">مفعّل!</span>' : '<span class="pill bad">التداول الحقيقي معطّل برمجيًا</span>'}</p><p class="muted" style="margin:0">${esc(d.production.note)}</p></div></div>
  <div class="card"><div class="card-h"><h2>الحدود الفعلية اللي يطبقها المحرك</h2></div><div class="kv">${Object.entries(d.effective_engine_limits).map(([k, v]) => `<div>${esc(arOf(AR.limits, k))} <code class="ltr">${esc(k)}</code></div><div>${esc(v === true ? "مفعّل" : v === false ? "غير مفعّل" : v)}</div>`).join("")}</div></div>`;
  $("#rsave").onclick = async () => { let ov; try { ov = JSON.parse($("#rov").value || "{}"); if (!ov || typeof ov !== "object" || Array.isArray(ov)) throw 0; }
    catch { $("#rmsg").innerHTML = errBox('الحدود الإضافية لازم تكون بصيغة JSON بين قوسين { }، مثل: {"max_order_notional_quote": "500"}'); return; }
    try { await api("/api/risk", { capital: $("#rcap").value, fee: $("#rfee").value, slippage_bps: $("#rslip").value, max_position: $("#rmax").value || null, risk_overrides: ov });
      $("#rmsg").innerHTML = '<div class="okmsg">انحفظ.</div>'; } catch (e) { $("#rmsg").innerHTML = errBox(e); } };
};

PAGES.logs = async () => {
  const d = await api("/api/logs");
  main.innerHTML = header("السجلات", "سجل التجارب ما ينحذف منه شي، وكل تجربة ترفع عتبة حكم المتانة", chip(`${d.experiments.length} تجربة`, "") + chip(`${d.errors.length} خطأ`, d.errors.length ? "bad" : ""))
  + `<div class="card"><div class="card-h"><h2>سجل التجارب</h2></div>${d.experiments.length ? `<div class="tw"><table><thead><tr><th>الوقت</th><th>الاستراتيجية</th><th>النوع</th><th>العائد</th><th>الاحتفاظ</th><th>التراجع</th><th>الحكم</th><th></th></tr></thead><tbody>
  ${d.experiments.map((r) => `<tr><td class="ltr">${esc(r.at.slice(0, 16).replace("T", " "))}</td><td><b>${esc(r.strategy)}</b></td><td>${r.mode === "engine" ? "كامل" : "سريع"}</td>
  <td class="ltr ${sign(r.net_return)}">${fmtSPct(r.net_return)}</td><td class="ltr">${fmtSPct(r.hold_return)}</td><td class="ltr bad">${fmtPct(r.max_drawdown)}</td>
  <td>${verdictPill(r.verdict)}</td><td><button class="sm" data-run="${esc(r.id)}">عرض</button></td></tr>`).join("")}</tbody></table></div>` : empty("ما فيه تجارب بعد", "flask")}</div>
  <div class="cols"><div class="card"><div class="card-h"><h2>العمليات</h2></div><div class="tw" style="max-height:380px"><table><thead><tr><th>الوقت</th><th>العملية</th><th>التفاصيل</th></tr></thead><tbody>
  ${d.operations.map((o) => `<tr><td class="ltr">${esc(o.at.slice(0, 19).replace("T", " "))}</td><td><span class="pill">${esc(arOf(AR.ops, o.kind))}</span></td><td class="muted wrap">${opDetails(o)}</td></tr>`).join("")}</tbody></table></div></div>
  <div class="card"><div class="card-h"><h2>الأخطاء</h2></div>${d.errors.length ? `<pre>${esc(d.errors.join("\n"))}</pre>` : empty("ما فيه أخطاء", "check")}</div></div>
  ${Object.entries(d.paper_logs).map(([k, v]) => `<div class="card"><div class="card-h"><h2>سجل الجلسة <small>${esc(k)}</small></h2></div><pre class="rtl-log">${esc(v.map(logLineAr).join("\n"))}</pre></div>`).join("")}`;
};

PAGES.system = async () => {
  const d = await api("/api/system");
  const ev = d.evidence_collector;
  const tests = d.tests ? d.tests.summary : "";
  const passed = (tests.match(/(\d+) passed/) || [])[1], failed = (tests.match(/(\d+) failed/) || [])[1];
  main.innerHTML = header("حالة النظام", "صحة المحرك ومكوّناته", chip(d.mainnet_enabled ? "التداول الحقيقي مفعّل!" : "التداول الحقيقي معطّل", d.mainnet_enabled ? "bad" : "good"))
  + `<div class="kpis">
  ${kpi({ k: "التداول الحقيقي", v: d.mainnet_enabled ? "مفعّل!" : "معطّل", cls: d.mainnet_enabled ? "bad" : "good", ic: "shield", tone: d.mainnet_enabled ? "r" : "g", s: "مُقفل برمجيًا (MAINNET_ENABLED)" })}
  ${kpi({ k: "جامع الأدلة", v: ev.available ? `${fmtNum(ev.entries, 0)} لقطة` : "غير متاح", ic: "db", tone: ev.available ? "g" : "w", s: ev.available ? `آخر لقطة <span class="ltr">${esc(String(ev.last || "").slice(0, 16).replace("T", " "))}</span>` : "" })}
  ${kpi({ k: "الاختبارات الآلية", v: passed ? `${fmtNum(passed, 0)} ناجح` : "—", cls: failed ? "bad" : "good", ic: "check", tone: failed ? "r" : "g", s: failed ? `${failed} فاشل` : "صفر فشل" })}
  </div>
  <div class="cols">${ev.available ? `<div class="card"><div class="card-h"><h2>جامع الأدلة</h2></div><div class="kv">${Object.entries(ev.kinds).map(([k, v]) => `<div>${esc(arOf(AR.evidence, k))}</div><div>${fmtNum(v, 0)}</div>`).join("")}</div>
  <div style="margin-top:12px">${ev.alerts.length ? `<pre>${esc(ev.alerts.join("\n"))}</pre>` : '<div class="okmsg">ما فيه تنبيهات.</div>'}</div></div>` : ""}
  <div class="card"><div class="card-h"><h2>تفاصيل</h2></div><div class="kv"><div>آخر تشغيل للاختبارات</div><div>${esc(d.tests ? d.tests.file : "—")}</div>
  <div>الملخص</div><div style="white-space:normal">${esc(tests ? pytestAr(tests) : "—")}</div><div>مساحة العمل</div><div>${esc(d.workspace)}</div><div>وقت الخادم</div><div>${esc(d.time.slice(0, 19).replace("T", " "))} غرينتش</div></div></div></div>`;
};

window.addEventListener("hashchange", () => { const p = location.hash.slice(1); if (p && p !== current && PAGES[p]) nav(p); });
let resizeT = null;
window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => { if (current === "backtest" && window.__last && $("#bres")) showResult(window.__last, false); }, 250); });
// first page only after every script (e.g. decisions.js) has registered its pages
window.addEventListener("DOMContentLoaded", () => nav((location.hash || "#home").slice(1)));
