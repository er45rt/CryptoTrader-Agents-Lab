"use strict";
/* محفظتي + مشترياتي الشهرية (D40, phase 1). The owner's REAL holdings, kept apart from Demo, Paper and Backtest.
   Every number comes from the local ledger (portfolio.py / dca.py) and Binance public prices (prices.py); when a price
   is missing the page says so with the reason - nothing is estimated in the browser except the SAR view
   (1 USDT ≈ 3.75 SAR, labelled as approximate). No buy/sell/connect button exists: this page records, never trades. */

const SAR_RATE = 3.75;
const pfState = { sar: false, txType: "all", txAsset: "", asset: null };
const TX_LABEL = { buy: "شراء", sell: "بيع", transfer: "تحويل بين محافظي", fee: "رسوم" };
const fmtQ = (x) => (isNum(x) ? Number(x).toLocaleString("en-US", { maximumFractionDigits: 8 }) : "—");
const money = (x, d = 2) => {
  if (!isNum(x)) return "—";
  const v = pfState.sar ? +x * SAR_RATE : +x;
  return (v < 0 ? "-" : "") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }) + (pfState.sar ? " SAR" : " USDT");
};
const sMoney = (x) => (isNum(x) && +x > 0 ? "+" : "") + money(x);
const pctTxt = (x) => (isNum(x) ? (+x > 0 ? "+" : "") + Number(x).toFixed(2) + "%" : "—");
const NA = (why) => `<span class="pill warn" title="${esc(why || "")}">غير متاح</span>${why ? ` <span class="muted" style="font-size:12px">${esc(why)}</span>` : ""}`;
const priceNote = (a) => (a.price_at ? `${esc(a.price_source || "")} · ${esc(String(a.price_at).slice(0, 16).replace("T", " "))} UTC` : "");
const sourceChips = () => chip("محلي على جهازك فقط", "good") + chip("الأسعار: Binance العامة (قراءة فقط)", "") + chip("لا يُرسل أي أمر", "");

