"use strict";
/* Arabic display layer for the engine UI. Engine values (timeframes, codes, enums, log lines, error messages)
   are translated HERE, at display time only: stored values and engine messages never change. Unknown text is
   shown as-is under «تفاصيل تقنية», never guessed. Glossary agreed in the D30 Arabic audit. */

const AR = {
  tf: { "1m": "دقيقة", "5m": "5 دقائق", "15m": "15 دقيقة", "30m": "30 دقيقة", "1h": "ساعة", "2h": "ساعتان", "4h": "4 ساعات",
        "6h": "6 ساعات", "8h": "8 ساعات", "12h": "12 ساعة", "1d": "يومي" },
  months: ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"],
  action: { buy: "شراء", sell: "بيع", hold: "انتظار" },
  riskDecision: { allowed: "مقبول", rejected: "مرفوض" },
  risk: {
    risk_state_timestamp_unavailable: "بيانات المخاطر بدون وقت", risk_state_timestamp_in_future: "وقت بيانات المخاطر في المستقبل",
    stale_risk_state: "بيانات المخاطر قديمة", trading_halted: "التداول موقوف", duplicate_intent: "أمر مكرر", conflicting_intent: "أمر متعارض",
    reduce_only_violation: "المسموح تقليص المركز فقط", reduce_only_quantity_exceeded: "كمية البيع أكبر من المركز",
    max_open_orders_exceeded: "تجاوز حد الأوامر المفتوحة", max_open_positions_exceeded: "تجاوز حد المراكز المفتوحة",
    max_order_notional_exceeded: "قيمة الأمر أكبر من الحد", max_symbol_exposure_exceeded: "الانكشاف على الزوج أكبر من الحد",
    max_strategy_exposure_exceeded: "انكشاف الاستراتيجية أكبر من الحد", max_gross_exposure_exceeded: "الانكشاف الإجمالي أكبر من الحد",
    max_net_exposure_exceeded: "الانكشاف الصافي أكبر من الحد", loss_limit_breached: "تجاوز حد الخسارة", max_drawdown_exceeded: "تجاوز حد التراجع",
    concentration_exceeded: "التركّز أعلى من الحد", all_rules_passed: "كل قواعد المخاطر ناجحة",
  },
  validity: { accepted: "صالح", expired: "انتهت صلاحيته", stale: "متقادم", market_moved: "السعر تحرّك أكثر من المسموح", superseded: "استُبدل بقرار أحدث", backpressure_rejected: "رُفض لازدحام الطلبات" },
  validityReason: {
    within_policy: "ضمن السياسة", valid_until_exceeded: "انتهت مدة صلاحية القرار", market_event_ts_in_future: "وقت بيانات السوق في المستقبل",
    max_age_exceeded: "القرار أقدم من المسموح", market_state_unavailable: "بيانات السوق غير متاحة", market_move_exceeded: "السعر تحرّك أكثر من الحد المسموح",
    newer_intent_for_scope: "فيه قرار أحدث لنفس الزوج", bounded_capacity_exhausted: "طابور القرارات ممتلئ",
  },
  submission: { hold: "ما فيه أمر", submitted_to_simulator: "أُرسل للمحاكي", stopped_by_validity: "أوقفه فحص الصلاحية", stopped_by_risk: "أوقفه محرك المخاطر",
                blocked_by_safety: "أوقفته الحماية", would_submit: "كان سيُرسل (وضع الظل)", not_applicable: "لا ينطبق" },
  ops: { "strategy.added": "إضافة استراتيجية", "strategy.removed": "حذف استراتيجية", "backtest.done": "اكتمال اختبار", "paper.created": "إنشاء جلسة ورقية",
         "paper.started": "تشغيل جلسة ورقية", "paper.stopped": "طلب إيقاف جلسة ورقية", "risk.saved": "حفظ إعدادات المخاطر",
         "demo.created": "إنشاء تجربة في الوضع التجريبي", "demo.deleted": "حذف تجربة من الوضع التجريبي" },
  opKeys: { file: "الملف", id: "رقم التجربة", strategy: "الاستراتيجية", mode: "النوع", name: "الجلسة", spec: "مسار الاستراتيجية", pid: "رقم عملية النظام",
            values: "القيم", capital: "رأس المال", fee: "الرسوم", slippage_bps: "الانزلاق", max_position: "الحد الأقصى للمركز", risk_overrides: "حدود إضافية" },
  mode: { fast: "سريع", engine: "كامل عبر المحرك" },
  limits: {
    limits_version: "إصدار الحدود", max_order_notional_quote: "أقصى قيمة للأمر الواحد (USDT)", max_symbol_exposure_quote: "أقصى انكشاف على الزوج (USDT)",
    max_gross_exposure_quote: "أقصى انكشاف إجمالي (USDT)", max_net_exposure_quote: "أقصى انكشاف صافٍ (USDT)", max_strategy_exposure_quote: "أقصى انكشاف للاستراتيجية (USDT)",
    loss_limit_quote: "حد الخسارة (USDT)", max_drawdown_pct: "أقصى تراجع (%)", concentration_limit_pct: "حد التركّز (%)", max_open_orders: "أقصى عدد للأوامر المفتوحة",
    max_open_positions: "أقصى عدد للمراكز المفتوحة", reduce_only_mode: "وضع تقليص المراكز فقط", trading_halted: "إيقاف التداول",
  },
  evidence: { SUCCESS: "ناجحة", FAILED: "فاشلة", GAP: "فجوة" },
  reasonTokens: {
    long: "مركز شراء", flat: "بدون مركز", hold: "شراء واحتفاظ", "fast>slow": "المتوسط السريع فوق البطيء", "fast<=slow": "المتوسط السريع تحت البطيء أو مساوٍ له",
    "ft:enter_long": "Freqtrade: إشارة دخول (شراء)", "ft:flat": "Freqtrade: بدون مركز", "ft:entering": "Freqtrade: بانتظار تنفيذ الدخول",
    "ft:hold_long": "Freqtrade: الاحتفاظ بالمركز", "ft:exit:stoploss": "Freqtrade: خروج بوقف الخسارة", "ft:exit:trailing": "Freqtrade: خروج بوقف خسارة متحرك",
    "ft:exit:roi": "Freqtrade: خروج عند بلوغ العائد المستهدف", "ft:exit:signal": "Freqtrade: خروج بإشارة من الاستراتيجية",
  },
};

