"use strict";
/* لوحة المتابعة (phase 2, owner decision D42). Four separate money spaces - my real portfolio, demo, paper and backtest.
   Each card shows its own numbers with their source and time and loads on its own; numbers from different spaces are
   never added, netted or compared in one figure. */

const DB = { space: "portfolio", spaces: {}, demo: null, status: null };
const DB_WAIT_MS = 300;   // longest the dashboard waits to draw all its spaces in one go
const DB_SPACES = [["portfolio", "محفظتي", "حقيقية"], ["demo", "التجريبي", "Demo"], ["paper", "التداول الورقي", "Paper"], ["backtest", "الاختبارات", "Backtest"]];
const dbAt = (t) => (t ? String(t).slice(0, 16).replace("T", " ") : "—");

PAGES.home = async () => {
  // the four spaces' data first, then the page is drawn once, complete (owner 2026-11-08: the space cards and the
  // detail filled in one by one and moved the page); each space still loads, succeeds or fails on its own
  // A slow space (e.g. prices being fetched) never holds the page: after DB_WAIT_MS the page is drawn with what has
  // arrived and each later space fills its own card, as before.
  const got = {}, visit = DB.visit = (DB.visit || 0) + 1;   // a late answer of an earlier visit never writes here
  let drawn = false;
  const load = (k, p) => p.then((d) => ({ ok: true, d }), (e) => ({ ok: false, e })).then((s) => {
    got[k] = s;
    if (!drawn || current !== "home" || DB.visit !== visit) return;
    DB.spaces[k] = s; dbCard(k); if (k === DB.space) dbDetail();
  });
  const loads = [load("portfolio", api("/api/portfolio")), load("demo", api("/api/demo")), load("paper", api("/api/paper")),
    load("backtest", api("/api/runs"))];
  await Promise.race([Promise.all(loads), new Promise((r) => setTimeout(r, DB_WAIT_MS))]);
  if (current !== "home" || DB.visit !== visit) return;
  DB.spaces = {};
  for (const k in got) DB.spaces[k] = got[k];
  drawn = true;
  main.innerHTML = header("لوحة المتابعة", "كل مساحة مالية لحالها: محفظتك الحقيقية، والتجريبي، والورقي، والاختبارات. أرقامها ما تنجمع مع بعض أبدًا.",
    chip("التداول الحقيقي معطّل", "") + chip("بدون مفاتيح", ""))
  // the status bar keeps its place from the first paint (owner 2026-11-08: it arrived last and pushed the page 61 px):
  // the last answer, refreshed in place by dbStatus() just below, or a one-line placeholder of the same height
  + `<div id="db-status">${DB.status ? dbStatusHtml(DB.status) : '<div class="stbar"><span class="pill">تحميل…</span></div>'}</div>
  <div class="spaces" id="db-spaces">${DB_SPACES.map(([k, t, s]) => `<button type="button" class="space sp-${k}" data-space="${k}">
    <small>${t} <span class="muted">· ${s}</span></small><b>…</b><span class="sp-sub"></span><span class="sp-src"></span></button>`).join("")}</div>
  <div class="card" id="db-detail">${loading("تحميل…")}</div>
  <div class="card" id="db-compare"></div>`;
  $("#db-spaces").onclick = (e) => { const b = e.target.closest("[data-space]"); if (!b) return; DB.space = b.dataset.space; dbPaint(); dbDetail(); };
  dbPaint();
  DB_SPACES.forEach(([k]) => dbCard(k));
  dbDetail();
  dbStatus();
  timer = setInterval(() => { if (current === "home") dbStatus(); }, 60000);
  dbCompare();
};

function dbPaint() { document.querySelectorAll("#db-spaces [data-space]").forEach((b) => b.classList.toggle("on", b.dataset.space === DB.space)); }