PAGES.portfolio = async () => {
  main.innerHTML = loading("تحميل المحفظة والأسعار…");
  const [doc, plans] = await Promise.all([api("/api/portfolio", undefined, 30000), api("/api/dca", undefined, 30000)]);
  pfState.plans = plans.plans;
  const t = doc.totals, held = doc.assets.filter((a) => !a.closed), closed = doc.assets.filter((a) => a.closed);
  const batches = [...new Map(doc.transactions.filter((x) => x.batch).map((x) => [x.batch, x])).values()];
  main.innerHTML = header("محفظتي", "ممتلكاتك الحقيقية كما سجّلتها أنت. منفصلة تمامًا عن الوضع التجريبي والتداول الورقي والاختبار التاريخي.", sourceChips())
  + `<div class="actions" style="margin-bottom:12px"><div class="tabs" id="pf-cur"><button data-c="usdt" class="${pfState.sar ? "" : "on"}">USDT</button><button data-c="sar" class="${pfState.sar ? "on" : ""}">SAR تقريبي</button></div>
     ${pfState.sar ? `<span class="muted" style="font-size:12.5px">الريال تقريبي: 1 USDT ≈ 3.75 SAR (ربط الدولار، وUSDT قريبة من الدولار وليست مساوية له بالضبط).</span>` : ""}</div>
  ${doc.unpriced.length ? note(`القيمة لا تشمل ${doc.unpriced.map(esc).join("، ")}: ما فيه سعر متاح لها حاليًا، فما دخلت في القيمة ولا في الربح غير المحقق.`) : ""}
  <div class="kpis">
    ${kpi({ k: "قيمة المحفظة (المسعّرة)", v: t.value === null ? "غير متاح" : money(t.value), ic: "wallet", s: doc.n_transactions ? `${held.length} أصل · ${doc.n_transactions} عملية` : "سجّل أول عملية من تحت" })}
    ${kpi({ k: "ربح/خسارة غير محقق", v: t.unrealized === null ? "غير متاح" : sMoney(t.unrealized), cls: sign(t.unrealized), ic: "trend", s: pctTxt(t.unrealized_pct) + " على التكلفة المسعّرة" })}
    ${kpi({ k: "ربح/خسارة محقق", v: sMoney(t.realized), cls: sign(t.realized), ic: "check", s: "من البيع والرسوم المدفوعة بالعملة" })}
    ${kpi({ k: "تكلفة الأصول الحالية", v: money(t.basis), ic: "layers", s: "متوسط التكلفة المرجّح" })}
    ${kpi({ k: "إجمالي المبالغ المستثمرة", v: money(t.invested), ic: "info", s: "كل المشتريات مع رسومها · الرسوم " + money(t.fees_quote) })}
  </div>
  <div class="cols">
    <div class="card"><div class="card-h"><h2>تطور القيمة <small>إغلاق يومي · Binance العامة</small></h2><div class="tabs" id="pf-rng"></div></div>
      <div class="legend" style="margin-bottom:8px"><span><i style="background:var(--accent)"></i>القيمة</span><span><i class="dash"></i>التكلفة</span></div>
      <div id="pf-hist">${doc.n_transactions ? loading("تحميل الأسعار اليومية…") : empty("يظهر الرسم بعد أول عملية.", "trend")}</div></div>
    <div class="card"><div class="card-h"><h2>التوزيع <small>نسبة من القيمة المسعّرة</small></h2></div>${allocation(held)}</div>
  </div>
  <div class="card"><div class="card-h"><h2>الأصول <small>اضغط على أصل تشوف رسمه وعملياته</small></h2></div>${holdingsTable(held)}
    ${closed.length ? `<details style="margin-top:10px"><summary class="muted">مراكز مغلقة (${closed.length})</summary>${holdingsTable(closed)}</details>` : ""}</div>
  <div id="pf-asset"></div>
  <div class="card"><div class="card-h"><h2>سجل العمليات <small>${doc.n_transactions}</small></h2>
    <div class="actions"><select id="pf-ft"><option value="all">كل الأنواع</option>${Object.entries(TX_LABEL).map(([k, v]) => `<option value="${k}" ${pfState.txType === k ? "selected" : ""}>${v}</option>`).join("")}</select>
    <select id="pf-fa"><option value="">كل الأصول</option>${doc.assets.map((a) => `<option ${pfState.txAsset === a.asset ? "selected" : ""}>${esc(a.asset)}</option>`).join("")}</select></div></div>
    <div id="pf-tx"></div></div>
  <div class="cols">
    <div class="card"><div class="card-h"><h2>إضافة عملية <small>عمليات منفّذة فعلًا فقط</small></h2></div>${txForm(plans.plans)}<div id="pf-msg"></div></div>
    <div class="card"><div class="card-h"><h2>استيراد CSV <small>معاينة قبل أي حفظ</small></h2></div>
      <div class="muted" style="font-size:12.5px;margin-bottom:8px">الأعمدة: <span class="ltr">date,type,asset,quantity,price,fee,fee_asset,network,wallet,note</span> — النوع: <span class="ltr">buy / sell / transfer / fee</span>. الاستيراد كله أو لا شيء، ويمكن التراجع عنه كاملًا.</div>
      <div class="actions"><input type="file" id="pf-file" accept=".csv,text/csv"><label class="muted"><input type="checkbox" id="pf-skip"> تخطَّ العمليات المكررة</label></div>
      <div id="pf-prev"></div>
      ${batches.length ? `<div style="margin-top:10px"><b>استيرادات سابقة</b>${batches.map((b) => `<div class="actions" style="margin-top:6px"><span class="ltr muted">${esc(b.source)} · ${esc(b.batch)}</span><button data-undo="${esc(b.batch)}">تراجع عن هذا الاستيراد</button></div>`).join("")}</div>` : ""}</div>
  </div>
  <div class="note">${svg("info")}<div>حساب الربح بمتوسط التكلفة المرجّح: البيع لا يغيّر المتوسط، والتحويل بين محافظك ليس ربحًا، ورسوم الشبكة تكلفة. ملفك: <span class="ltr">${esc(doc.storage)}</span></div></div>`;
  $("#pf-cur").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; pfState.sar = b.dataset.c === "sar"; PAGES.portfolio(); };
  const renderTx = () => { $("#pf-tx").innerHTML = txTable(doc.transactions.filter((x) => (pfState.txType === "all" || x.type === pfState.txType) && (!pfState.txAsset || x.asset === pfState.txAsset)), plans.plans); };
  $("#pf-ft").onchange = (e) => { pfState.txType = e.target.value; renderTx(); };
  $("#pf-fa").onchange = (e) => { pfState.txAsset = e.target.value; renderTx(); };
  renderTx();
  main.onclick = async (e) => {
    const row = e.target.closest("[data-asset]");
    if (row) { pfState.asset = row.dataset.asset; return assetDetail(row.dataset.asset); }
    const del = e.target.closest("[data-del]");
    if (del && await uiConfirm("حذف هذه العملية من سجلك؟ يُرفض الحذف لو كانت عملية لاحقة تعتمد عليها.", "حذف")) {
      try { await api("/api/portfolio/delete", { id: del.dataset.del }); PAGES.portfolio(); } catch (err) { $("#pf-msg").innerHTML = errBox(err); }
    }
    const undo = e.target.closest("[data-undo]");
    if (undo && await uiConfirm("التراجع يحذف كل عمليات هذا الاستيراد فقط.", "تراجع")) {
      try { await api("/api/portfolio/import/undo", { batch: undo.dataset.undo }); PAGES.portfolio(); } catch (err) { $("#pf-prev").innerHTML = errBox(err); }
    }
  };
  wireTxForm(); wireImport();
  if (doc.n_transactions) loadHistory();
  if (pfState.asset && doc.assets.some((a) => a.asset === pfState.asset)) assetDetail(pfState.asset);
};

