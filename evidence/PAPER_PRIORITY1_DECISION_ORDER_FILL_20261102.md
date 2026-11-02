# أولوية 1: إثبات تسلسل Decision → Order → Fill في Paper / NautilusTrader
التاريخ: 2026-11-02 — الحالة: **مثبتة للنطاق المحلي المختبر فقط**.

## نسخة الشيفرة
- Commit: `af484ae52d98d9a56b51136e2508b4c3ffabacf1`
- Git tree: `96045eed806fa98123be9c591f464b4a069b143b`
- محرك NautilusTrader: `1.231.0`، دفتر `L2_MBP`، تنفيذ محاكى محليًا.
- لم تُعدّل ملفات الشيفرة، ولم يُنفذ دمج أو نشر أو اتصال Testnet/Live.

## نتيجة اختبارات الأولوية
نفّذنا الاختبارات التالية من نسخة التطوير المعزولة عبر Python 3.12 وpytest:
- `test_paper_nautilus.py`
- `test_p02_nautilus_fills.py`
- `test_p03_temporal_ordering.py`
- `test_p03_causality_accounting.py`
- `test_crew_paper_bridge.py`
- `test_paper_end_to_end.py`

نتيجة JUnit النهائية: **93 passed, 0 failures, 0 errors, 0 skipped** في 69.984 ثانية. ظهرت **79 warning**، غالبها تحذيرات تقادم Pandas؛ لا توجد حالات اختبار فاشلة. هذه اختبارات متخصصة وليست تشغيل pytest الكامل لكل المشروع.

## شاهد مستقل من SQLite
شُغّلت حالة Paper أصلية من `NautilusSimulationRunner` على تدفق تداول افتراضي لزوج ETHUSDT، ثم فُحص سجل SQLite وتحقق `report_from_journal(strict_native_provenance=True)`.

ترتيب الإدخال الفعلي في السجل:
1. `strategy.decision.recorded` — 12:00:00 UTC
2. `trade.intent.created` — 12:00:00 UTC
3. `validity.disposition.recorded` — 12:00:00 UTC
4. `risk.decision.recorded` — 12:00:00 UTC
5. `risk.evaluation.recorded` — 12:00:00 UTC، السماح مثبت
6. `order.state.changed` — 12:00:00.010 UTC
7. `order.state.changed` — 12:00:00.010 UTC
8. `order.state.changed` — 12:00:00.030 UTC
9. `execution.fill.recorded` — 12:00:00.030 UTC

تحققنا أن intent يشير إلى event_id للقرار، وأن أحداث order/fill تشير إلى event_id للـintent نفسه؛ هوية الأمر مطابقة، الكمية مطابقة، الوقت بعد القرار، والرصيد المتبقي صفر. أعاد تقرير السجل بناء أمر نهائي `filled` وFill واحدًا دون خطأ.

**تنبيه مهم في قراءة السجل:** `read_by_correlation` و`read_range` ترتبان أحداث التوقيت الواحد وفق `occurred_at_ns` ثم `sequence_number` ثم `event_id`، ولا تحتفظان بترتيب الإدخال ضمن الطابع الزمني المتساوي. لذلك فُحص ترتيب الإدخال باستخدام `rowid` في SQLite المحفوظ لهذا الشاهد، واستُخدمت روابط `causation_id` لإثبات السببية. لا يجوز اعتبار ترتيب واجهة القراءة ذات الطابع الزمني المتساوي دليلًا على الترتيب السببي.

## بصمات الأدلة
- JUnit XML SHA256: `9f316dec5a9ce7dce712069e20d09997ae7ae6e4214b093c59043500773fd179`
- JSON trace SHA256: `58dd7ab8d04f4a8f34048b7eb3c1e3fc792d228c010c179c13fb8724ae315339`
- SQLite trace SHA256: `8c5ab1c99c32348e39c223eb802fac416d89e6e7d8673fdb7dad194d11f9dc82`

الأدلة الخام محفوظة خارج المستودع في مجلد إثبات الأولوية 1 على Windows، ولم تُرفع ملفات البيانات الخام إلى GitHub.

## الحدود
هذا إثبات لسلوك Paper / NautilusTrader المختبر، وليس إثباتًا لجودة الاستراتيجية أو جاهزية التشغيل الحقيقي. جميع الأوامر كانت محاكاة محلية. B1 وB4 يظلان HOLD دون تعديل أو إعادة فتح، وفق قرار المالك. Live وTestnet يبقيان NO-GO.
