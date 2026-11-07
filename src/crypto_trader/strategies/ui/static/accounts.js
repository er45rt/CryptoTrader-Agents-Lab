"use strict";
/* الحسابات المرتبطة (owner decision D44). Binance READ-ONLY: the key pair is typed here by the owner, sent once to the
   local server (which checks it is read-only before storing it with Windows DPAPI) and cleared from the page at once.
   Nothing here can place an order, withdraw or transfer. Synced trades wait in a review list; only selected trades
   marked "ready" are added to My Portfolio. */

const ACC = { sel: new Set(), preview: null };
const ACC_HIST = { complete: ["مطابق", "good"], incomplete: ["تاريخ الشراء ناقص", "warn"], exceeds: ["السجل أكبر من الرصيد", "warn"],
  missing: ["ما فيه سجل شراء", "warn"], quote: ["عملة التسعير", ""] };
const ACC_STATUS = { ready: ["جاهزة", "good"], imported: ["مستوردة", ""], possible_duplicate: ["مكررة محتملة", "warn"], blocked: ["محجوبة", "bad"] };
const ACC_REASON = { fee_other_asset: "الرسوم مدفوعة بعملة ثالثة (مثل BNB)؛ ما تُستورد تلقائيًا", same_as_ledger_row: "مطابقة لعملية مسجّلة يدويًا" };
const accAt = (t) => (t ? String(t).slice(0, 16).replace("T", " ") : "—");

PAGES.accounts = async () => {
  const st = await api("/api/accounts");
  main.innerHTML = header("الحسابات المرتبطة", "ربط Binance للقراءة فقط: يعرض ممتلكاتك الحقيقية ولا يقدر يتصرف فيها.",
    chip("قراءة فقط", "good") + chip("لا شراء ولا بيع ولا سحب", ""))
  + `<div class="card" id="acc-pin"></div><div class="card" id="acc-bn"></div>
  <div class="card" id="acc-bal"></div><div class="card" id="acc-trades"></div>`;
  accPin(st); accLink(st);
  if (st.connected) { accBalances(); accPreview(); }
};

function accPin(st) {
  const box = $("#acc-pin");
  box.innerHTML = `<div class="card-h"><h2>رمز الدخول <small>${st.pin_set ? "مفعّل: يُطلب عند فتح البرنامج، ويُقفل تلقائيًا بعد 15 دقيقة بدون استخدام" : "مطلوب قبل ربط أي حساب"}</small></h2></div>
    <div class="form"><div><label>${st.pin_set ? "رمز جديد" : "رمز الدخول"} (6–12 رقم)</label><input id="pin1" type="password" inputmode="numeric" maxlength="12" class="ltr" autocomplete="new-password"></div>
      <div><label>أعد كتابة الرمز</label><input id="pin2" type="password" inputmode="numeric" maxlength="12" class="ltr" autocomplete="new-password"></div></div>
    <div class="actions"><button class="primary" id="pin-save">${st.pin_set ? "تغيير الرمز" : "إنشاء الرمز"}</button></div><div id="pin-msg"></div>`;
  $("#pin-save").onclick = async () => {
    const a = $("#pin1").value, b = $("#pin2").value; $("#pin1").value = ""; $("#pin2").value = "";
    if (a !== b) { $("#pin-msg").innerHTML = `<div class="err">${esc("الرمزان غير متطابقين.")}</div>`; return; }
    try { await api("/api/auth/setup", { pin: a }); authState = { enabled: true, logged_in: true }; paintLock(); PAGES.accounts(); }
    catch (e) { $("#pin-msg").innerHTML = errBox(e); }
  };
}

