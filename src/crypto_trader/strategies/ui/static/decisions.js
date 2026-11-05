"use strict";
/* مركز القرارات (D31). Shows, for every recorded decision, what happened at each step and who decided it.
   Every line comes from the engine's records (journal-derived decision chain + decision trace written while
   deciding). Summaries are fixed templates filled with recorded fields — never generated text. */

const DC = {
  kinds: {
    entry: ["دخول", "good"], exit: ["خروج", "warn"], rejected_risk: ["رفض المخاطر", "bad"], rejected_validity: ["رفض الصلاحية", "bad"],
    blocked_safety: ["حظر الحماية", "bad"], missed: ["فائتة", "warn"], no_entry: ["امتناع عن الدخول", ""], keep: ["إبقاء المركز", ""], other: ["أخرى", ""],
  },
  exitKinds: { stop_loss: "وقف خسارة", trailing_stop: "وقف خسارة متحرك", take_profit: "العائد المستهدف", exit_signal: "إشارة خروج", strategy_exit: "إشارة خروج من الاستراتيجية" },
};
const kindPill = (k) => { const [t, c] = DC.kinds[k] || [k, ""]; return `<span class="pill ${c}">${esc(t)}</span>`; };
const pctOf = (t) => (isNum(t) ? Math.round(+t * 100) + "%" : "—");
const rationaleAr = rationaleText;   // i18n.js
function oneLine(i) {   // fixed template over recorded fields; no free text is generated
  const k = i.kind, sig = i.signal;
  if (k === "entry") return `شراء · هدف ${pctOf(sig?.target)} · ${i.fills.length ? "تنفّذ عند " + fmtNum(i.fills[0].price) : "أُرسل وما تنفّذ بعد"}`;
  if (k === "exit") return `${DC.exitKinds[i.exit_kind] || "خروج"} · ${i.fills.length ? "تنفّذ عند " + fmtNum(i.fills[0].price) : "أُرسل وما تنفّذ بعد"}`;
  if (k === "rejected_risk") return "محرك المخاطر رفض: " + (i.risk?.codes || []).map((c) => arOf(AR.risk, c)).join("، ");
  if (k === "rejected_validity") return "فحص الصلاحية رفض: " + arOf(AR.validityReason, i.validity?.reason);
  if (k === "blocked_safety") return "الحماية أوقفت الأمر";
  if (k === "missed") return "فائتة: الشمعة وصلت متأخرة";
  if (k === "no_entry") return i.rationale === "strategy:no_cash" ? "إشارة شراء بدون رصيد كافٍ" : sig && +sig.target === 0 ? "الاستراتيجية ما تبي تدخل (هدف 0%)" : sig ? "ما تغيّر شي يستاهل أمر" : "الاستراتيجية ما أعطت إشارة";
  if (k === "keep") return `باقي في المركز · هدف ${pctOf(sig?.target)}`;
  return rationaleAr(i.rationale);
}

const dcState = { filters: { source: "", strategy: "", symbol: "", since: "", until: "", kinds: [] }, selected: null, auto: true, data: null };

PAGES.decisions = async () => {
  main.innerHTML = header("مركز القرارات", "ليش دخلت الاستراتيجية أو ما دخلت أو طلعت، ومن قرّر كل خطوة. كل شي هنا من سجلات المحرك وقت القرار.",
    chip("الذكاء الاصطناعي: غير مشارك في هذا المحرك", "") + `<span class="chip" style="cursor:pointer" id="dc-auto"><i></i>تحديث تلقائي</span>`)
  + `<div class="kpis" id="dc-kpis"><div class="skel"></div></div>
  <div class="card"><div class="form" id="dc-form">
    <div><label>المصدر</label><select id="dc-source"><option value="">الكل</option></select></div>
    <div><label>الاستراتيجية</label><select id="dc-strategy"><option value="">الكل</option></select></div>
    <div><label>الزوج</label><select id="dc-symbol"><option value="">الكل</option></select></div>
    <div><label>من</label><input id="dc-since" type="datetime-local"></div>
    <div><label>إلى</label><input id="dc-until" type="datetime-local"></div>
  </div><div class="actions"><div class="tabs" id="dc-kinds"></div></div></div>
  <div class="cols dc-cols"><div class="card"><div class="card-h"><h2>القرارات <small id="dc-total"></small></h2></div><div class="tw" style="max-height:640px"><table>
    <thead><tr><th>الوقت (غرينتش)</th><th>النوع</th><th>الاستراتيجية · الزوج</th><th>الخلاصة</th></tr></thead><tbody id="dc-rows"></tbody></table></div></div>
  <div class="stack" id="dc-detail">${empty("اختر قرار من القائمة عشان تشوف سلسلته كاملة", "layers")}</div></div>`;
  const f = dcState.filters;
  ["source", "strategy", "symbol", "since", "until"].forEach((k) => { const el = $("#dc-" + k); el.value = f[k];
    el.onchange = () => { f[k] = el.value; dcLoad(); }; });
  const autoChip = $("#dc-auto"); const paintAuto = () => autoChip.classList.toggle("good", dcState.auto); paintAuto();
  autoChip.onclick = () => { dcState.auto = !dcState.auto; paintAuto(); };
  await dcLoad();
  timer = setInterval(() => { if (current === "decisions" && dcState.auto) dcLoad(true).catch(() => {}); }, 30000);
};

