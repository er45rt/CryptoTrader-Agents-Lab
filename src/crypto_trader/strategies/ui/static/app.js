"use strict";
const $ = (s, r = document) => r.querySelector(s);
const main = $("#main");
const fmtPct = (x) => (x === null || x === undefined || isNaN(x)) ? "—" : (x * 100).toFixed(1) + "%";
const fmtNum = (x, d = 2) => (x === null || x === undefined || isNaN(x)) ? "—" : Number(x).toLocaleString("en-US", { maximumFractionDigits: d });
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-CT-UI": "1" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
function errBox(e) { return `<div class="err">${esc(e.message || e)}</div>`; }
let timer = null;
function nav(page) {
  clearInterval(timer);
  current = page;
  document.querySelectorAll(".side a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
  (PAGES[page] || PAGES.home)().catch((e) => { main.innerHTML = errBox(e); });
  location.hash = page;
}
document.querySelectorAll(".side a").forEach((a) => a.addEventListener("click", () => nav(a.dataset.page)));

/* ---------- charts (canvas, no library) ---------- */
function setupCanvas(c, h) {
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth || 800;
  c.width = w * dpr; c.height = h * dpr; c.style.height = h + "px";
  const g = c.getContext("2d"); g.scale(dpr, dpr);
  return { g, w, h };
}
function scale(min, max, a, b) { const span = (max - min) || 1; return (v) => b - (v - min) / span * (b - a); }
function candles(c, bars, trades, overlays = []) {
  const { g, w, h } = setupCanvas(c, 300);
  if (!bars.length) { g.fillStyle = css("--muted"); g.fillText("لا توجد بيانات", 10, 20); return; }
  const lo = Math.min(...bars.map((b) => b.l)), hi = Math.max(...bars.map((b) => b.h));
  const y = scale(lo, hi, 10, h - 24), bw = Math.max(1, (w - 20) / bars.length);
  const x = (i) => 10 + i * bw + bw / 2;
  bars.forEach((b, i) => {
    const up = b.c >= b.o; g.strokeStyle = g.fillStyle = up ? css("--good") : css("--bad");
    g.beginPath(); g.moveTo(x(i), y(b.h)); g.lineTo(x(i), y(b.l)); g.stroke();
    const top = y(Math.max(b.o, b.c)), bot = y(Math.min(b.o, b.c));
    g.fillRect(x(i) - Math.max(0.5, bw * 0.35), top, Math.max(1, bw * 0.7), Math.max(1, bot - top));
  });
  overlays.forEach((o) => { g.strokeStyle = o.color; g.lineWidth = 1.5; g.beginPath(); let s = false;
    o.values.forEach((v, i) => { if (v === null || isNaN(v)) return; s ? g.lineTo(x(i), y(v)) : g.moveTo(x(i), y(v)); s = true; }); g.stroke(); g.lineWidth = 1; });
  const idx = (t) => { const ts = Date.parse(t); let k = 0; for (let i = 0; i < bars.length; i++) if (Date.parse(bars[i].t) <= ts) k = i; return k; };
  const marks = [];
  (trades || []).forEach((t) => {
    if (t.entry_time) marks.push({ i: idx(t.entry_time), p: +t.entry_price, buy: true, t });
    if (t.exit_time) marks.push({ i: idx(t.exit_time), p: +t.exit_price, buy: false, t });
  });
  marks.forEach((m) => { g.fillStyle = m.buy ? css("--good") : css("--bad"); const yy = y(m.p); g.beginPath();
    if (m.buy) { g.moveTo(x(m.i), yy + 4); g.lineTo(x(m.i) - 5, yy + 13); g.lineTo(x(m.i) + 5, yy + 13); }
    else { g.moveTo(x(m.i), yy - 4); g.lineTo(x(m.i) - 5, yy - 13); g.lineTo(x(m.i) + 5, yy - 13); } g.fill(); });
  g.fillStyle = css("--muted"); g.font = "11px sans-serif";
  g.fillText(bars[0].t.slice(0, 10), w - 80, h - 6); g.fillText(bars[bars.length - 1].t.slice(0, 10), 10, h - 6);
  g.fillText(fmtNum(hi, 0), w - 60, 18); g.fillText(fmtNum(lo, 0), w - 60, h - 26);
  tooltip(c, (px) => { const i = Math.min(bars.length - 1, Math.max(0, Math.floor((px - 10) / bw)));
    const b = bars[i]; const ms = marks.filter((m) => Math.abs(m.i - i) <= 1);
    return `${b.t.slice(0, 16)}<br>O ${fmtNum(b.o)} H ${fmtNum(b.h)} L ${fmtNum(b.l)} C ${fmtNum(b.c)}` +
      ms.map((m) => `<br><b class="${m.buy ? "good" : "bad"}">${m.buy ? "دخول" : "خروج"} @ ${fmtNum(m.p)}</b>` + (!m.buy && m.t.pnl_quote ? ` (${fmtNum(m.t.pnl_quote)})` : "")).join(""); });
}
function lines(c, series, h = 200, pct = false) {
  const { g, w } = setupCanvas(c, h);
  const all = series.flatMap((s) => s.points.map((p) => p[1]));
  if (!all.length) return;
  const lo = Math.min(...all), hi = Math.max(...all), y = scale(lo, hi, 8, h - 20);
  series.forEach((s) => { const n = s.points.length; g.strokeStyle = s.color; g.lineWidth = 1.6; g.beginPath();
    s.points.forEach((p, i) => { const xx = 10 + i / Math.max(1, n - 1) * (w - 20); i ? g.lineTo(xx, y(p[1])) : g.moveTo(xx, y(p[1])); });
    if (s.fill) { g.lineTo(w - 10, y(0)); g.lineTo(10, y(0)); g.closePath(); g.fillStyle = s.fill; g.fill(); } else g.stroke(); });
  g.fillStyle = css("--muted"); g.font = "11px sans-serif";
  g.fillText(pct ? fmtPct(hi) : fmtNum(hi), w - 70, 14); g.fillText(pct ? fmtPct(lo) : fmtNum(lo), w - 70, h - 22);
}
function tooltip(c, html) {
  let tip = c.parentElement.querySelector(".tip");
  if (!tip) { tip = document.createElement("div"); tip.className = "tip"; c.parentElement.style.position = "relative"; c.parentElement.appendChild(tip); }
  c.onmousemove = (e) => { const r = c.getBoundingClientRect(); tip.innerHTML = html(e.clientX - r.left); tip.style.display = "block";
    tip.style.left = Math.min(r.width - 220, Math.max(0, e.clientX - r.left + 12)) + "px"; tip.style.top = (e.clientY - r.top + 12) + "px"; };
  c.onmouseleave = () => { tip.style.display = "none"; };
}
function heat(monthly) {
  return `<div class="heat">${monthly.map((m) => { const v = m.return; const a = Math.min(1, Math.abs(v) / 0.2);
    const bg = v >= 0 ? `rgba(29,158,117,${0.15 + a * 0.7})` : `rgba(226,75,74,${0.15 + a * 0.7})`;
    return `<div title="${m.month}: ${fmtPct(v)}" style="background:${bg}">${m.month.slice(2)}</div>`; }).join("")}</div>`;
}
function sma(bars, n) { const out = []; let s = 0; bars.forEach((b, i) => { s += b.c; if (i >= n) s -= bars[i - n].c; out.push(i >= n - 1 ? s / n : null); }); return out; }

/* ---------- pages ---------- */
const PAGES = {};
PAGES.home = async () => {
  const d = await api("/api/home");
  const pnl = d.equity_total - d.capital_total;
  main.innerHTML = `<h1>الرئيسية</h1>
  <div class="grid">
    <div class="metric"><div class="k">جلسات ورقية تعمل</div><div class="v">${d.running} / ${d.sessions.length}</div></div>
    <div class="metric"><div class="k">الرصيد الورقي</div><div class="v">${fmtNum(d.equity_total)}</div></div>
    <div class="metric"><div class="k">الربح / الخسارة</div><div class="v ${pnl >= 0 ? "good" : "bad"}">${fmtNum(pnl)}</div></div>
    <div class="metric"><div class="k">مراكز مفتوحة</div><div class="v">${d.open_positions.length}</div></div>
    <div class="metric"><div class="k">تجارب الاختبار</div><div class="v">${d.experiments}</div></div>
  </div>
  <div class="card" style="margin-top:14px"><h2>الجلسات</h2>${sessionTable(d.sessions)}</div>
  ${d.last_run ? `<div class="card"><h2>آخر اختبار</h2>${runRow(d.last_run)}</div>` : ""}
  <p class="muted">كل الأرقام هنا ورقية (محاكاة). ما فيه اتصال بحساب حقيقي، والتداول الحقيقي معطّل في الكود.</p>`;
  timer = setInterval(() => PAGES.home().catch(() => {}), 15000);
};
function sessionTable(ss) {
  if (!ss.length) return `<p class="muted">ما فيه جلسات. أنشئ وحدة من صفحة التداول الورقي.</p>`;
  return `<table><tr><th>الجلسة</th><th>الاستراتيجية</th><th>الحالة</th><th>الرصيد</th><th>العائد</th><th>المركز</th><th>صفقات</th><th>فائتة</th></tr>
  ${ss.map((s) => `<tr><td><a href="#" data-session="${esc(s.name)}">${esc(s.name)}</a></td><td>${esc(s.strategy)} · ${esc(s.timeframe)}</td>
  <td>${s.running && s.stopping ? '<span class="pill warn">يتوقف…</span>' : s.running ? '<span class="pill good">تعمل</span>' : '<span class="pill warn">متوقفة</span>'}${s.error ? ' <span class="pill bad" title="' + esc(s.error.error) + '">خطأ</span>' : ""}</td>
  <td>${fmtNum(s.equity)}</td><td class="${(s.net_return || 0) >= 0 ? "good" : "bad"}">${fmtPct(s.net_return)}</td>
  <td>${esc(JSON.stringify(s.position || {}))}</td><td>${s.fills ?? "—"}</td><td>${s.missed_bars ?? "—"}</td></tr>`).join("")}</table>`;
}
document.addEventListener("click", (e) => { const a = e.target.closest("[data-session]"); if (a) { e.preventDefault(); paperDetail(a.dataset.session); } });
function runRow(r) {
  return `<div class="muted">${esc(r.at.slice(0, 16))} · ${esc(r.strategy)} · ${r.mode === "engine" ? "كامل" : "سريع"}</div>
  العائد <b class="${r.net_return >= 0 ? "good" : "bad"}">${fmtPct(r.net_return)}</b> مقابل الاحتفاظ ${fmtPct(r.hold_return)} ·
  أقصى تراجع ${fmtPct(r.max_drawdown)} · ${r.verdict === "promising" ? '<span class="pill good">واعدة</span>' : '<span class="pill warn">غير مثبتة</span>'}
  <button data-run="${esc(r.id)}" style="margin-right:8px">عرض</button>`;
}
document.addEventListener("click", (e) => { const b = e.target.closest("[data-run]"); if (b) { nav("backtest"); setTimeout(() => api("/api/run?id=" + b.dataset.run).then(showResult), 300); } });

PAGES.strategies = async () => {
  const d = await api("/api/strategies");
  main.innerHTML = `<h1>الاستراتيجيات</h1>
  <div class="card"><h2>إضافة استراتيجية</h2>
    <p class="muted">ملف Python فيه صنف يرث من <code>Strategy</code>. يُفحص قبل الحفظ، والملف المرفوض ما ينحفظ.</p>
    <div class="row"><input type="file" id="up" accept=".py"><button class="primary" id="upb">رفع وفحص</button></div><div id="upr"></div>
    <h2 style="margin-top:14px">فحص استراتيجية Freqtrade أو ملف بمساره</h2>
    <div class="row"><input id="spec" placeholder="freqtrade:C:\\path\\MyStrategy.py:MyStrategy"><button id="chk">فحص</button></div><div id="chr"></div></div>
  <div class="card"><h2>المركّبة</h2><table><tr><th>الاسم</th><th>الإطار</th><th>التهيئة</th><th>المصدر</th><th>الحالة</th><th></th></tr>
  ${d.strategies.map((s) => `<tr><td>${esc(s.name || s.file)}</td><td>${esc(s.timeframe || "—")}</td><td>${s.warmup_bars ?? "—"}</td><td>${s.folder === "workspace" ? "مضافة" : "أمثلة"}</td>
  <td>${s.ok ? '<span class="pill good">تعمل</span>' : '<span class="pill bad" title="' + esc(s.error) + '">مرفوضة</span>'}</td>
  <td>${s.ok ? `<button data-bt="${esc(s.spec)}">اختبار</button> <button data-pp="${esc(s.spec)}">ورقي</button>` : esc(s.error || "")}
  ${s.removable ? ` <button data-rm="${esc(s.file)}">حذف</button>` : ""}</td></tr>`).join("")}</table></div>`;
  $("#upb").onclick = async () => { const f = $("#up").files[0]; if (!f) { $("#upr").innerHTML = errBox("اختر ملف .py"); return; }
    try { const r = await api("/api/strategies/upload", { filename: f.name, content: await f.text() }); $("#upr").innerHTML = `<p class="good">انضافت: ${esc(r.file)}</p>`; PAGES.strategies(); }
    catch (e) { $("#upr").innerHTML = errBox(e); } };
  $("#chk").onclick = async () => { try { const r = await api("/api/strategies/check", { spec: $("#spec").value });
    $("#chr").innerHTML = `<p class="good">تعمل: ${esc(r.name)} · ${esc(r.timeframe)} · تهيئة ${r.warmup_bars}</p>`; } catch (e) { $("#chr").innerHTML = errBox(e); } };
  document.querySelectorAll("[data-rm]").forEach((b) => b.onclick = async () => { if (!confirm("حذف " + b.dataset.rm + "؟")) return;
    await api("/api/strategies/remove", { filename: b.dataset.rm }); PAGES.strategies(); });
  document.querySelectorAll("[data-bt]").forEach((b) => b.onclick = () => { sessionStorage.setItem("spec", b.dataset.bt); nav("backtest"); });
  document.querySelectorAll("[data-pp]").forEach((b) => b.onclick = () => { sessionStorage.setItem("spec", b.dataset.pp); nav("paper"); });
};

PAGES.backtest = async () => {
  const [st, df, risk] = await Promise.all([api("/api/strategies"), api("/api/data"), api("/api/risk")]);
  const pre = sessionStorage.getItem("spec") || "";
  const okS = st.strategies.filter((s) => s.ok);
  main.innerHTML = `<h1>الاختبار التاريخي</h1>
  <div class="card"><div class="row">
    <div><label>الاستراتيجية</label><select id="bs">${okS.map((s) => `<option value="${esc(s.spec)}" data-tf="${esc(s.timeframe)}" ${s.spec === pre ? "selected" : ""}>${esc(s.name)} (${esc(s.timeframe)})</option>`).join("")}</select></div>
    <div><label>البيانات</label><select id="bd">${df.files.map((f) => `<option value="${esc(f.path)}" data-tf="${esc(f.timeframe || "")}">${esc(f.name)}</option>`).join("")}</select></div>
    <div><label>من</label><input id="bfrom" type="date" value="2020-01-01"></div>
    <div><label>إلى (قبل 2025-09-07)</label><input id="bto" type="date" value="2025-09-06"></div>
  </div><div class="row" style="margin-top:8px">
    <div><label>رأس المال</label><input id="bcap" value="${esc(risk.editable.capital)}"></div>
    <div><label>الرسوم لكل جانب</label><input id="bfee" value="${esc(risk.editable.fee)}"></div>
    <div><label>الانزلاق (bps)</label><input id="bslip" value="${esc(risk.editable.slippage_bps)}"></div>
    <div><label>معاملات (JSON)</label><input id="bpar" placeholder='{"fast": 20, "slow": 50}'></div>
    <button class="primary" id="bfast">اختبار سريع</button><button id="beng">كامل عبر المحرك</button>
  </div><div id="bmsg"></div></div><div id="bres"></div>`;
  const go = async (engine) => {
    let params = {}; try { params = $("#bpar").value ? JSON.parse($("#bpar").value) : {}; } catch { $("#bmsg").innerHTML = errBox("المعاملات لازم تكون JSON صحيح"); return; }
    const body = { spec: $("#bs").value, data: $("#bd").value, start: $("#bfrom").value || null, end: $("#bto").value || null,
      capital: $("#bcap").value, fee: $("#bfee").value, slippage_bps: $("#bslip").value, params, engine };
    $("#bfast").disabled = $("#beng").disabled = true;
    $("#bmsg").innerHTML = `<p class="muted">جاري ${engine ? "الاختبار الكامل (قد ياخذ دقائق)" : "الاختبار السريع"}…</p>`;
    try { const { job } = await api("/api/backtest", body);
      for (;;) { await new Promise((r) => setTimeout(r, 1200)); const j = await api("/api/job?id=" + job);
        if (j.state === "done") { $("#bmsg").innerHTML = ""; showResult(j.result); break; }
        if (j.state === "error") { $("#bmsg").innerHTML = errBox(j.error); break; } }
    } catch (e) { $("#bmsg").innerHTML = errBox(e); }
    $("#bfast").disabled = $("#beng").disabled = false; };
  const matchData = () => { const tf = $("#bs").selectedOptions[0]?.dataset.tf;
    const opt = [...$("#bd").options].find((o) => o.dataset.tf === tf); if (opt) $("#bd").value = opt.value;
    $("#bmsg").innerHTML = opt || !tf ? "" : `<p class="warn">ما فيه ملف بيانات بإطار ${esc(tf)}. أضف ملف في مجلد data بمساحة العمل.</p>`; };
  $("#bs").onchange = matchData; matchData();
  $("#bfast").onclick = () => go(false); $("#beng").onclick = () => go(true);
};
function showResult(r) {
  const m = r.metrics, b = r.benchmark, v = r.verdict;
  $("#bres").innerHTML = `
  <div class="card"><h2>${esc(r.strategy)} · ${esc(r.timeframe)} · ${r.mode === "engine" ? "كامل عبر المحرك" : "سريع"} · ${esc(r.start.slice(0, 10))} → ${esc(r.end.slice(0, 10))}</h2>
  <div class="grid">
    <div class="metric"><div class="k">العائد الصافي</div><div class="v ${m.net_return >= 0 ? "good" : "bad"}">${fmtPct(m.net_return)}</div></div>
    <div class="metric"><div class="k">الاحتفاظ</div><div class="v">${fmtPct(b.net_return)}</div></div>
    <div class="metric"><div class="k">Sharpe (مقابل الاحتفاظ)</div><div class="v">${fmtNum(m.sharpe)} / ${fmtNum(b.sharpe)}</div></div>
    <div class="metric"><div class="k">أقصى تراجع</div><div class="v bad">${fmtPct(m.max_drawdown)}</div></div>
    <div class="metric"><div class="k">صفقات / نسبة الربح</div><div class="v">${m.round_trips} / ${fmtPct(m.win_rate)}</div></div>
    <div class="metric"><div class="k">الرسوم + الانزلاق</div><div class="v">${fmtNum(m.fees_quote + m.slippage_cost_quote_est)}</div></div>
    <div class="metric"><div class="k">وقت في السوق</div><div class="v">${fmtPct(m.time_in_market)}</div></div>
  </div></div>
  <div class="card"><h2>حكم المتانة: ${v.verdict === "promising" ? '<span class="pill good">واعدة</span>' : '<span class="pill warn">غير مثبتة</span>'}</h2>
  <table>${v.checks.map((c) => `<tr><td>${esc(c.label)}</td><td>${c.ok === null ? '<span class="muted">غير معرّف</span>' : c.ok ? '<span class="good">✓</span>' : '<span class="bad">✗</span>'}</td><td class="muted">${esc(c.detail)}</td></tr>`).join("")}</table>
  <p class="muted">${esc(v.note)}</p></div>
  <div class="card"><h2>الشموع والصفقات</h2><canvas id="cc"></canvas><p class="muted">▲ دخول · ▼ خروج · أزرق SMA20 · برتقالي SMA50 · مرّر على الشارت للتفاصيل</p></div>
  <div class="card"><h2>رأس المال: الاستراتيجية مقابل الاحتفاظ</h2><canvas id="ce"></canvas></div>
  <div class="card"><h2>التراجع عبر الزمن</h2><canvas id="cd"></canvas></div>
  <div class="card"><h2>العائد الشهري</h2>${heat(r.monthly)}</div>
  <div class="card"><h2>الصفقات (${r.trades.length})</h2><table><tr><th>دخول</th><th>سعر</th><th>خروج</th><th>سعر</th><th>كمية</th><th>ربح/خسارة</th></tr>
  ${r.trades.slice(-200).reverse().map((t) => `<tr><td>${esc((t.entry_time || "").slice(0, 16))}</td><td>${fmtNum(t.entry_price)}</td><td>${esc((t.exit_time || "مفتوحة").slice(0, 16))}</td><td>${fmtNum(t.exit_price)}</td><td>${esc(t.quantity)}</td>
  <td class="${(+t.pnl_quote || 0) >= 0 ? "good" : "bad"}">${fmtNum(t.pnl_quote)}</td></tr>`).join("")}</table></div>`;
  candles($("#cc"), r.bars, r.trades, [{ values: sma(r.bars, 20), color: css("--accent") }, { values: sma(r.bars, 50), color: "#BA7517" }]);
  const holdCap = r.equity.length ? r.equity[0][1] : 1;
  const bars = r.bars; const first = bars.length ? bars[0].c : 1;
  lines($("#ce"), [{ points: r.equity, color: css("--accent") },
    { points: bars.map((x) => [x.t, holdCap * x.c / first]), color: "#888780" }], 220);
  lines($("#cd"), [{ points: r.drawdown, color: css("--bad"), fill: "rgba(226,75,74,0.25)" }], 140, true);
}

PAGES.paper = async () => {
  const [d, st] = await Promise.all([api("/api/paper"), api("/api/strategies")]);
  const pre = sessionStorage.getItem("spec") || "";
  main.innerHTML = `<h1>التداول الورقي</h1>
  <div class="card"><h2>جلسة جديدة</h2>
  <p class="muted">على الأسعار الحية من Binance العامة، بدون مفاتيح، وما ينرسل أي أمر. تشغيل الجلسة يعتبر تفويضًا منك لهالجلسة.</p>
  <div class="row"><div><label>الاسم</label><input id="pn" placeholder="btc-sma-1"></div>
  <div><label>الاستراتيجية</label><select id="ps">${st.strategies.filter((s) => s.ok).map((s) => `<option value="${esc(s.spec)}" ${s.spec === pre ? "selected" : ""}>${esc(s.name)} (${esc(s.timeframe)})</option>`).join("")}</select></div>
  <div><label>الزوج</label><input id="psym" value="BTCUSDT"></div><div><label>رأس المال</label><input id="pcap" value="80"></div>
  <button class="primary" id="pcreate">إنشاء وتشغيل</button></div><div id="pmsg"></div></div>
  <div class="card"><h2>الجلسات</h2>${sessionTable(d.sessions)}
  ${d.sessions.map((s) => `<button data-start="${esc(s.name)}">تشغيل ${esc(s.name)}</button> <button data-stop="${esc(s.name)}">إيقاف ${esc(s.name)}</button> `).join("")}</div>
  <div id="pdet"></div>`;
  $("#pcreate").onclick = async () => { try { const name = $("#pn").value.trim();
    await api("/api/paper/new", { name, spec: $("#ps").value, symbol: $("#psym").value.trim(), base: $("#psym").value.trim().replace(/USDT$/, ""), capital: $("#pcap").value });
    await api("/api/paper/start", { name }); PAGES.paper(); } catch (e) { $("#pmsg").innerHTML = errBox(e); } };
  document.querySelectorAll("[data-start]").forEach((b) => b.onclick = async () => { await api("/api/paper/start", { name: b.dataset.start }); PAGES.paper(); });
  document.querySelectorAll("[data-stop]").forEach((b) => b.onclick = async () => { await api("/api/paper/stop", { name: b.dataset.stop }); PAGES.paper(); });
};
async function paperDetail(name) {
  if (!$("#pdet")) { await PAGES.paper(); }
  const draw = async () => { const d = await api("/api/paper/session?name=" + encodeURIComponent(name)); const s = d.status || {};
    $("#pdet").innerHTML = `<div class="card"><h2>${esc(name)} · ${esc(d.session.strategy_name)} · ${d.running ? '<span class="pill good">تعمل</span>' : '<span class="pill warn">متوقفة</span>'}</h2>
    <div class="grid"><div class="metric"><div class="k">الرصيد</div><div class="v">${fmtNum(s.equity_quote)}</div></div>
    <div class="metric"><div class="k">العائد</div><div class="v ${(s.net_return || 0) >= 0 ? "good" : "bad"}">${fmtPct(s.net_return)}</div></div>
    <div class="metric"><div class="k">المركز</div><div class="v">${esc(JSON.stringify(s.position || {}))}</div></div>
    <div class="metric"><div class="k">آخر سعر</div><div class="v">${d.bars.length ? fmtNum(d.bars[d.bars.length - 1].c) : "—"}</div></div>
    <div class="metric"><div class="k">آخر دورة</div><div class="v" style="font-size:13px">${esc((s.cycle_at || "—").slice(0, 19))}</div></div></div>
    <canvas id="pc" style="margin-top:10px"></canvas>
    <h2 style="margin-top:12px">القرارات</h2><table><tr><th>الوقت</th><th>القرار</th><th>السبب</th><th>المخاطر</th></tr>
    ${d.decisions.map((x) => `<tr><td>${esc(x.at.slice(0, 16))}</td><td>${x.action === "buy" ? '<span class="good">شراء</span>' : x.action === "sell" ? '<span class="bad">بيع</span>' : "انتظار"}</td>
    <td class="${x.rationale.startsWith("missed") ? "warn" : ""}">${esc(x.rationale.startsWith("missed") ? "فائتة (وصلت متأخرة)" : x.rationale)}</td><td>${esc(x.risk || "—")}</td></tr>`).join("")}</table>
    <h2 style="margin-top:12px">السجل</h2><pre>${esc(d.log.join("\n"))}</pre></div>`;
    candles($("#pc"), d.bars, s.trades || [], [{ values: sma(d.bars, 20), color: css("--accent") }]); };
  await draw(); clearInterval(timer); timer = setInterval(() => draw().catch(() => {}), 15000);
}

PAGES.risk = async () => {
  const d = await api("/api/risk");
  main.innerHTML = `<h1>المخاطر والإعدادات</h1>
  <div class="card"><h2>إعدادات الاختبار والتداول الورقي (قابلة للتعديل)</h2><div class="row">
  <div><label>رأس المال الافتراضي</label><input id="rcap" value="${esc(d.editable.capital)}"></div>
  <div><label>الرسوم لكل جانب</label><input id="rfee" value="${esc(d.editable.fee)}"></div>
  <div><label>الانزلاق (bps)</label><input id="rslip" value="${esc(d.editable.slippage_bps)}"></div>
  <div><label>حد أقصى للمركز (اختياري)</label><input id="rmax" value="${esc(d.editable.max_position ?? "")}"></div></div>
  <label style="margin-top:8px">حدود مخاطر إضافية للمحرك (JSON، مثل {"max_order_notional_quote": "500"})</label>
  <textarea id="rov" rows="3">${esc(JSON.stringify(d.editable.risk_overrides || {}))}</textarea>
  <div class="row" style="margin-top:8px"><button class="primary" id="rsave">حفظ</button></div><div id="rmsg"></div></div>
  <div class="card"><h2>الحدود الفعلية التي يطبقها المحرك</h2><table>${Object.entries(d.effective_engine_limits).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("")}</table></div>
  <div class="card"><h2>قواعد ثابتة</h2><ul>${d.rules.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>
  <div class="card"><h2>المسار الإنتاجي</h2><p>${d.production.mainnet_enabled ? '<span class="bad">مفعّل!</span>' : '<span class="pill bad">التداول الحقيقي معطّل في الكود</span>'}</p><p class="muted">${esc(d.production.note)}</p></div>`;
  $("#rsave").onclick = async () => { let ov; try { ov = JSON.parse($("#rov").value || "{}"); } catch { $("#rmsg").innerHTML = errBox("الحدود الإضافية لازم تكون JSON صحيح"); return; }
    try { await api("/api/risk", { capital: $("#rcap").value, fee: $("#rfee").value, slippage_bps: $("#rslip").value, max_position: $("#rmax").value || null, risk_overrides: ov });
      $("#rmsg").innerHTML = '<p class="good">انحفظ.</p>'; } catch (e) { $("#rmsg").innerHTML = errBox(e); } };
};

PAGES.logs = async () => {
  const d = await api("/api/logs");
  main.innerHTML = `<h1>السجلات</h1>
  <div class="card"><h2>سجل التجارب (${d.experiments.length}) — ما ينحذف، وكل تجربة ترفع عتبة «حكم المتانة»</h2>
  <table><tr><th>الوقت</th><th>الاستراتيجية</th><th>النوع</th><th>العائد</th><th>الاحتفاظ</th><th>التراجع</th><th>الحكم</th><th></th></tr>
  ${d.experiments.map((r) => `<tr><td>${esc(r.at.slice(0, 16))}</td><td>${esc(r.strategy)}</td><td>${r.mode === "engine" ? "كامل" : "سريع"}</td>
  <td class="${r.net_return >= 0 ? "good" : "bad"}">${fmtPct(r.net_return)}</td><td>${fmtPct(r.hold_return)}</td><td>${fmtPct(r.max_drawdown)}</td>
  <td>${r.verdict === "promising" ? '<span class="pill good">واعدة</span>' : '<span class="pill warn">غير مثبتة</span>'}</td><td><button data-run="${esc(r.id)}">عرض</button></td></tr>`).join("")}</table></div>
  <div class="card"><h2>العمليات</h2><table><tr><th>الوقت</th><th>العملية</th><th>التفاصيل</th></tr>
  ${d.operations.map((o) => `<tr><td>${esc(o.at.slice(0, 19))}</td><td>${esc(o.kind)}</td><td class="muted">${esc(JSON.stringify(Object.fromEntries(Object.entries(o).filter(([k]) => !["at", "kind"].includes(k)))))}</td></tr>`).join("")}</table></div>
  <div class="card"><h2>الأخطاء</h2>${d.errors.length ? `<pre>${esc(d.errors.join("\n"))}</pre>` : '<p class="muted">ما فيه أخطاء.</p>'}</div>
  ${Object.entries(d.paper_logs).map(([k, v]) => `<div class="card"><h2>سجل الجلسة ${esc(k)}</h2><pre>${esc(v.join("\n"))}</pre></div>`).join("")}`;
};

PAGES.system = async () => {
  const d = await api("/api/system");
  const ev = d.evidence_collector;
  main.innerHTML = `<h1>حالة النظام</h1>
  <div class="grid">
  <div class="metric"><div class="k">التداول الحقيقي</div><div class="v ${d.mainnet_enabled ? "bad" : "good"}">${d.mainnet_enabled ? "مفعّل!" : "معطّل في الكود"}</div></div>
  <div class="metric"><div class="k">جامع الأدلة</div><div class="v">${ev.available ? `${ev.entries} لقطة` : "غير متاح"}</div></div>
  <div class="metric"><div class="k">آخر اختبارات</div><div class="v" style="font-size:13px">${esc(d.tests ? d.tests.summary : "—")}</div></div></div>
  ${ev.available ? `<div class="card" style="margin-top:14px"><h2>جامع الأدلة</h2><p>آخر لقطة: ${esc(ev.last)}</p><p>${esc(JSON.stringify(ev.kinds))}</p>
  ${ev.alerts.length ? `<pre>${esc(ev.alerts.join("\n"))}</pre>` : '<p class="good">ما فيه تنبيهات.</p>'}</div>` : ""}
  <p class="muted">مساحة العمل: ${esc(d.workspace)}</p>`;
};

let current = null;
window.addEventListener("hashchange", () => { const p = location.hash.slice(1); if (p && p !== current && PAGES[p]) nav(p); });
nav((location.hash || "#home").slice(1));