function allocation(held) {
  const rows = held.filter((a) => isNum(a.allocation_pct)).sort((a, b) => b.allocation_pct - a.allocation_pct);
  const miss = held.filter((a) => !isNum(a.allocation_pct));
  if (!held.length) return empty("ما فيه أصول بعد.", "wallet");
  return rows.map((a) => `<div class="alloc" data-asset="${esc(a.asset)}" style="cursor:pointer;margin:8px 0"><div class="actions" style="justify-content:space-between"><b class="ltr">${esc(a.asset)}</b><span class="ltr">${Number(a.allocation_pct).toFixed(2)}% · ${money(a.value)}</span></div>
    <div style="height:8px;border-radius:6px;background:var(--panel2)"><div style="height:8px;border-radius:6px;width:${Math.max(1, +a.allocation_pct)}%;background:var(--accent)"></div></div></div>`).join("")
    + miss.map((a) => `<div class="actions" style="justify-content:space-between;margin:8px 0" data-asset="${esc(a.asset)}"><b class="ltr">${esc(a.asset)}</b>${NA(a.price_missing)}</div>`).join("");
}

function holdingsTable(list) {
  return `<div class="tw"><table><thead><tr><th>الأصل</th><th>الشبكة</th><th>الكمية</th><th>السعر الحالي</th><th>القيمة</th><th>متوسط التكلفة</th><th>التكلفة</th><th>غير محقق</th><th>محقق</th></tr></thead><tbody>
  ${list.map((a) => `<tr data-asset="${esc(a.asset)}" style="cursor:pointer"><td class="ltr"><b>${esc(a.asset)}</b></td><td class="muted">${esc(a.networks.join(", ") || "—")}</td>
    <td class="ltr">${fmtQ(a.qty)}</td><td class="ltr">${a.closed ? "—" : a.price === null ? NA(a.price_missing) : `${fmtQ(a.price)}<div class="muted" style="font-size:11px">${priceNote(a)}</div>`}</td>
    <td class="ltr">${a.value === null ? (a.closed ? "—" : "غير متاح") : money(a.value)}</td><td class="ltr">${a.avg_cost === null ? "—" : fmtQ(a.avg_cost)}</td><td class="ltr">${money(a.basis)}</td>
    <td class="ltr ${sign(a.unrealized)}">${a.unrealized === null ? "—" : `${sMoney(a.unrealized)} <small>${pctTxt(a.unrealized_pct)}</small>`}</td><td class="ltr ${sign(a.realized)}">${sMoney(a.realized)}</td></tr>`).join("")}</tbody></table></div>`;
}