const tfAr = (tf) => AR.tf[tf] || tf || "—";
const arOf = (map, v) => (v in map ? map[v] : v);
const reasonAr = (r) => (r in AR.reasonTokens ? AR.reasonTokens[r] : r);   // strategy-author text stays as written
const utc = (s) => `${s} غرينتش`;

/** Decision rationale (persisted engine value) -> Arabic sentence. */
function rationaleText(r) {
  if (!r) return "—";
  if (r === "warmup") return "تهيئة المؤشرات";
  if (r === "pre-session-history") return "بيانات قبل بدء الجلسة";
  if (r === "missed:bar-arrived-late") return "فائتة: الشمعة وصلت بعد فوات وقت تنفيذها";
  if (r === "strategy:keep") return "لا إشارة جديدة، يبقى الوضع كما هو";
  if (r === "strategy:no_cash") return "الإشارة شراء، لكن الرصيد ما يكفي لأقل قيمة أمر";
  let m = /^strategy:(.*):at_target$/.exec(r);
  if (m) return `${reasonAr(m[1])} — المركز مطابق للهدف، ما يحتاج أمر`;
  m = /^strategy:(.*)$/.exec(r);
  if (m) return `إشارة الاستراتيجية: ${reasonAr(m[1])}`;
  return r;
}