function accLink(st) {
  const box = $("#acc-bn");
  const ip = st.ip_restricted === true ? `<span class="pill good">مقيّد بعنوان IP</span>`
    : st.ip_restricted === false ? `<span class="pill warn">غير مقيّد بعنوان IP — يُفضّل تقييده من Binance إذا عنوانك ثابت</span>` : "";
  box.innerHTML = `<div class="card-h"><h2>Binance <small>${st.connected ? "مربوط · قراءة فقط" : "غير مربوط"}</small></h2>
      ${st.connected ? `<button class="sm danger" id="bn-off">فصل الحساب</button>` : ""}</div>
    ${st.connected ? `<div class="kv"><div>المفتاح</div><div>${esc(st.key || "—")}</div><div>الصلاحية</div><div style="direction:rtl">قراءة فقط (تم التحقق من Binance)</div>
      <div>تاريخ الربط (غرينتش)</div><div>${esc(accAt(st.connected_at))}</div><div>آخر مزامنة (غرينتش)</div><div>${esc(accAt(st.last_sync))}</div></div>
      <div style="margin-top:8px">${ip}</div>` : `
      ${note("أنشئ مفتاح API من Binance (إدارة API) وفعّل «Enable Reading» فقط. لا تفعّل التداول ولا السحب ولا التحويل، والبرنامج يرفض أي مفتاح فيه صلاحيات غيرها. لا تُدخل كلمة مرور Binance ولا عبارة الاسترداد هنا أبدًا.")}
      ${st.pin_set ? `<div class="form"><div><label>API Key</label><input id="bn-key" type="password" class="ltr" autocomplete="off" spellcheck="false"></div>
        <div><label>Secret Key</label><input id="bn-secret" type="password" class="ltr" autocomplete="off" spellcheck="false"></div></div>
        <div class="actions"><button class="primary" id="bn-on">تحقق واربط</button>
        <span class="muted" style="font-size:12.5px">يُحفظ المفتاح مشفّرًا بحماية ويندوز على هذا الجهاز فقط، بعد التحقق إنه للقراءة فقط.</span></div>`
        : `<div class="muted">أنشئ رمز الدخول أولًا من فوق.</div>`}`}
    ${st.last_error ? `<div class="err" style="margin-top:8px">آخر خطأ <span class="ltr">${esc(accAt(st.last_error_at))}</span>: ${esc(trErr(st.last_error).ar || st.last_error)}</div>` : ""}
    <div id="bn-msg"></div>`;
  const on = $("#bn-on");
  if (on) on.onclick = async () => {
    const api_key = $("#bn-key").value, api_secret = $("#bn-secret").value;
    $("#bn-key").value = ""; $("#bn-secret").value = "";          // never kept in the page
    on.disabled = true; $("#bn-msg").innerHTML = loading("التحقق من صلاحيات المفتاح لدى Binance…");
    try { await api("/api/accounts/binance/connect", { api_key, api_secret }); PAGES.accounts(); }
    catch (e) { $("#bn-msg").innerHTML = errBox(e); on.disabled = false; }
  };
  const off = $("#bn-off");
  if (off) off.onclick = async () => {
    if (!(await uiConfirm("تفصل Binance وتحذف المفتاح المحفوظ وسجل المزامنة من هذا الجهاز؟ عمليات محفظتك المستوردة تبقى كما هي.", "فصل"))) return;
    try { await api("/api/accounts/binance/disconnect", {}); PAGES.accounts(); } catch (e) { $("#bn-msg").innerHTML = errBox(e); }
  };
}

async function accBalances() {
  const box = $("#acc-bal"); if (!box) return;
  box.innerHTML = `<div class="card-h"><h2>الأرصدة الحقيقية في Binance</h2><button class="sm" id="bal-go">تحديث</button></div>${loading("قراءة الأرصدة…")}`;
  let d; try { d = await api("/api/accounts/binance/balances", undefined, 30000); }
  catch (e) { box.innerHTML = `<div class="card-h"><h2>الأرصدة الحقيقية في Binance</h2><button class="sm" id="bal-go">تحديث</button></div>${errBox(e)}`; $("#bal-go").onclick = accBalances; return; }
  if (current !== "accounts" || !$("#acc-bal")) return;
  box.innerHTML = `<div class="card-h"><h2>الأرصدة الحقيقية في Binance <small>قراءة <span class="ltr">${esc(accAt(d.at))}</span> غرينتش</small></h2><button class="sm" id="bal-go">تحديث</button></div>
    <div class="big ltr" style="text-align:right">${fmtUsd(d.value)}</div>
    <div class="muted" style="font-size:12.5px">القيمة من الأصول اللي لها سعر في Binance فقط${d.value_complete ? "" : " — بعض الأصول بدون سعر"}.</div>
    ${d.balances.length ? `<div class="tw" style="margin-top:10px"><table><thead><tr><th>الأصل</th><th>الكمية</th><th>متاح</th><th>محجوز</th><th>السعر</th><th>القيمة</th><th>سجل الشراء</th><th>التكلفة</th><th>غير محقق</th></tr></thead><tbody>
    ${d.balances.map((r) => { const h = ACC_HIST[r.history] || [r.history || "—", ""];
      return `<tr><td><b>${esc(r.asset)}</b></td><td class="ltr">${fmtNum(r.total, 8)}</td><td class="ltr">${fmtNum(r.free, 8)}</td><td class="ltr">${fmtNum(r.locked, 8)}</td>
      <td class="ltr">${isNum(r.price) ? fmtNum(r.price, 6) : `<span class="muted">${esc("غير متاح")}</span>`}</td><td class="ltr">${isNum(r.value) ? fmtUsd(r.value) : "—"}</td>
      <td><span class="pill ${h[1]}">${esc(h[0])}</span></td>
      <td class="ltr">${isNum(r.cost) ? fmtUsd(r.cost) : r.history === "quote" ? "—" : `<span class="muted">${esc("غير متاحة")}</span>`}</td>
      <td class="ltr ${sign(r.unrealized)}">${isNum(r.unrealized) ? fmtUsd(r.unrealized) : r.history === "quote" ? "—" : `<span class="muted">${esc("غير متاح")}</span>`}</td></tr>`; }).join("")}
    </tbody></table></div>
    <div class="muted" style="font-size:12px;margin-top:6px">التكلفة والربح تظهر فقط إذا كان سجل الشراء المسجّل في محفظتك (محفظة Binance) يطابق الرصيد تمامًا. غير كذا تبقى «غير متاحة» بدل رقم مضلل.</div>`
    : empty("ما فيه أرصدة في الحساب.", "wallet")}`;
  $("#bal-go").onclick = accBalances;
}