async function dcLoad(quiet = false) {
  const f = dcState.filters, q = new URLSearchParams();
  ["source", "strategy", "symbol"].forEach((k) => { if (f[k]) q.set(k, f[k]); });
  if (f.since) q.set("since", new Date(f.since + "Z").toISOString());
  if (f.until) q.set("until", new Date(f.until + "Z").toISOString());
  if (f.kinds.length) q.set("kinds", f.kinds.join(","));
  q.set("limit", "400");
  let d;
  try { d = await api("/api/decisions?" + q.toString()); } catch (e) { if (!quiet) $("#dc-rows").innerHTML = `<tr><td colspan="4">${errBox(e)}</td></tr>`; return; }
  dcState.data = d;
  if (current !== "decisions" || !$("#dc-kpis")) return;   // the user moved to another page meanwhile
  const fill = (id, opts, cur) => { const el = $(id); if (!el) return; el.innerHTML = `<option value="">الكل</option>` + opts.map(([v, t]) => `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(t)}</option>`).join(""); };
  fill("#dc-source", d.facets.sources.map((s) => [s.id, s.label]), f.source);
  fill("#dc-strategy", d.facets.strategies.map((s) => [s, s]), f.strategy);
  fill("#dc-symbol", d.facets.symbols.map((s) => [s, s]), f.symbol);
  const c = d.counts;
  $("#dc-kpis").innerHTML = [
    kpi({ k: "دخول", v: String(c.entry), ic: "trend", tone: "g" }), kpi({ k: "خروج", v: String(c.exit), ic: "down", tone: "w" }),
    kpi({ k: "رفض (مخاطر · صلاحية · حماية)", v: String(c.rejected_risk + c.rejected_validity + c.blocked_safety), ic: "shield", tone: (c.rejected_risk + c.rejected_validity + c.blocked_safety) ? "r" : "" }),
    kpi({ k: "فائتة", v: String(c.missed), ic: "clock", tone: c.missed ? "w" : "", s: "شموع وصلت متأخرة وما تداولت" }),
    kpi({ k: "امتناع عن الدخول", v: String(c.no_entry), ic: "box" }), kpi({ k: "إبقاء المركز", v: String(c.keep), ic: "layers" }),
  ].join("");
  const kt = $("#dc-kinds");
  kt.innerHTML = `<button data-k="" class="${f.kinds.length ? "" : "on"}">الكل</button>` + KIND_ORDER.filter((k) => c[k]).map((k) =>
    `<button data-k="${k}" class="${f.kinds.includes(k) ? "on" : ""}">${esc(DC.kinds[k][0])} <span class="muted">${c[k]}</span></button>`).join("");
  kt.onclick = (e) => { const b = e.target.closest("button"); if (!b) return; const k = b.dataset.k;
    f.kinds = !k ? [] : f.kinds.includes(k) ? f.kinds.filter((x) => x !== k) : [...f.kinds, k]; dcLoad(); };
  $("#dc-total").textContent = d.total > d.items.length ? `أحدث ${d.items.length} من ${d.total}` : `${d.total}`;
  $("#dc-rows").innerHTML = d.items.map((i) => `<tr data-dc="${esc(i.id)}" class="${i.id === dcState.selected ? "sel" : ""}" style="cursor:pointer">
    <td class="ltr">${esc(i.at.slice(0, 16).replace("T", " "))}</td><td>${kindPill(i.kind)}</td>
    <td><b>${esc(i.strategy)}</b> <span class="muted">· ${esc(i.symbol)} · ${esc(tfAr(i.timeframe))} · ${esc(({ paper: "ورقي", demo: "تجريبي", backtest: "اختبار" })[i.source.kind] || i.source.kind)}</span></td>
    <td class="wrap">${esc(oneLine(i))}</td></tr>`).join("") || `<tr><td colspan="4">${empty("ما فيه قرارات بهالفلاتر. القرارات تظهر هنا بعد أول شمعة حيّة في جلسة ورقية، أو بعد اختبار «كامل عبر المحرك».", "layers")}</td></tr>`;
  $("#dc-rows").onclick = (e) => { const tr = e.target.closest("tr[data-dc]"); if (!tr) return; dcShow(tr.dataset.dc); };
  if (dcState.selected && !quiet) dcShow(dcState.selected, true);
}
const KIND_ORDER = ["entry", "exit", "rejected_risk", "rejected_validity", "blocked_safety", "missed", "no_entry", "keep", "other"];