function txTable(txs, plans) {
  if (!txs.length) return empty("ما فيه عمليات بهذا التصفية.", "layers");
  const planName = (id) => { const p = plans.find((x) => x.id === id); return p ? `${p.asset} · ${p.amount} USDT` : id; };
  return `<div class="tw"><table><thead><tr><th>التاريخ</th><th>النوع</th><th>الأصل</th><th>الكمية</th><th>السعر</th><th>الرسوم</th><th>المحفظة / الشبكة</th><th>المصدر</th><th></th></tr></thead><tbody>
  ${txs.map((x) => `<tr><td class="ltr">${esc(x.at.slice(0, 16).replace("T", " "))}</td><td>${TX_LABEL[x.type]}${x.dca_plan ? ` <span class="pill">شهري: ${esc(planName(x.dca_plan))}</span>` : ""}</td>
    <td class="ltr"><b>${esc(x.asset)}</b></td><td class="ltr">${fmtQ(x.qty)}</td><td class="ltr">${x.price ? fmtQ(x.price) : "—"}</td><td class="ltr">${+x.fee ? `${fmtQ(x.fee)} ${esc(x.fee_asset)}` : "—"}</td>
    <td class="muted">${esc([x.wallet, x.network].filter(Boolean).join(" · ") || "—")}${x.note ? `<div style="font-size:11px">${esc(x.note)}</div>` : ""}</td>
    <td class="muted ltr" style="font-size:11.5px">${esc(x.source)}</td><td><button data-del="${esc(x.id)}" title="حذف">✕</button></td></tr>`).join("")}</tbody></table></div>`;
}

function txForm(plans) {
  const now = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  return `<div class="form">
    <div><label>النوع</label><select id="tf-type">${Object.entries(TX_LABEL).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></div>
    <div><label>الأصل</label><input id="tf-asset" class="ltr" placeholder="BTC"></div>
    <div><label>التاريخ والوقت (توقيت جهازك)</label><input id="tf-at" type="datetime-local" value="${now}" max="${now}"></div>
    <div data-for="buy sell transfer"><label>الكمية</label><input id="tf-qty" class="ltr" inputmode="decimal"></div>
    <div data-for="buy sell"><label>السعر (USDT لكل وحدة)</label><input id="tf-price" class="ltr" inputmode="decimal"></div>
    <div><label>الرسوم</label><input id="tf-fee" class="ltr" inputmode="decimal" placeholder="0"></div>
    <div data-for="buy sell"><label>عملة الرسوم</label><select id="tf-fa"><option value="USDT">USDT</option><option value="ASSET">نفس الأصل</option></select></div>
    <div><label>المحفظة</label><input id="tf-wallet" placeholder="Binance / Ledger"></div>
    <div><label>الشبكة</label><input id="tf-net" class="ltr" placeholder="Bitcoin / Arbitrum"></div>
    <div data-for="buy"><label>ضمن خطة شهرية</label><select id="tf-plan"><option value="">لا</option>${plans.filter((p) => p.active).map((p) => `<option value="${esc(p.id)}" data-a="${esc(p.asset)}">${esc(p.asset)} · ${esc(p.amount)} USDT يوم ${p.day}</option>`).join("")}</select></div>
    <div><label>ملاحظة</label><input id="tf-note"></div>
  </div><div class="actions"><button class="primary" id="tf-go">حفظ العملية</button><span class="muted" style="font-size:12.5px" id="tf-hint"></span></div>`;
}