async function accPreview(sync = false) {
  const box = $("#acc-trades"); if (!box) return;
  const head = `<div class="card-h"><h2>سجل الصفقات <small>مراجعة قبل الدمج في محفظتك</small></h2><button class="sm" id="tr-sync">مزامنة من Binance</button></div>`;
  box.innerHTML = head + loading(sync ? "جلب سجل الصفقات من Binance…" : "تحميل المعاينة…");
  let p;
  try { if (sync) await api("/api/accounts/binance/sync", {}, 180000); p = await api("/api/accounts/binance/preview"); }
  catch (e) { box.innerHTML = head + errBox(e); $("#tr-sync").onclick = () => accPreview(true); return; }
  if (current !== "accounts" || !$("#acc-trades")) return;
  ACC.preview = p; ACC.sel = new Set([...ACC.sel].filter((x) => p.rows.some((r) => r.trade.ext_id === x && r.status === "ready")));
  const c = p.counts;
  box.innerHTML = head + `<div class="muted" style="font-size:12.5px">آخر مزامنة <span class="ltr">${esc(accAt(p.synced_at))}</span> غرينتش ·
      جاهزة ${c.ready} · مستوردة ${c.imported} · مكررة محتملة ${c.possible_duplicate} · محجوبة ${c.blocked}</div>
    ${p.replay_error ? `<div class="err" style="margin-top:8px">${esc(trErr(p.replay_error).ar || p.replay_error)}</div>` : ""}
    ${p.rows.length ? `<div class="tw" style="margin-top:10px;max-height:420px"><table><thead><tr><th></th><th>الوقت (غرينتش)</th><th>العملية</th><th>الأصل</th><th>الكمية</th><th>السعر</th><th>الرسوم</th><th>الحالة</th></tr></thead><tbody>
    ${p.rows.slice().reverse().map((r) => { const t = r.trade, s = ACC_STATUS[r.status] || [r.status, ""];
      return `<tr><td>${r.status === "ready" ? `<input type="checkbox" data-ext="${esc(t.ext_id)}" ${ACC.sel.has(t.ext_id) ? "checked" : ""}>` : ""}</td>
      <td class="ltr">${esc(accAt(t.at))}</td><td>${t.side === "buy" ? '<span class="pill good">شراء</span>' : '<span class="pill bad">بيع</span>'}</td>
      <td><b>${esc(t.asset)}</b></td><td class="ltr">${fmtNum(t.qty, 8)}</td><td class="ltr">${fmtNum(t.price, 6)}</td>
      <td class="ltr">${fmtNum(t.fee, 8)} <span class="muted">${esc(t.fee_asset)}</span></td>
      <td><span class="pill ${s[1]}">${esc(s[0])}</span>${r.reason ? ` <span class="muted" style="font-size:11.5px">${esc(ACC_REASON[r.reason] || r.reason)}</span>` : ""}</td></tr>`; }).join("")}
    </tbody></table></div>
    <div class="actions"><button class="sm" id="tr-all">تحديد كل الجاهزة</button><button class="primary" id="tr-import">استيراد المحدد إلى محفظتي (<span id="tr-n">${ACC.sel.size}</span>)</button></div>`
    : empty("ما فيه صفقات للمراجعة. اضغط «مزامنة من Binance».", "layers")}<div id="tr-msg"></div>`;
  $("#tr-sync").onclick = () => accPreview(true);
  box.querySelectorAll("[data-ext]").forEach((cb) => cb.onchange = () => { cb.checked ? ACC.sel.add(cb.dataset.ext) : ACC.sel.delete(cb.dataset.ext); $("#tr-n").textContent = ACC.sel.size; });
  const all = $("#tr-all"); if (all) all.onclick = () => { p.rows.filter((r) => r.status === "ready").forEach((r) => ACC.sel.add(r.trade.ext_id)); accPreview(); };
  const imp = $("#tr-import");
  if (imp) imp.onclick = async () => {
    if (!ACC.sel.size) return;
    if (!(await uiConfirm(`تضيف ${ACC.sel.size} صفقة إلى محفظتي؟ كل صفقة تُضاف مرة وحدة فقط، وتقدر تحذفها لاحقًا من صفحة محفظتي.`, "استيراد"))) return;
    imp.disabled = true;
    try { const r = await api("/api/accounts/binance/import", { ext_ids: [...ACC.sel] }); ACC.sel.clear();
      await accPreview(); accBalances();     // the message goes in after the list is redrawn, so it stays visible
      const m = $("#tr-msg"); if (m) m.innerHTML = `<div class="okmsg">${esc("تمت إضافة")} ${r.imported} ${esc("صفقة إلى محفظتك.")}</div>`; }
    catch (e) { $("#tr-msg").innerHTML = errBox(e); imp.disabled = false; }
  };
}