/** Network / OS error text from the paper loop -> Arabic cause (or null). */
function netCause(text) {
  const t = String(text || "");
  if (/aswMonFltProxy/.test(t)) return "برنامج الحماية Avast يمنع اتصال الجلسة بـ Binance";
  if (/CERTIFICATE_VERIFY_FAILED/.test(t)) return "تعذّر التحقق من شهادة الأمان (قد يكون برنامج حماية يعترض الاتصال)";
  if (/getaddrinfo failed|11001|Name or service not known/.test(t)) return "تعذّر الوصول إلى Binance — تحقّق من الإنترنت";
  if (/10061|Connection refused/.test(t)) return "الاتصال مرفوض";
  if (/10054|ConnectionReset|reset by peer/.test(t)) return "انقطع الاتصال من الطرف الآخر";
  if (/timed out|TimeoutError/.test(t)) return "انتهت مهلة الاتصال";
  if (/HTTP Error 400/.test(t)) return "Binance رفض الطلب: رمز الزوج غير صحيح أو غير مدعوم (اكتبه بحروف كبيرة مثل BTCUSDT)";
  if (/HTTP Error (429|418)/.test(t)) return "Binance حدّ من عدد الطلبات مؤقتًا";
  if (/HTTP Error 451/.test(t)) return "Binance غير متاح من هذا الموقع";
  if (/HTTP Error (\d{3})/.test(t)) return `Binance ردّ بخطأ ${/HTTP Error (\d{3})/.exec(t)[1]}`;
  if (/URLError|ConnectionError|OSError|PermissionError/.test(t)) return "تعذّر الاتصال بـ Binance";
  return null;
}