function wireTxForm() {
  const sync = () => { const ty = $("#tf-type").value; document.querySelectorAll("[data-for]").forEach((d) => { d.style.display = d.dataset.for.split(" ").includes(ty) ? "" : "none"; });
    $("#tf-hint").textContent = ty === "transfer" ? "التحويل بين محافظك ما يغيّر الكمية ولا المتوسط؛ الرسوم بنفس الأصل." : ty === "fee" ? "رسوم مدفوعة بالأصل نفسه (مثل رسوم شبكة)." : ""; };
  $("#tf-type").onchange = sync; sync();
  const plansFor = () => { const a = $("#tf-asset").value.trim().toUpperCase();   // only plans of the typed asset
    [...$("#tf-plan").options].forEach((o) => { o.hidden = !!o.value && o.dataset.a !== a; });
    if ($("#tf-plan").selectedOptions[0]?.hidden) $("#tf-plan").value = ""; };
  $("#tf-asset").oninput = plansFor; plansFor();
  $("#tf-go").onclick = async () => {
    const ty = $("#tf-type").value, asset = $("#tf-asset").value.trim().toUpperCase();
    const body = { type: ty, asset, at: $("#tf-at").value ? new Date($("#tf-at").value).toISOString() : "", fee: $("#tf-fee").value,
      wallet: $("#tf-wallet").value, network: $("#tf-net").value, note: $("#tf-note").value };
    if (ty !== "fee") body.qty = $("#tf-qty").value;
    if (ty === "buy" || ty === "sell") { body.price = $("#tf-price").value; body.fee_asset = $("#tf-fa").value === "ASSET" ? asset : "USDT"; }
    if (ty === "fee" || ty === "transfer") body.fee_asset = asset;
    if (ty === "buy" && $("#tf-plan").value) body.dca_plan = $("#tf-plan").value;
    $("#tf-go").disabled = true;
    try { await api("/api/portfolio/add", body); PAGES.portfolio(); }
    catch (e) { $("#pf-msg").innerHTML = errBox(e); $("#tf-go").disabled = false; }
  };
}

function wireImport() {
  let text = "", name = "";
  const show = async () => {
    if (!text) return;
    $("#pf-prev").innerHTML = loading("معاينة الملف…");
    try {
      const p = await api("/api/portfolio/import/preview", { text, skip_duplicates: $("#pf-skip").checked });
      const bad = p.rows.filter((r) => r.error || r.duplicate);
      $("#pf-prev").innerHTML = `<div class="actions" style="margin:8px 0">${chip(`${p.n_rows} صف`, "")}${chip(`${p.n_ok} جاهز`, "good")}${p.n_errors ? chip(`${p.n_errors} خطأ`, "bad") : ""}${p.n_duplicates ? chip(`${p.n_duplicates} مكرر`, "warn") : ""}</div>
        ${p.already_imported ? `<div class="err">هذا الملف مستورد من قبل.</div>` : ""}${p.replay_error ? `<div class="err">${esc(p.replay_error)}</div>` : ""}
        ${bad.length ? `<div class="tw"><table><thead><tr><th>السطر</th><th>المشكلة</th></tr></thead><tbody>${bad.map((r) => `<tr><td class="ltr">${r.line}</td><td>${r.error ? esc(r.error) : r.duplicate === "existing" ? "مكرر: موجود في سجلك" : "مكرر داخل الملف"}</td></tr>`).join("")}</tbody></table></div>` : ""}
        <div class="actions" style="margin-top:8px"><button class="primary" id="pf-imp" ${p.can_import ? "" : "disabled"}>استيراد ${p.n_ok} عملية</button></div>`;
      if (p.can_import) $("#pf-imp").onclick = async () => {
        $("#pf-imp").disabled = true;
        try { await api("/api/portfolio/import/confirm", { text, sha256: p.sha256, skip_duplicates: $("#pf-skip").checked, filename: name }); PAGES.portfolio(); }
        catch (e) { $("#pf-prev").innerHTML = errBox(e); }
      };
    } catch (e) { $("#pf-prev").innerHTML = errBox(e); }
  };
  $("#pf-file").onchange = async (e) => { const f = e.target.files[0]; if (!f) return; if (f.size > 1900000) { $("#pf-prev").innerHTML = errBox("الملف أكبر من 1.9 ميجابايت."); return; } name = f.name; text = await f.text(); show(); };
  $("#pf-skip").onchange = show;
}