function step(state, who, title, body) {
  // state: pass | stop | skip | info
  const icon = { pass: "✓", stop: "✕", skip: "–", info: "•" }[state];
  return `<div class="dc-step ${state}"><div class="dc-dot">${icon}</div><div class="dc-body"><div class="dc-who">${who}</div><div class="dc-title">${title}</div>${body ? `<div class="dc-text">${body}</div>` : ""}</div></div>`;
}

async function dcShow(id, keepScroll = false) {
  dcState.selected = id;
  document.querySelectorAll("#dc-rows tr").forEach((tr) => tr.classList.toggle("sel", tr.dataset.dc === id));
  const box = $("#dc-detail"); if (!box) return;
  if (!keepScroll) box.innerHTML = `<div class="card">${loading("تحميل سلسلة القرار…")}</div>`;
  let d; try { d = await api("/api/decision?id=" + encodeURIComponent(id)); } catch (e) { box.innerHTML = `<div class="card">${errBox(e)}</div>`; return; }
  const i = d.item, sig = i.signal, read = i.read || {}, sz = i.sizing || {};
  const intent = i.action === "buy" || i.action === "sell";
  const stopAt = i.kind === "rejected_validity" ? "validity" : i.kind === "rejected_risk" ? "risk" : i.kind === "blocked_safety" ? "safety" : null;
  const readRows = Object.entries(read);
  const steps = [
    step(sig ? "info" : i.kind === "missed" ? "info" : "skip", `الاستراتيجية «${esc(i.strategy)}»`,
      sig ? `قالت: ${+sig.target > 0 ? "أبي مركز " + pctOf(sig.target) + " من الحد" : "ما أبي مركز (0%)"}` : i.evidence?.trace ? "ما أعطت إشارة على هالشمعة" : "ما فيه سجل إشارة لهالشمعة",
      (sig ? `السبب كما كتبه مؤلف الاستراتيجية: <code class="ltr">${esc(sig.reason || "—")}</code>${reasonAr(sig.reason) !== sig.reason ? ` (${esc(reasonAr(sig.reason))})` : ""}` : "") +
      (readRows.length ? `<div class="kv" style="margin-top:8px">${readRows.map(([k, v]) => `<div class="ltr" style="text-align:right">${esc(k)}</div><div>${v === null ? "غير متوفر (بيانات ناقصة)" : fmtNum(v, 4)}</div>`).join("")}</div>
        <div class="muted" style="font-size:11.5px;margin-top:4px">قيم المؤشرات اللي قرأتها الاستراتيجية بنفسها على هالشمعة، مسجّلة وقت القرار.</div>`
        : i.evidence?.trace ? `<div class="muted" style="font-size:12px;margin-top:6px">ما قرأت مؤشرات مدمجة (مثل استراتيجيات Freqtrade اللي تحسب مؤشراتها داخليًا).</div>` : "")),
    step("skip", "الذكاء الاصطناعي", "لم يشارك",
      `مصدر القرار: <code class="ltr">${esc(i.decision_source)}</code> — استراتيجية برمجية بقواعد ثابتة. ما فيه وكيل ذكاء اصطناعي في هذا المسار.`),
    step(i.kind === "missed" ? "stop" : intent ? "pass" : "info", "محوّل القرار في المحرك",
      intent ? (i.action === "buy" ? "حوّل الإشارة إلى أمر شراء" : "حوّل الإشارة إلى أمر بيع") : i.kind === "missed" ? "سجّل القرار «فائت» وما أرسل أمر" : "ما أرسل أمر",
      `${esc(rationaleAr(i.rationale))} <span class="muted ltr" style="font-size:11.5px">(${esc(i.rationale)})</span>` +
      (sz.held !== undefined ? `<div class="kv" style="margin-top:8px"><div>المركز وقتها</div><div>${fmtNum(sz.held, 6)}</div><div>رأس المال وقتها</div><div>${fmtUsd(sz.equity_quote)}</div>
        ${sz.want_qty !== undefined ? `<div>الكمية المطلوبة حسب الهدف</div><div>${fmtNum(sz.want_qty, 6)}</div><div>الفرق</div><div>${fmtNum(sz.delta, 6)}</div>` : ""}</div>` : "")),
    intent ? step(i.validity?.disposition === "accepted" ? "pass" : "stop", "فحص الصلاحية",
      `${esc(arOf(AR.validity, i.validity?.disposition))} · ${esc(arOf(AR.validityReason, i.validity?.reason))}`, `<code class="ltr">${esc(i.validity?.disposition)} / ${esc(i.validity?.reason)}</code>`)
      : step("skip", "فحص الصلاحية", "ما انطلب", "ما فيه أمر، فما فيه شي يتفحّص"),
    intent && i.risk ? step(i.risk.decision === "allowed" ? "pass" : "stop", "محرك المخاطر", esc(arOf(AR.riskDecision, i.risk.decision)),
      i.risk.codes.map((c) => `${esc(arOf(AR.risk, c))} <code class="ltr">${esc(c)}</code>`).join("<br>"))
      : step("skip", "محرك المخاطر", "ما انطلب", intent ? "توقّف القرار قبل محرك المخاطر" : "ما فيه أمر"),
    intent && i.risk?.decision === "allowed" ? step(i.kind === "blocked_safety" ? "stop" : "pass", "الحماية", i.kind === "blocked_safety" ? "أوقفت الأمر" : "سمحت")
      : step("skip", "الحماية", "ما انطلبت"),
    step(i.fills.length ? "pass" : intent && !stopAt ? "info" : "skip", "التنفيذ (محاكاة)", esc(AR.submission[i.submission] || i.submission || "—"),
      i.fills.length ? i.fills.map((x) => `كمية ${fmtNum(x.quantity, 6)} عند ${fmtNum(x.price)} <span class="muted">(متبقي ${fmtNum(x.remaining_quantity, 6)})</span>`).join("<br>") : ""),
  ];
  box.innerHTML = `<div class="card"><div class="card-h"><h2>${kindPill(i.kind)} <span class="ltr">${esc(i.at.slice(0, 16).replace("T", " "))} غرينتش</span></h2><small class="muted">${esc(i.source.label)}</small></div>
    <div class="dc-sum">${esc(oneLine(i))}${i.kind === "exit" ? ` <span class="pill warn">${esc(DC.exitKinds[i.exit_kind] || "")}</span>` : ""}</div>
    <div class="chart" style="margin-top:10px"><canvas id="dc-chart"></canvas></div>
    <div class="dc-steps">${steps.join("")}</div>
    <details class="dc-ev"><summary>الدليل من السجلات</summary>
      <div class="muted" style="font-size:12px;margin:8px 0">سجل القرار من دفتر المحرك (RunReport)، وسجل الإشارة المكتوب وقت القرار${i.trace_recorded_at ? ` في <span class="ltr">${esc(i.trace_recorded_at.slice(0, 19).replace("T", " "))}</span>` : ""}.</div>
      <pre>${esc(JSON.stringify(i.evidence.journal_decision, null, 1))}</pre>
      ${i.evidence.trace ? `<pre style="margin-top:8px">${esc(JSON.stringify(i.evidence.trace, null, 1))}</pre>` : `<div class="err">ما فيه سجل إشارة لهالقرار (قرار قبل تفعيل تسجيل الإشارات).</div>`}
    </details></div>`;
  const bars = d.bars || [];
  if (bars.length) {
    const fills = i.fills.map((x) => ({ entry_time: i.action === "buy" ? i.at : null, entry_price: x.price, exit_time: i.action === "sell" ? i.at : null, exit_price: x.price }));
    candles($("#dc-chart"), bars, fills, [{ values: sma(bars, 20), color: css("--sma1") }], 230);
    dcMark($("#dc-chart"), bars, i.bar_open || i.at);
  } else $("#dc-chart").replaceWith(Object.assign(document.createElement("div"), { className: "muted", textContent: "ما فيه شموع محفوظة لهالمصدر." }));
}
function dcMark(c, bars, t) {   // highlight the decided bar (same geometry as candles())
  const g = c.getContext("2d"), w = c.clientWidth, h = c.clientHeight, L = 8, R = w - 72, bw = (R - L) / bars.length;
  const ts = msOf(t); let k = 0; bars.forEach((b, n) => { if (msOf(b.t) <= ts) k = n; });
  g.save(); g.fillStyle = "rgba(59,130,246,0.16)"; g.fillRect(L + k * bw, 12, Math.max(3, bw), h - 38);
  g.fillStyle = css("--accent"); g.textAlign = "center"; g.fillText("القرار", L + k * bw + bw / 2, 8); g.restore();
}