/** One space's headline, from that space's own endpoint only. */
function dbCard(k) {
  const el = $(`#db-spaces [data-space="${k}"]`); if (!el) return;
  const s = DB.spaces[k], set = (v, sub, src) => { el.querySelector("b").innerHTML = v; el.querySelector(".sp-sub").innerHTML = sub; el.querySelector(".sp-src").innerHTML = src; };
  if (!s) return;
  if (!s.ok) return set("—", `<span class="bad">${esc(trErr(s.e.message).ar || "تعذّر التحميل")}</span>`, "");
  const d = s.d;
  if (k === "portfolio") {
    const t = d.totals, at = d.assets.map((a) => a.price_at).filter(Boolean).sort().pop();
    if (!d.n_transactions) return set("—", "ما فيه عمليات مسجّلة في محفظتك بعد؛ أضفها من صفحة «محفظتي»",   // the reason, not a blank
      `سجل المحفظة فاضي · <span class="ltr">${esc(d.storage)}</span>`);
    return set(t.value ? fmtUsd(t.value) : "—",
      (t.unrealized !== null ? `<span class="${sign(t.unrealized)}">${+t.unrealized > 0 ? "+" : ""}${fmtUsd(t.unrealized)}</span> غير محقق` : "ما فيه أصول مسعّرة")
        + (t.value_complete ? "" : ` · <span class="warn">${d.unpriced.length} أصل بدون سعر</span>`),
      `سجل المحفظة · أسعار Binance العامة${at ? ` · <span class="ltr">${esc(dbAt(at))}</span>` : ""}`);
  }
  if (k === "demo") {
    const last = [...d.demos].sort((a, b) => (a.created_at < b.created_at ? 1 : -1))[0];
    return set(last && isNum(last.equity) ? fmtUsd(last.equity) : last ? fmtUsd(last.capital) : "—",
      last ? `${esc(last.name)} · <span class="${sign(last.net_return)}">${fmtSPct(last.net_return, 2)}</span> · ${d.demos.length} جلسة` : "ما فيه جلسات تجريبية",
      `إعادة تشغيل محلية لأسعار تاريخية${last?.clock ? ` · <span class="ltr">${esc(dbAt(last.clock))}</span>` : ""}`);
  }
  if (k === "paper") {
    const run = d.sessions.filter((x) => x.running).length, last = d.sessions.map((x) => x.last_cycle).filter(Boolean).sort().pop();
    return set(`<span class="ltr">${run} / ${d.sessions.length}</span> تعمل`,
      d.sessions.length ? d.sessions.slice(0, 2).map((x) => `${esc(x.name)} <span class="${sign(x.net_return)}">${fmtSPct(x.net_return, 2)}</span>`).join(" · ") : "ما فيه جلسات ورقية",
      `أسعار Binance العامة الحية · محاكاة${last ? ` · <span class="ltr">${esc(dbAt(last))}</span>` : ""}`);
  }
  const r = d.runs[0];
  return set(`<span class="ltr">${d.count}</span> اختبار`,
    r ? `آخرها ${esc(r.strategy)} · <span class="${sign(r.net_return)}">${fmtSPct(r.net_return)}</span>` : "ما فيه اختبارات بعد",
    `سجل التجارب runs.jsonl${r ? ` · <span class="ltr">${esc(dbAt(r.at))}</span>` : ""}`);
}