async function loadHistory() {
  const el = $("#pf-hist");
  try {
    const h = await api("/api/portfolio/history", undefined, 60000);
    if (!h.points.length) { el.innerHTML = empty("ما فيه أيام مغلقة بعد أول عملية.", "trend"); return; }
    const miss = Object.entries(h.missing);
    const partial = h.points.filter((p) => !p.complete).length;
    el.innerHTML = `<canvas id="pf-c"></canvas>${miss.length || partial ? `<div class="muted" style="font-size:12px;margin-top:6px">${partial} يوم بقيمة ناقصة (أصل بدون سعر ذاك اليوم)${miss.length ? " · " + miss.map(([a, r]) => `${esc(a)}: ${esc(r)}`).join(" · ") : ""}</div>` : ""}`;
    const k = pfState.sar ? SAR_RATE : 1, ms = h.points.map((p) => msOf(p.d)), view = makeView(ms[0], Math.max(ms[ms.length - 1], ms[0] + 86400e3));
    rangeButtons(view, "pf-rng");
    const accent = css("--accent");
    lines($("#pf-c"), [{ name: "القيمة", points: h.points.map((p) => [p.d, +p.value * k]), color: accent, area: [accent + "44", accent + "00"] },
      { name: "التكلفة", points: h.points.map((p) => [p.d, +p.basis * k]), color: css("--hold"), dash: true, width: 1.5 }], 240, {}, view);
  } catch (e) { el.innerHTML = errBox(e); }
}

async function assetDetail(asset) {
  const el = $("#pf-asset"); if (!el) return;
  el.innerHTML = `<div class="card">${loading("تحميل " + esc(asset) + "…")}</div>`;
  try {
    const d = await api("/api/portfolio/asset?asset=" + encodeURIComponent(asset), undefined, 60000);
    const pts = Object.entries(d.closes).sort();
    el.innerHTML = `<div class="card"><div class="card-h"><h2><span class="ltr">${esc(asset)}/USDT</span> <small>إغلاق يومي · ${esc(d.source)}</small></h2><div class="actions"><div class="tabs" id="pa-rng"></div><button id="pa-x">إغلاق</button></div></div>
      ${pts.length ? `<canvas id="pa-c"></canvas><div class="muted" style="font-size:12px">النقاط على الخط: عملياتك (أخضر شراء، أحمر بيع).</div>` : `<div>${NA(d.reason)}</div>`}
      ${txTable(d.transactions, pfState.plans || [])}</div>`;
    $("#pa-x").onclick = () => { pfState.asset = null; el.innerHTML = ""; };
    if (pts.length) {
      const ms = pts.map((p) => msOf(p[0])), view = makeView(ms[0], Math.max(ms[ms.length - 1], ms[0] + 86400e3));
      rangeButtons(view, "pa-rng");
      const draw = lines($("#pa-c"), [{ name: asset, points: pts.map(([t, c]) => [t, +c]), color: css("--accent") }], 260, {}, view);
      const marks = d.transactions.filter((x) => x.price && (x.type === "buy" || x.type === "sell"));
      if (marks.length) {   // trade markers drawn on the same canvas after each redraw
        const c = $("#pa-c"), paint = () => { const g = c.getContext("2d"), w = c.clientWidth, L = 8, R = w - 72, T = 10, B = 260 - 26;
          const vals = pts.filter(([t]) => msOf(t) >= view.t0 && msOf(t) <= view.t1).map(([, v]) => +v); if (!vals.length) return;
          let lo = Math.min(...vals), hi = Math.max(...vals); const pad = (hi - lo) * 0.06 || 0.01; lo -= pad; hi += pad;
          marks.forEach((m) => { const t = msOf(m.at); if (t < view.t0 || t > view.t1) return; const x = L + (t - view.t0) / ((view.t1 - view.t0) || 1) * (R - L), y = yScale(lo, hi, T, B)(+m.price);
            g.beginPath(); g.arc(x, y, 4, 0, 7); g.fillStyle = m.type === "buy" ? css("--good") : css("--bad"); g.fill(); }); };
        paint(); view.subs.push(paint); void draw;
      }
    }
    el.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) { el.innerHTML = `<div class="card">${errBox(e)}</div>`; }
}