/* Error messages: exact text or a stable regex -> Arabic. Engine messages are matched, never edited. */
const ERRS = [
  [/^host not allowed$/, () => "العنوان غير مسموح. افتح الواجهة من 127.0.0.1 أو localhost."],
  [/^missing X-CT-UI header$/, () => "طلب مرفوض: ترويسة الأمان X-CT-UI غير موجودة."],
  [/^not found$/, () => "المسار غير موجود."],
  [/^request too large\.$/, () => "حجم الطلب كبير جدًا (الحد 2 ميجابايت)."],
  [/^unknown job$|^'unknown job'$/, () => "مهمة الاختبار غير موجودة (ربما أُعيد تشغيل الخادم)؛ شغّل الاختبار من جديد."],
  [/^'(spec|data|name|id|filename|content)'$/, (m) => `حقل مطلوب ناقص: ${({ spec: "الاستراتيجية", data: "ملف البيانات", name: "اسم الجلسة", id: "المعرّف", filename: "اسم الملف", content: "محتوى الملف" })[m[1]]}`],
  [/^'(date|open|high|low|close|volume)'$/, (m) => `عمود ناقص في ملف البيانات: ${m[1]} (المطلوب: date, open, high, low, close, volume)`],
  [/^decision not found\.$|^'decision not found\.'$/, () => "القرار غير موجود."],
  [/^bad decision id\.$/, () => "معرّف القرار غير صالح."],
  [/^(?:ValueError: )?Invalid isoformat string: (.+)$/, () => "صيغة التاريخ غير صحيحة."],
  [/^strategy file not found: (.+)$/, (m) => `ملف الاستراتيجية غير موجود: ${m[1]}`],
  [/^cannot import strategy module '([^']+)': (.*)$/, (m) => `تعذّر استيراد وحدة الاستراتيجية «${m[1]}»`],
  [/^cannot import (.+)$/, (m) => `تعذّر تحميل ملف الاستراتيجية: ${m[1]}`],
  [/^strategy file (.+?) failed to import: (\w+): (.*)$/, (m) => `تعذّر تحميل ملف الاستراتيجية بسبب خطأ برمجي في الملف (${m[2]})`],
  [/^'([^']+)' in (.+) is not a Strategy subclass\.$/, (m) => `«${m[1]}» ليست استراتيجية صالحة (لازم ترث من Strategy).`],
  [/^(.+) defines (\d+) Strategy classes; use 'file\.py:ClassName'\.$/, (m) => +m[2] === 0 ? "الملف ما فيه أي استراتيجية." : `الملف فيه ${m[2]} استراتيجيات؛ حدّد المطلوبة بالصيغة file.py:ClassName`],
  [/^(\w+): strategy `name` is required\.$/, (m) => `${m[1]}: لازم تحدد اسم الاستراتيجية (name).`],
  [/^(\w+): unsupported timeframe (.+)\.$/, (m) => `${m[1]}: الإطار الزمني ${m[2]} غير مدعوم.`],
  [/^unsupported timeframe (.+)\.$/, (m) => `الإطار الزمني ${m[1]} غير مدعوم.`],
  [/^(\w+): warmup_bars must be a positive int\.$/, (m) => `${m[1]}: عدد شموع التهيئة (warmup_bars) لازم يكون عددًا صحيحًا موجبًا.`],
  [/^(\w+): on_bar is not implemented\.$/, (m) => `${m[1]}: الدالة on_bar غير موجودة.`],
  [/^(\w+)\.on_bar must return Signal or None\.$/, (m) => `${m[1]}: الدالة on_bar لازم ترجع Signal أو None.`],
  [/^bar open_time must be timezone-aware\.$|^timestamps must be timezone-aware\.$/, () => "الوقت لازم يتضمن المنطقة الزمنية."],
  [/^timestamp (.+) has no timezone; use UTC/, (m) => `الوقت ${m[1]} بدون منطقة زمنية؛ استخدم توقيت غرينتش (+00:00).`],
  [/^bar prices must be positive finite Decimals\.$/, () => "أسعار الشمعة لازم تكون أرقامًا موجبة."],
  [/^bar volume must be a non-negative finite Decimal\.$/, () => "حجم تداول الشمعة لازم يكون رقمًا غير سالب."],
  [/^inconsistent OHLC at (\S+)\.$/, (m) => `أسعار الشمعة غير متّسقة (الافتتاح/الأعلى/الأدنى/الإغلاق) عند ${m[1]}.`],
  [/^BarHistory (was given|view would expose) a bar that is not closed yet \(look-ahead\)\.$/, () => "رُفضت شمعة لم تُغلق بعد (منعًا لاستخدام بيانات مستقبلية)."],
  [/^unknown OHLCV field '(.*)'\.$/, (m) => `حقل غير معروف في بيانات الشمعة «${m[1]}»؛ المتاح: open وhigh وlow وclose وvolume.`],
  [/^bars must be strictly increasing\.$/, () => "الشموع لازم تكون مرتبة زمنيًا بدون تكرار."],
  [/^signal target must be a finite Decimal\.$/, () => "النسبة المستهدفة في الإشارة لازم تكون رقمًا صالحًا."],
  [/^signal target must be within \[0, 1\]/, () => "النسبة المستهدفة لازم تكون بين 0 و1 (تداول فوري، شراء فقط، بدون رافعة مالية)."],
  [/^no bars\.$/, () => "ما فيه شموع في الفترة المحددة."],
  [/^mixed timeframes in one series\.$/, () => "السلسلة فيها أطر زمنية مختلفة."],
  [/^bars not strictly increasing at (\S+)\.$/, (m) => `الشموع غير مرتبة أو مكررة عند ${m[1]}.`],
  [/^bar off the (\S+) grid at (\S+)\.$/, (m) => `شمعة ما تطابق توقيت إطار ${tfAr(m[1])} عند ${m[2]}.`],
  [/^data file must be \.csv or \.feather\.$/, () => "ملف البيانات لازم يكون بصيغة .csv أو .feather."],
  [/^redirect refused for public klines\.$/, () => "رُفض تحويل اتصال جلب الأسعار من Binance (إجراء حماية)."],
  [/^InvalidOperation:/, () => "قيمة رقمية غير صالحة في ملف البيانات."],
  [/^need at least 2 bars/, () => "يلزم شمعتان على الأقل للتشغيل."],
  [/^strategy timeframe (\S+) != data timeframe (\S+)\.$/, (m) => `إطار الاستراتيجية (${tfAr(m[1])}) ما يطابق إطار ملف البيانات (${tfAr(m[2])}).`],
  [/^benchmark needs at least 2 bars\.$/, () => "المقارنة مع الاحتفاظ تحتاج شمعتين على الأقل."],
  [/^(.+) already exists; use a new output folder per run\.$/, () => "مجلد النتائج موجود؛ كل تجربة تحتاج مجلدًا جديدًا."],
  [/^observation (\S+) is not one of the bars\.$/, () => "قراءة السوق ما تطابق أي شمعة (خطأ داخلي)."],
  [/^next_open must be the open/, () => "سعر الافتتاح التالي ما يخص الشمعة اللي بعد آخر شمعة مغلقة."],
  [/^(.+) already holds a session; use a new folder\.$/, () => "فيه جلسة بنفس الاسم؛ اختر اسمًا آخر."],
  [/^bars\.jsonl is corrupt at line (\d+)/, (m) => `ملف الشموع bars.jsonl تالف عند السطر ${m[1]}؛ أُوقفت الجلسة لحماية البيانات.`],
  [/^strategy file changed since the session was created/, () => "ملف الاستراتيجية تغيّر بعد إنشاء الجلسة؛ أنشئ جلسة جديدة."],
  [/^Freqtrade Python not found: (.+) \(set CT_FREQTRADE_PYTHON\)\.$/, (m) => `ما لقيت بيئة Python الخاصة بـ Freqtrade: ${m[1]} (حدّد مسارها في CT_FREQTRADE_PYTHON).`],
  [/^Freqtrade helper did not answer within (\d+)s/, (m) => `برنامج Freqtrade المساعد ما استجاب خلال ${m[1]} ثانية؛ أُوقف احترازيًا.`],
  [/^Freqtrade helper exited unexpectedly\.$/, () => "برنامج Freqtrade المساعد توقف فجأة."],
  [/^Freqtrade strategy error: (.*)$/, () => "خطأ في استراتيجية Freqtrade."],
  [/^Freqtrade strategy (\w+) cannot run in this engine: (.+)$/, (m) => `استراتيجية Freqtrade «${m[1]}» ما تشتغل في هذا المحرك: ` + m[2].split("; ").map((p) =>
    /^can_short/.test(p) ? "البيع على المكشوف والعقود الآجلة غير مدعومة" : /^informative pairs/.test(p) ? "البيانات المساندة من أزواج أو أطر أخرى غير مدعومة"
    : /^custom callbacks not supported: (.+)$/.test(p) ? "دوال مخصّصة غير مدعومة: " + /^custom callbacks not supported: (.+)$/.exec(p)[1]
    : /^timeframe (.+) is not supported$/.test(p) ? `الإطار ${/^timeframe (.+) is not supported$/.exec(p)[1]} غير مدعوم` : p).join("؛ ")],
  [/^Freqtrade spec must be/, () => "صيغة استراتيجية Freqtrade لازم تكون: freqtrade:path/to/File.py:ClassName"],
  [/^adapter called twice for the same bar\.$/, () => "المحوّل استُدعي مرتين لنفس الشمعة (خطأ داخلي)."],
  [/^indicator period must be a positive int\.$/, () => "فترة المؤشر لازم تكون عددًا صحيحًا موجبًا."],
  [/^unknown indicator '(.*)'; available: \[(.*)\]$/, (m) => `مؤشر غير معروف «${m[1]}». المتاح: ${m[2].replace(/'/g, "")}`],
  [/^risk limits rejected by the engine: (?:\w+Error: )?unknown risk limits keys: \[(.*)\]$/, (m) => `مفاتيح غير معروفة في حدود المخاطر: ${m[1].replace(/'/g, "")}`],
  [/^risk limits rejected by the engine: (?:\w+Error: )?missing mandatory risk limits keys: \[(.*)\]$/, (m) => `حدود مخاطر إلزامية ناقصة: ${m[1].replace(/'/g, "")}`],
  [/^risk limits rejected by the engine: (?:\w+Error: )?(\w+) must be a decimal number\.$/, (m) => `قيمة «${arOf(AR.limits, m[1])}» لازم تكون رقمًا.`],
  [/^risk limits rejected by the engine: (?:\w+Error: )?(\w+) must be a positive Decimal\.$/, (m) => `قيمة «${arOf(AR.limits, m[1])}» لازم تكون أكبر من صفر.`],
  [/^risk limits rejected by the engine: (?:\w+Error: )?(\w+) must be a positive integer\.$/, (m) => `قيمة «${arOf(AR.limits, m[1])}» لازم تكون عددًا صحيحًا أكبر من صفر.`],
  [/^risk limits rejected by the engine: (?:\w+Error: )?(\w+) must be an integer\.$/, (m) => `قيمة «${arOf(AR.limits, m[1])}» لازم تكون عددًا صحيحًا (بدون علامات تنصيص).`],
  [/^risk limits rejected by the engine: (?:\w+Error: )?(\w+) must be a boolean\.$/, (m) => `قيمة «${arOf(AR.limits, m[1])}» لازم تكون true أو false.`],
  [/^risk limits rejected by the engine: /, () => "المحرك رفض حدود المخاطر."],
  [/^ValueError: invalid literal for int\(\)/, () => "قيمة معامل غير صالحة: لازم تكون عددًا صحيحًا (مثل 20)."],
  [/^Failed to fetch$|NetworkError|Load failed/, () => "تعذّر الاتصال بالخادم المحلي. تأكد إن الواجهة شغالة وحدّث الصفحة."],
  [/is not valid JSON|Unexpected token/, () => "رد غير متوقع من الخادم."],
];