/** The selected space in detail, with its own chart. */
async function dbDetail() {
  const box = $("#db-detail"), k = DB.space, s = DB.spaces[k]; if (!box) return;
  const title = DB_SPACES.find((x) => x[0] === k)[1];
  if (!s) { box.innerHTML = loading("تحميل…"); return; }
  if (!s.ok) { box.innerHTML = `<div class="card-h"><h2>${title}</h2></div>${errBox(s.e)}`; return; }
  const d = s.d;
  if (k === "portfolio") {
    const t = d.totals;
    box.innerHTML = `<div class="card-h"><h2>${title} <small>القيمة من الأصول المسعّرة فقط</small></h2><button class="sm" data-nav="portfolio">افتح محفظتي</button></div>
      <div class="kv"><div>القيمة الحالية</div><div>${t.value ? fmtUsd(t.value) : "—"}</div><div>تكلفة الأصول المسعّرة</div><div>${fmtUsd(t.priced_basis)}</div>
      <div>غير محقق</div><div class="${sign(t.unrealized)}">${t.unrealized !== null ? fmtUsd(t.unrealized) : "—"}</div><div>محقق</div><div class="${sign(t.realized)}">${fmtUsd(t.realized)}</div>
      <div>الرسوم بالدولار</div><div>${fmtUsd(t.fees_quote)}</div><div>العمليات</div><div>${d.n_transactions}</div></div>
      ${t.value_complete ? "" : note(`أصول بدون سعر من Binance ما دخلت في القيمة: ${d.unpriced.map(esc).join("، ")}`)}
      <div class="chart" style="margin-top:12px"><canvas id="db-c"></canvas></div>`;
    if (!d.n_transactions) { $("#db-c").replaceWith(Object.assign(document.createElement("div"), { className: "muted", textContent: "ما فيه عمليات مسجّلة بعد." })); return; }
    api("/api/portfolio/history", undefined, 20000).then((h) => { if (current !== "home" || DB.space !== "portfolio" || !$("#db-c")) return;
      const acc = css("--accent");
      lines($("#db-c"), [{ name: "القيمة", points: h.points.map((p) => [p.d, +p.value]), color: acc, area: [acc + "44", acc + "00"] },
        { name: "التكلفة", points: h.points.map((p) => [p.d, +p.basis]), color: css("--hold"), dash: true, width: 1.5 }], 240); })
      .catch((e) => { const c = $("#db-c"); if (c) c.replaceWith(Object.assign(document.createElement("div"), { innerHTML: errBox(e) })); });
    return;
  }
  if (k === "demo") {
    const ds = [...d.demos].sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
    if (!ds.length) { box.innerHTML = `<div class="card-h"><h2>${title}</h2></div>${empty("ما فيه جلسات تجريبية. ابدأ وحدة من صفحة الوضع التجريبي.", "play")}<div class="actions"><button class="primary sm" data-nav="demo">▶ الوضع التجريبي</button></div>`; return; }
    if (!DB.demo || !ds.some((x) => x.name === DB.demo)) DB.demo = ds[0].name;
    box.innerHTML = `<div class="card-h"><h2>${title} <small>جلسات تعيد تشغيل أسعار تاريخية محليًا</small></h2><button class="sm" data-nav="demo">افتح الوضع التجريبي</button></div>
      <div class="tw"><table><thead><tr><th>الجلسة</th><th>الاستراتيجية</th><th>الحالة</th><th>الرصيد</th><th>العائد</th><th>التنفيذات</th></tr></thead><tbody>
      ${ds.map((x) => `<tr data-db-demo="${esc(x.name)}" class="${x.name === DB.demo ? "sel" : ""}" style="cursor:pointer"><td><b>${esc(x.name)}</b></td><td>${esc(x.strategy)} <span class="muted">· ${esc(tfAr(x.timeframe))}</span></td>
        <td>${x.running ? '<span class="pill good">تعمل</span>' : `<span class="pill">${esc(x.state)}</span>`}</td><td class="ltr">${isNum(x.equity) ? fmtUsd(x.equity) : fmtUsd(x.capital)}</td>
        <td class="ltr ${sign(x.net_return)}">${fmtSPct(x.net_return, 2)}</td><td>${isNum(x.fills) ? fmtNum(x.fills, 0) : "—"}</td></tr>`).join("")}</tbody></table></div>
      <div class="chart" style="margin-top:12px"><canvas id="db-c"></canvas></div>`;
    box.querySelector("tbody").onclick = (e) => { const tr = e.target.closest("[data-db-demo]"); if (tr) { DB.demo = tr.dataset.dbDemo; dbDetail(); } };
    api("/api/demo/session?name=" + encodeURIComponent(DB.demo)).then((x) => { if (current !== "home" || DB.space !== "demo" || !$("#db-c")) return;
      const acc = css("--warn");
      if (!x.equity.length) { $("#db-c").replaceWith(Object.assign(document.createElement("div"), { className: "muted", textContent: "ما فيه منحنى رصيد بعد لهالجلسة." })); return; }
      lines($("#db-c"), [{ name: "الرصيد", points: x.equity.map((p) => [p.t, +p.equity]), color: acc, area: [acc + "44", acc + "00"] }], 240); }).catch(() => {});
    return;
  }
  if (k === "paper") {
    box.innerHTML = `<div class="card-h"><h2>${title} <small>كل جلسة برأس مالها الورقي</small></h2><button class="sm" data-nav="paper">إدارة الجلسات</button></div>${sessionTable(d.sessions)}
      ${d.sessions.length ? `<div class="chart" style="margin-top:12px"><canvas id="db-c"></canvas></div>` : ""}`;
    const live = d.sessions.find((x) => x.running) || d.sessions[0];
    if (live) api("/api/paper/session?name=" + encodeURIComponent(live.name)).then((x) => { if (current !== "home" || DB.space !== "paper" || !$("#db-c")) return;
      const b = x.bars.slice(-120); if (!b.length) return; candles($("#db-c"), b, [], [{ values: sma(b, 20), color: css("--sma1") }], 260); }).catch(() => {});
    return;
  }
  const r = d.runs[0];
  box.innerHTML = `<div class="card-h"><h2>${title} <small>آخر اختبار تاريخي</small></h2><button class="sm" data-nav="backtest">الاختبار التاريخي</button></div>
    ${r ? runSummary(r) : empty("ما فيه اختبارات بعد", "flask")}`;
}