/* ---------- مشترياتي الشهرية ---------- */
const DCA_STATE = { executed: ["good", "منفّذ"], missed: ["bad", "فائت"], due: ["warn", "مستحق اليوم"], upcoming: ["", "قادم"] };

PAGES.dca = async () => {
  main.innerHTML = loading("تحميل الخطط…");
  const [st, doc] = await Promise.all([api("/api/dca", undefined, 30000), api("/api/portfolio", undefined, 30000)]);
  main.innerHTML = header("مشترياتي الشهرية", "متابعة خطط الشراء الشهري: المخطط مقابل المنفّذ فعلًا. للمتابعة فقط — التطبيق ما ينفّذ أي شراء.", chip("متابعة فقط", "good") + chip("المنفّذ = عمليات محفظتي نفسها", ""))
  + (st.reminders.length ? `<div class="card"><div class="card-h"><h2>تنبيهات <small>${st.reminders.length}</small></h2></div>${st.reminders.map((r) => `<div class="actions" style="margin:4px 0"><span class="pill ${DCA_STATE[r.state][0]}">${DCA_STATE[r.state][1]}</span><b class="ltr">${esc(r.asset)}</b><span class="ltr muted">${esc(r.planned_date)}</span></div>`).join("")}
      <div class="muted" style="font-size:12px;margin-top:6px">لو اشتريت فعلًا: سجّل الشراء في محفظتي واختر الخطة، أو اربط عملية موجودة من تحت.</div></div>` : "")
  + (st.plans.length ? st.plans.map((p) => planCard(p, doc.transactions)).join("") : `<div class="card">${empty("ما فيه خطط بعد. أنشئ خطة من تحت.", "layers")}</div>`)
  + `<div class="card"><div class="card-h"><h2>خطة جديدة</h2></div><div class="form">
      <div><label>الأصل</label><input id="dp-asset" class="ltr" placeholder="BTC"></div>
      <div><label>المبلغ الشهري (USDT)</label><input id="dp-amt" class="ltr" inputmode="decimal"></div>
      <div><label>يوم الشراء (1–28)</label><input id="dp-day" class="ltr" inputmode="numeric" value="1"></div>
      <div><label>من شهر</label><input id="dp-start" type="month" value="${esc(st.today.slice(0, 7))}"></div>
      <div><label>ملاحظة</label><input id="dp-note"></div></div>
      <div class="actions"><button class="primary" id="dp-go">إنشاء الخطة</button></div><div id="dp-msg"></div></div>`;
  $("#dp-go").onclick = async () => {
    try { await api("/api/dca/create", { asset: $("#dp-asset").value, amount: $("#dp-amt").value, day: $("#dp-day").value, start: $("#dp-start").value, note: $("#dp-note").value }); PAGES.dca(); }
    catch (e) { $("#dp-msg").innerHTML = errBox(e); }
  };
  main.onclick = async (e) => {
    const b = e.target.closest("[data-plan-active]");
    if (b) { await api("/api/dca/active", { id: b.dataset.planActive, active: b.dataset.to === "1" }); return PAGES.dca(); }
    const l = e.target.closest("[data-link]");
    if (l) { const sel = $("#lk-" + l.dataset.link); if (!sel.value) return;
      try { await api("/api/dca/link", { tx_id: sel.value, plan: l.dataset.link }); PAGES.dca(); } catch (err) { l.insertAdjacentHTML("afterend", errBox(err)); } }
    const u = e.target.closest("[data-unlink]");
    if (u && await uiConfirm("فك ربط هذه العملية من الخطة؟ العملية تبقى في محفظتك.", "فك الربط")) { await api("/api/dca/link", { tx_id: u.dataset.unlink, plan: null }); PAGES.dca(); }
  };
};

