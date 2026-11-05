"use strict";
/* الوضع التجريبي (D32). A paper session replayed offline from local real bars through the same engine.
   Every number on this page comes from the engine's own files for the selected demo (status.json, equity.jsonl,
   bars.jsonl); nothing is simulated in the browser. */

const demoState = { active: null, poll: null };
const SPEED_LABEL = { "1": "1×", "2": "2×", "5": "5×", max: "أقصى" };

PAGES.demo = async () => {
  const [lst, st, df] = await Promise.all([api("/api/demo"), api("/api/strategies"), api("/api/data")]);
  const okS = st.strategies.filter((s) => s.ok);
  if (!demoState.active || !lst.demos.some((d) => d.name === demoState.active)) demoState.active = lst.demos[0]?.name || null;
  main.innerHTML = header("الوضع التجريبي", "تجربة كاملة بدون فلوس وبدون إنترنت: أسعار تاريخية حقيقية من ملفاتك تُعاد شمعة شمعة عبر نفس المحرك، وكل قرار يروح لمركز القرارات.",
    chip("بدون إنترنت", "good") + chip("ما ينرسل أي أمر", "") + chip("قبل 2025-09-07 فقط", ""))
  + `<div id="demo-live"></div>
  <div class="card" id="demo-new"><div class="card-h"><h2>تجربة جديدة <small>التجارب السابقة تبقى كما هي</small></h2></div>
    <div class="form">
      <div><label>الاستراتيجية</label><select id="dn-s">${okS.map((s) => `<option value="${esc(s.spec)}" data-tf="${esc(s.timeframe)}">${esc(s.name)} (${esc(tfAr(s.timeframe))})</option>`).join("")}</select></div>
      <div><label>البيانات (محلية)</label><select id="dn-d">${df.files.map((f) => `<option value="${esc(f.path)}" data-tf="${esc(f.timeframe || "")}">${esc(f.name)}</option>`).join("")}</select></div>
      <div><label>تبدأ من (اختياري)</label><input id="dn-from" type="date" max="2025-09-05"></div>
      <div><label>رأس المال الافتراضي (USDT)</label><input id="dn-cap" class="ltr" value="${esc(lst.default_capital)}"></div>
      <div><label>السرعة</label><select id="dn-speed">${lst.speeds.map((v) => `<option value="${v}" ${v === "2" ? "selected" : ""}>${SPEED_LABEL[v] || v}</option>`).join("")}</select></div>
    </div>
    <div class="actions"><button class="primary" id="dn-go">▶ ابدأ التجربة</button>
      <span class="muted" style="font-size:12.5px">بدون تاريخ: آخر ~500 شمعة قبل الفترة المختومة. كل شمعة تمر على المحرك كامل، فالسرعة الفعلية تعتمد على جهازك.</span></div>
    <div id="dn-msg"></div></div>
  <div class="card"><div class="card-h"><h2>التجارب <small>${lst.demos.length}</small></h2></div>${lst.demos.length ? `<div class="tw"><table><thead><tr><th>التجربة</th><th>الاستراتيجية</th><th>الحالة</th><th>الرصيد</th><th>العائد</th><th>عمليات التنفيذ</th><th>الساعة التجريبية</th></tr></thead><tbody>
    ${lst.demos.map((d) => `<tr data-demo="${esc(d.name)}" class="${d.name === demoState.active ? "sel" : ""}" style="cursor:pointer"><td class="ltr"><b>${esc(d.name)}</b></td><td>${esc(d.strategy)} <span class="muted">· ${esc(tfAr(d.timeframe))}</span></td>
    <td>${demoStatePill(d.state, d.running)}</td><td class="ltr">${isNum(d.equity) ? fmtUsd(d.equity) : fmtUsd(d.capital)}</td><td class="ltr ${sign(d.net_return)}">${fmtSPct(d.net_return, 2)}</td>
    <td>${d.fills ?? "—"}</td><td class="ltr muted">${esc((d.clock || "—").slice(0, 16).replace("T", " "))}</td></tr>`).join("")}</tbody></table></div>`
    : empty("ما فيه تجارب بعد. ابدأ وحدة من فوق.", "play")}</div>`;
  const match = () => { const tf = $("#dn-s").selectedOptions[0]?.dataset.tf; const o = [...$("#dn-d").options].find((x) => x.dataset.tf === tf);
    if (o) $("#dn-d").value = o.value; $("#dn-msg").innerHTML = o || !tf ? "" : `<div class="err">ما فيه ملف بيانات بإطار ${esc(tfAr(tf))}.</div>`; };
  $("#dn-s").onchange = match; match();
  $("#dn-go").onclick = async () => {
    $("#dn-go").disabled = true; $("#dn-msg").innerHTML = loading("تجهيز التجربة (تحميل البيانات وتهيئة المؤشرات)…");
    try {
      const res = await api("/api/demo/new", { spec: $("#dn-s").value, data: $("#dn-d").value, start: $("#dn-from").value || null,
        capital: $("#dn-cap").value, speed: $("#dn-speed").value, autostart: true });
      demoState.active = res.demo.name; await PAGES.demo();
    } catch (e) { $("#dn-msg").innerHTML = errBox(e); $("#dn-go").disabled = false; }
  };
  document.querySelectorAll("[data-demo]").forEach((tr) => tr.onclick = () => { demoState.active = tr.dataset.demo; PAGES.demo(); });
  if (demoState.active) await demoDraw(true);
  clearInterval(timer);
  timer = setInterval(() => { if (current === "demo" && demoState.active) demoDraw(false).catch(() => {}); }, 2500);
};