/** Strategy comparison (B2): backtests side by side only inside a group with the same data, timeframe, period, mode and
    costs; groups are never ranked against each other, and a run without a comparable peer is listed apart. */
async function dbCompare() {
  const box = $("#db-compare"); if (!box) return;
  box.innerHTML = loading("تحميل المقارنة…");
  let d; try { d = await api("/api/compare"); } catch (e) { box.innerHTML = errBox(e); return; }
  if (current !== "home" || !$("#db-compare")) return;
  const pct = (x) => (isNum(x) ? fmtSPct(x) : "—");
  const head = (k) => `<span class="ltr" title="${esc(k.data_path)}">${esc(k.data)}</span> · ${esc(tfAr(k.timeframe))} · <span class="ltr">${esc(dbAt(k.start).slice(0, 10))} → ${esc(dbAt(k.end).slice(0, 10))}</span>
    · ${k.mode === "engine" ? "كامل عبر المحرك" : "سريع"} · رسوم <span class="ltr">${isNum(k.taker_fee) ? (+k.taker_fee * 100).toFixed(2) + "%" : "—"}</span> · انزلاق <span class="ltr">${esc(k.slippage_bps ?? "—")} bps</span>`;
  const table = (rs) => `<div class="tw"><table><thead><tr><th>الاستراتيجية</th><th>العائد</th><th>الاحتفاظ</th><th>أقصى تراجع</th><th>الصفقات</th><th>الرسوم</th><th>شارب</th><th>الحكم</th><th>وقت التجربة</th></tr></thead><tbody>
    ${rs.map((r) => `<tr><td><b>${esc(r.strategy)}</b>${Object.keys(r.params).length ? ` <code class="ltr muted" style="font-size:11px">${esc(JSON.stringify(r.params))}</code>` : ""}</td>
      <td class="ltr ${sign(r.net_return)}">${pct(r.net_return)}</td><td class="ltr">${pct(r.hold_return)}</td><td class="ltr bad">${isNum(r.max_drawdown) ? fmtPct(r.max_drawdown) : "—"}</td>
      <td class="ltr">${isNum(r.round_trips) ? fmtNum(r.round_trips, 0) : "—"}</td><td class="ltr">${isNum(r.fees_quote) ? fmtUsd(r.fees_quote) : "—"}</td><td class="ltr">${isNum(r.sharpe) ? fmtNum(r.sharpe) : "—"}</td>
      <td>${verdictPill(r.verdict)}</td><td><a href="#" data-run="${esc(r.id)}" class="ltr">${esc(dbAt(r.at))}</a></td></tr>`).join("")}</tbody></table></div>`;
  const comp = d.groups.filter((g) => g.comparable), alone = d.groups.filter((g) => !g.comparable);
  box.innerHTML = `<div class="card-h"><h2>مقارنة أداء الاستراتيجيات <small class="muted"><span class="ltr">${d.runs}</span> اختبار</small></h2></div>
    ${comp.length ? comp.map((g) => `<div class="cmp-g"><div class="cmp-h">${head(g.key)}</div>${table(g.runs)}</div>`).join("")
      : `<div class="muted">ما فيه اختبارين بنفس البيانات والفترة والإعدادات بعد. شغّل استراتيجيتين على نفس الملف والفترة عشان تظهر المقارنة هنا.</div>`}
    ${alone.length ? `<details class="cmp-alone"><summary>اختبارات بلا نظير قابل للمقارنة (<span class="ltr">${alone.length}</span>)</summary>
      ${alone.map((g) => `<div class="cmp-g"><div class="cmp-h">${head(g.key)}</div>${table(g.runs)}</div>`).join("")}</details>` : ""}
    ${d.missing.length ? `<div class="warn" style="font-size:12px;margin-top:6px">${d.missing.length} تجربة بدون ملف نتيجة، ما دخلت المقارنة.</div>` : ""}
    <div class="muted" style="font-size:12px;margin-top:8px">نقارن فقط داخل المجموعة الواحدة: نفس ملف البيانات والإطار والفترة ونوع الاختبار والرسوم والانزلاق. ما نرتّب المجموعات مقابل بعض. كل تجربة إضافية ترفع عتبة حكم المتانة.</div>`;
}