function planCard(p, txs) {
  const free = txs.filter((t) => t.type === "buy" && t.asset === p.asset && !t.dca_plan);
  const maxPaid = Math.max(+p.amount, ...p.months.map((m) => +m.paid || 0)) || 1;
  return `<div class="card"><div class="card-h"><h2><span class="ltr">${esc(p.asset)}</span> · ${esc(p.amount)} USDT شهريًا يوم ${p.day} <small>من ${esc(p.start)}${p.note ? " · " + esc(p.note) : ""}</small></h2>
    <div class="actions">${p.active ? `<span class="pill good">نشطة</span><button data-plan-active="${esc(p.id)}" data-to="0">إيقاف الخطة</button>` : `<span class="pill">موقوفة</span><button data-plan-active="${esc(p.id)}" data-to="1">استئناف</button>`}</div></div>
    <div class="kpis">
      ${kpi({ k: "الشراء القادم", v: p.next_purchase || "—", ic: "info", small: true })}
      ${kpi({ k: "المدفوع", v: money(p.paid), ic: "wallet", s: `${p.n_executed} شهر منفّذ · ${p.n_missed} فائت` })}
      ${kpi({ k: "الكمية المتراكمة", v: fmtQ(p.qty) + " " + esc(p.asset), ic: "layers", s: p.avg_cost ? "متوسط التكلفة " + fmtQ(p.avg_cost) : "" })}
      ${kpi({ k: "القيمة الحالية", v: p.value === null ? "غير متاح" : money(p.value), ic: "trend", s: p.value === null ? esc(p.price_missing || "") : priceNote(p) })}
      ${kpi({ k: "القيمة − المدفوع", v: p.value_minus_paid === null ? "غير متاح" : sMoney(p.value_minus_paid), cls: sign(p.value_minus_paid), ic: "check", s: "مساهمات وقيمتها، ليست دخلًا ولا ربح محرك" })}
    </div>
    <div class="tw"><table><thead><tr><th>الشهر</th><th>الموعد المخطط</th><th>المخطط</th><th>الحالة</th><th>المنفّذ فعلًا</th><th>العمليات</th></tr></thead><tbody>
    ${p.months.slice().reverse().map((m) => `<tr><td class="ltr">${esc(m.month)}</td><td class="ltr">${esc(m.planned_date)}</td><td class="ltr">${esc(m.planned_amount)} USDT</td>
      <td><span class="pill ${DCA_STATE[m.state][0]}">${DCA_STATE[m.state][1]}</span></td>
      <td class="ltr"><div style="display:flex;gap:6px;align-items:center"><div style="width:90px;height:6px;border-radius:4px;background:var(--panel2)"><div style="height:6px;border-radius:4px;width:${Math.min(100, (+m.paid || 0) / maxPaid * 100)}%;background:var(--good)"></div></div>${+m.paid ? money(m.paid) : "—"}</div></td>
      <td>${m.tx_ids.map((id) => `<button data-unlink="${esc(id)}" title="فك الربط">${esc(id.slice(0, 6))} ✕</button>`).join(" ") || "—"}</td></tr>`).join("")}</tbody></table></div>
    ${free.length ? `<div class="actions" style="margin-top:8px"><span class="muted">ربط شراء موجود بهذه الخطة:</span><select id="lk-${esc(p.id)}"><option value="">اختر عملية</option>${free.map((t) => `<option value="${esc(t.id)}">${esc(t.at.slice(0, 10))} · ${fmtQ(t.qty)} @ ${fmtQ(t.price)}</option>`).join("")}</select><button data-link="${esc(p.id)}">ربط</button></div>` : ""}</div>`;
}
