# CryptoTrader-Agents-Lab: mandatory governance
Scope: this repository. These instructions guide process and grant no operational authority.
Precedence: AGENTS.md > engineering_rules.md > lessons_candidates.md.
Within governance, this file is authoritative; engineering_rules.md only interprets and applies it.
lessons_candidates.md has no authority and cannot change behavior; CLAUDE.md is only a loader.
Any governance conflict or ambiguity: STOP -> SENSITIVE -> human resolution; do not choose a rule yourself.
Read engineering_rules.md before changes; record both version fields, repository_policy_id and file hashes.
Start read-only: verify HEAD/tree/status/refs and preserve existing tracked and untracked work.
Do not modify main/master, rewrite history, or include unrelated local files.
For every material step, ChatGPT inspects evidence first; release-wide independent reviewer assessment occurs once per version under قاعدة المراجعة الدائمة.
Resolve end-of-version reviewer disagreements by source evidence, independent Sol adjudication, and the fixed GO/NO-GO gate; unresolved blockers stop release approval.
Reviewer silence, failure or model substitution is never approval; a reviewer who did not produce source-pinned output is not counted.
Obtain explicit human approval for the proposed material/sensitive scope before implementation.
Implement only approved scope; verify each item by regression tests and full pytest before its own named-files-only commit; perform a single seven-reviewer batch at version end and record provenance.
A local checkpoint requires separate explicit human authorization; review approval alone cannot create it.
These files authorize no push, merge, deploy, promotion or live action.
PROVENANCE_VERIFIED proves only the exact candidate provenance claimed by its verifier.
Completion requires appropriate tests/traces/logs/mutation/provenance bound to the reviewed candidate.
Self-assessment, votes and historical test counts cannot replace current evidence.
If new evidence falsifies the plan or a result is unexpected, stop and present evidence before further edits.
Fix the root cause with the smallest necessary delta; no opportunistic refactor or elegance work.
Classify every intended changed path under the pinned repository policy before editing.
Unknown classification is SENSITIVE; any sensitive change receives the highest review tier regardless of size.
Autonomy is policy-limited: the initial ROUTINE allowlist is empty; only read-only analysis is autonomous.
Subagents may research/read/cross-check only; they cannot write or decide safety, invariants or merge authority.
Lessons follow candidate -> review -> human promotion -> version -> retirement; never auto-update rules.
Human approval and explicit versioning are required for material permanent-rule changes/removal and lesson retirement.
Never silently change existing taxonomy/enum meanings; preserve persisted contracts and verify affected replay.
Inspect applicable loading overrides; missing required rules, ambiguity or conflict means STOP -> SENSITIVE -> human resolution.

## قاعدة المراجعة الدائمة

هذه قاعدة دائمة لجميع إصدارات مشروع CryptoTrader-Agents-Lab (v7 وv8 وما بعدها)، بناءً على اعتماد المالك الصريح، مع الحفاظ على قيود الأمان وحماية WIP.

1. لا تُستدعى لجنة المراجعين السبعة عند كل بند أو إصلاح. تُستدعى **مرة واحدة فقط، دفعة واحدة في نهاية كل إصدار**، بعد إتمام البنود واختباراتها وcommits الخاصة بها. لا تُنشأ مراجعات شكلية أو توقيعات لمراجعين لم يعملوا.
2. الدورة الإلزامية لكل بند داخل الإصدار: **إصلاح محدود النطاق → اختبار انحدار يثبت الإصلاح → pytest كامل دون إخفاقات → commit مستقل بأسماء الملفات المعنية فقط → البند التالي**. لا يُجمع بندان في commit واحد، ولا ينتقل إلى البند التالي قبل تحقق هذه الدورة. ظهور فشل حقيقي أو حد أمان يلزم إغلاقه بأضيق تغيير وإعادة الاختبار، وليس تجاوزه.
3. بعد آخر بند، يُنشأ جدول إغلاق الإصدار: **البند ← SHA الـcommit المستقل ← نتيجة pytest الكامل**، مع البصمات والمخرجات الفعلية لكل بند.
4. تُنشأ **حزمة مراجعة واحدة** للنسخة النهائية، مرتبطة بالبصمات الفعلية وGit Tree الخاص بها، وتضم الشيفرة المتغيرة والاختبارات والأدلة وبرومبتًا مستقلًا لكل مراجع.
5. تُشغَّل لجنة السبعة مرة واحدة جماعيًا بنهاية الإصدار: **GPT-6.1 Sol** (الحكم)، **Claude Opus 5.5**، **Claude Sonnet 5.5**، **Claude Opus 4.6 Thinking**، **Claude Sonnet 4.6** (لا يُنسب وضع Thinking إلا بدليل)، **Gemini 3.1 Pro**، و**DeepSeek سعيد** وفق أعلى نموذج متاح ومثبت. يُحافظ على المراجعات الأصلية مستقلة ولا تُستبدل صامتًا.
6. بعد وصول التقارير الحقيقية المرتبطة ببصمات الإصدار، يجمع **GPT-6.1 Sol** الاعتراضات، يزيل المكرر، يرفض ما لا يثبته سطر شيفرة أو اختبار قابل لإعادة التشغيل، ويصنّف مانعًا أو غير مانع. الاعتراضات غير المانعة تُرحّل إلى backlog للإصدار التالي.
7. معيار **GO البحثي** ثابت: **صفر اعتراضات مانعة مثبتة + pytest كامل دون إخفاقات + إثبات Git Tree للنسخة**. عند NO-GO تُغلق الاعتراضات المحددة ويُعاد تحقق مقدم الاعتراض فقط من الإصلاح. لا يُضاف شرط اعتماد جديد دون قرار صريح من المالك.
8. بعد GO فقط، يُكتب **commit إغلاق الإصدار المعتمد** ويُثبت SHA الـcommit والـTree الفعلي ومطابقته لشجرة الإصدار المقصودة. لا يُعمل push أو merge أو نشر أو ترقية تشغيلية دون تصريح منفصل.
9. هذا AGENTS.md وسائر ملفات الحوكمة **مستبعدة من بصمات نطاق الكود الخاصة بـv6 وv7** ومن commits البنود البرمجية؛ عند تعديل الحوكمة يجري تثبيت مصدرها ومراجعتها مستقلاً عن حزمة الإصدار. حماية WIP أولوية: لا reset/checkout/clean، لا staging شامل، ولا تعديل أو commit للملفات غير المعتمدة.
10. امتثال السلامة دائم: Research-only لا يمنح صلاحية التداول الفعلي أو Paper/Testnet/Live، ولا OMS/Execution أو مفاتيح أو Billing، ومراجعة الإصدار ليست ترخيصًا لهذه العمليات. المنع الأمني لا يُتجاوز.