function demoStatePill(state, running) {
  if (running || state === "running") return '<span class="pill good">تعمل</span>';
  return { paused: '<span class="pill">متوقفة مؤقتًا</span>', finished: '<span class="pill warn">انتهت البيانات</span>', error: '<span class="pill bad">خطأ</span>' }[state] || `<span class="pill">${esc(state)}</span>`;
}

async function demoDraw(full) {
  const name = demoState.active, box = $("#demo-live"); if (!box || !name) return;
  const d = await api("/api/demo/session?name=" + encodeURIComponent(name));
  if (current !== "demo" || demoState.active !== name || !$("#demo-live")) return;
  const doc = d.demo, s = d.status || {}, cap = +doc.capital, eq = isNum(s.equity_quote) ? +s.equity_quote : cap, pnl = eq - cap;
  const pct = d.progress.total ? d.progress.done / d.progress.total : 0, running = d.running;
  const lastBar = d.bars[d.bars.length - 1];
  const html = `<div class="card"><div class="card-h"><h2>${esc(doc.strategy)} <small class="ltr">${esc(name)}</small> ${demoStatePill(doc.state, running)}</h2>
      <div class="actions" style="margin:0">
        ${running ? `<button class="sm" data-dact="pause">⏸ إيقاف مؤقت</button>` : doc.state === "finished" ? "" : `<button class="sm ok" data-dact="start">▶ تشغيل</button><button class="sm" data-dact="step">⏭ شمعة واحدة</button>`}
        <div class="tabs" id="demo-speed">${Object.keys(SPEED_LABEL).map((v) => `<button data-speed="${v}" class="${doc.speed === v ? "on" : ""}">${SPEED_LABEL[v]}</button>`).join("")}</div>
        <button class="sm" data-dc-source="demo:${esc(name)}">قراراتها في مركز القرارات ←</button>
        <button class="sm danger" data-dact="delete">حذف</button></div></div>
    ${doc.state === "error" ? `<div class="err">توقفت التجربة بسبب خطأ: ${esc(trErr(doc.error).ar || "خطأ غير معروف")}<details class="tech"><summary>تفاصيل تقنية</summary><pre>${esc(doc.error)}</pre></details></div>` : ""}
    <div class="demo-progress"><div style="width:${(pct * 100).toFixed(1)}%"></div></div>
    <div class="muted" style="font-size:12.5px;margin:6px 0 14px">الساعة التجريبية: <span class="ltr">${esc((doc.clock || doc.replay_from).slice(0, 16).replace("T", " "))} غرينتش</span> ·
      ${d.progress.done} من ${d.progress.total} شمعة · البيانات من <span class="ltr">${esc(doc.replay_from.slice(0, 10))}</span> إلى <span class="ltr">${esc(doc.replay_to.slice(0, 10))}</span></div>
    <div class="kpis">
      ${kpi({ k: "الرصيد", v: fmtUsd(eq), ic: "wallet", s: `رأس المال ${fmtUsd(cap, 0)}`, spark: "dsp" })}
      ${kpi({ k: "الربح / الخسارة", v: (pnl > 0 ? "+" : "") + fmtUsd(pnl), cls: sign(pnl), ic: pnl >= 0 ? "trend" : "down", tone: pnl >= 0 ? "g" : "r", s: fmtSPct(cap ? pnl / cap : null, 2) })}
      ${kpi({ k: "آخر سعر", v: lastBar ? fmtUsd(lastBar.c) : "—", ic: "trend", s: esc(doc.timeframe ? tfAr(doc.timeframe) : "") })}
      ${kpi({ k: "المركز المفتوح", v: posText(s.position), ic: "box", small: true })}
      ${kpi({ k: "عمليات التنفيذ", v: String(s.fills ?? 0), ic: "coins", s: `أقصى تراجع ${fmtPct(s.max_drawdown)}` })}
    </div>
    <div class="cols"><div><div class="card-h"><h2>السعر والصفقات</h2></div><canvas id="dch"></canvas></div>
      <div><div class="card-h"><h2>الأداء <small>الرصيد مقابل الاحتفاظ</small></h2></div><canvas id="deq"></canvas></div></div>
    <div class="card-h" style="margin-top:14px"><h2>آخر الصفقات</h2></div>
    ${(s.trades || []).length ? `<div class="tw" style="max-height:260px"><table><thead><tr><th>دخول</th><th>سعر</th><th>خروج</th><th>سعر</th><th>كمية</th><th>ربح/خسارة</th></tr></thead><tbody>
      ${s.trades.slice().reverse().map((t) => `<tr><td class="ltr">${esc((t.entry_time || "").slice(0, 16).replace("T", " "))}</td><td class="ltr">${fmtNum(t.entry_price)}</td>
      <td class="ltr">${t.exit_time ? esc(t.exit_time.slice(0, 16).replace("T", " ")) : '<span class="pill warn">مفتوحة</span>'}</td><td class="ltr">${fmtNum(t.exit_price)}</td>
      <td class="ltr">${fmtNum(t.quantity, 5)}</td><td class="ltr ${sign(t.pnl_quote)}">${isNum(t.pnl_quote) ? fmtNum(t.pnl_quote) : "—"}</td></tr>`).join("")}</tbody></table></div>`
      : empty("ما فيه صفقات بعد. شغّل التجربة وانتظر إشارة من الاستراتيجية.", "box")}</div>`;
  box.innerHTML = html;
  const bars = d.bars.slice(-160);
  if (bars.length) candles($("#dch"), bars, s.trades || [], [{ values: sma(bars, 20), color: css("--sma1") }], 300);
  if (d.equity.length > 1) {
    const p0 = +d.equity[0].price, e0 = +d.equity[0].equity, accent = css("--accent");
    lines($("#deq"), [{ name: "الرصيد", points: d.equity.map((x) => [x.t, +x.equity]), color: accent, area: [accent + "55", accent + "00"] },
      { name: "الاحتفاظ", points: d.equity.map((x) => [x.t, e0 * +x.price / p0]), color: css("--hold"), dash: true, width: 1.5 }], 300);
    spark($("#dsp"), thin(d.equity).map((x) => +x.equity), pnl >= 0 ? css("--good") : css("--bad"));
  } else $("#deq").replaceWith(Object.assign(document.createElement("div"), { className: "muted", textContent: "يظهر منحنى الأداء بعد أول شمعتين." }));
  box.querySelectorAll("[data-dact]").forEach((b) => b.onclick = async () => {
    const act = b.dataset.dact;
    try {
      if (act === "delete") {
        if (!(await uiConfirm(`تحذف التجربة ${name} نهائيًا مع سجلاتها؟ ما يمكن التراجع.`, "حذف نهائي"))) return;
        await api("/api/demo/delete", { name, confirm: name }); demoState.active = null; return PAGES.demo();
      }
      b.disabled = true;
      await api("/api/demo/" + act, { name, ...(act === "step" ? { n: 1 } : {}) });
    } catch (e) { box.insertAdjacentHTML("afterbegin", errBox(e)); }
    demoDraw(true);
  });
  $("#demo-speed").onclick = async (e) => { const b = e.target.closest("[data-speed]"); if (!b) return;
    try { await api("/api/demo/speed", { name, speed: b.dataset.speed }); } catch (er) { box.insertAdjacentHTML("afterbegin", errBox(er)); } demoDraw(true); };
}