/** Status and alerts bar (B3): one line per recorded fact from /api/status (no network behind it). Stale data (a process
    alive but late) and a stopped process are worded apart; problems first, then information, then what is fine. */
const dbAgo = (s) => (!isNum(s) ? "غير معروف" : s < 3600 ? `قبل ${Math.max(1, Math.round(s / 60))} دقيقة` : s < 172800 ? `قبل ${Math.round(s / 3600)} ساعة` : `قبل ${Math.round(s / 86400)} يوم`);
const DB_ST = {
  live_disabled: () => "التداول الحقيقي معطّل",
  live_enabled: () => "التداول الحقيقي مفعّل",
  session_error: (i) => `جلسة ${i.name}: خطأ مسجّل ${i.at ? dbAt(i.at) : ""}`,
  paper_stale: (i) => `جلسة ${i.name} تعمل لكن بياناتها متأخرة (آخر دورة ${dbAgo(i.age_s)})`,
  paper_running: (i) => `جلسة ${i.name} تعمل`,
  paper_stopped: (i) => `جلسة ${i.name} متوقفة`,
  demo_error: (i) => `الجلسة التجريبية ${i.name}: خطأ`,
  price_unavailable: (i) => `سعر ${i.asset} غير متاح`,
  price_stale: (i) => `سعر ${i.asset} متأخر (${dbAgo(i.age_s)})`,
  collector_ok: (i) => `جامع الأدلة يعمل · آخر قيد ${dbAgo(i.age_s)}`,
  collector_stale: (i) => `جامع الأدلة متأخر · آخر قيد ${dbAgo(i.age_s)}`,
  collector_unavailable: () => "جامع الأدلة غير مرصود",
  dca_due: (i) => `شراء شهري مستحق اليوم: ${i.asset}`,
  dca_missed: (i) => `شراء شهري فات موعده: ${i.asset} (${i.count > 1 ? i.count + " أشهر، آخرها " : ""}${i.month})`,
  ui_errors: (i) => `${i.count} خطأ في الواجهة خلال 24 ساعة`,
  check_failed: (i) => `تعذّر فحص ${DB_ST_AREA[i.area] || i.area}`,
};
const DB_ST_AREA = { paper: "الجلسات الورقية", demo: "الجلسات التجريبية", prices: "الأسعار", collector: "جامع الأدلة", dca: "الشراء الشهري", errors: "سجل الأخطاء" };
const DB_ST_NAV = { paper: "paper", demo: "demo", prices: "portfolio", collector: "system", dca: "dca", errors: "logs" };
async function dbStatus() {
  const box = $("#db-status"); if (!box) return;
  let d; try { d = await api("/api/status"); } catch (e) { box.innerHTML = `<div class="stbar">${errBox(e)}</div>`; return; }
  DB.status = d;
  if (current !== "home" || !$("#db-status")) return;
  $("#db-status").innerHTML = dbStatusHtml(d);
}
function dbStatusHtml(d) {
  const order = { bad: 0, warn: 1, info: 2, ok: 3 }, tone = { bad: "bad", warn: "warn", info: "", ok: "good" };
  const items = [...d.items].sort((a, b) => order[a.level] - order[b.level]);
  const head = d.summary.bad ? `<span class="pill bad">${d.summary.bad} مشكلة</span>` : d.summary.warn ? `<span class="pill warn">${d.summary.warn} تنبيه</span>`
    : `<span class="pill good">كل شي سليم</span>`;
  return `<div class="stbar">${head}${items.map((i) => {
    const nav = DB_ST_NAV[i.area], text = (DB_ST[i.code] || (() => i.code))(i);
    return `<span class="pill ${tone[i.level]}" ${nav ? `data-nav="${nav}" style="cursor:pointer"` : ""} title="${esc(i.detail || "")}">${esc(text)}</span>`; }).join("")}
    <span class="muted stbar-at">تحديث <span class="ltr">${esc(dbAt(d.at))}</span> غرينتش</span></div>`;
}