/** Any error text -> {ar, raw}; ar is null only when nothing safe is known (then the raw text is shown as technical detail). */
function trErr(text) {
  const raw = String(text ?? "").replace(/^ERROR: /, "");
  if (typeof LANG !== "undefined" && LANG === "en" && !/[\u0600-\u06FF]/.test(raw)) return { ar: raw, raw: null };   // English: the original message
  if (/[؀-ۿ]/.test(raw)) return { ar: raw, raw: null };   // the UI's own messages are already Arabic
  for (const [re, fn] of ERRS) { const m = re.exec(raw); if (m) return { ar: fn(m), raw }; }
  const net = netCause(raw);
  if (net) return { ar: net, raw };
  if (/^\w+(Error|Exception): /.test(raw)) return { ar: "حدث خطأ غير متوقع. التفاصيل التقنية تحت، وفي صفحة السجلات.", raw };
  return { ar: null, raw };
}

/** paper loop.log line -> Arabic (known shapes only; everything else unchanged). */
function logLineAr(line) {
  if (typeof LANG !== "undefined" && LANG === "en") return line;   // English: the original log line
  let m = /^\[([^\]]+)\] bars=(\d+) new=(\d+) live=(\d+) equity=(\S+) fills=(\S+) position=(.*)$/.exec(line);
  if (m) {
    let pos = m[7];
    try { const o = JSON.parse(pos.replace(/'/g, '"')); const e = Object.entries(o).filter(([, v]) => +v); pos = e.length ? e.map(([k, v]) => `${v} ${k}`).join(" · ") : "بدون مركز"; } catch { if (pos === "None") pos = "—"; }
    const nn = (v) => (v === "None" ? "—" : v);
    return `[${m[1].slice(0, 19).replace("T", " ")}] الشموع: ${m[2]} · جديدة: ${m[3]} · حيّة: ${m[4]} · الرصيد: ${nn(m[5])} · عمليات التنفيذ: ${nn(m[6])} · المركز: ${pos}`;
  }
  m = /^cycle failed, will retry: (.*)$/.exec(line);
  if (m) return `فشلت الدورة، وستُعاد المحاولة: ${netCause(m[1]) || m[1]}`;
  if (line === "STOP file found; paper loop stopped.") return "أُوقفت الجلسة بناءً على طلب الإيقاف.";
  return line;
}

/** pytest summary line -> Arabic counts. */
function pytestAr(line) {
  const s = String(line || "");
  if (typeof LANG !== "undefined" && LANG === "en") return s;
  const n = (w) => (new RegExp("(\\d+) " + w).exec(s) || [])[1];
  const parts = [[n("passed"), "ناجح"], [n("failed"), "فاشل"], [n("errors?"), "خطأ"], [n("skipped"), "متجاوَز"], [n("xfailed"), "فشل متوقَّع"],
    [n("xpassed"), "نجاح غير متوقَّع"], [n("deselected"), "مستبعَد"], [n("warnings?"), "تحذير"]].filter(([v]) => v).map(([v, w]) => `${v} ${w}`);
  const dur = /\((\d+:\d{2}:\d{2})\)/.exec(s);
  if (!parts.length) return /no tests ran/.test(s) ? "لم يُشغَّل أي اختبار" : s;
  return parts.join(" · ") + (dur ? ` · المدة ${dur[1]}` : "");
}

/** Arabic count phrase with correct agreement for common nouns. */
function countAr(n, one, two, few, many) {
  n = +n;
  if (n === 1) return one;
  if (n === 2) return two;
  if (n >= 3 && n <= 10) return `${n} ${few}`;
  return `${n} ${many}`;
}
