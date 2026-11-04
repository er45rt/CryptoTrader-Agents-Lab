# CryptoTrader-Agents-Lab: mandatory governance
Scope: this repository. These instructions guide process and grant no operational authority.
Precedence: AGENTS.md > engineering_rules.md > lessons_candidates.md.
Within governance, this file is authoritative; engineering_rules.md only interprets and applies it.
lessons_candidates.md has no authority and cannot change behavior; CLAUDE.md is only a loader.
Any governance conflict or ambiguity: STOP -> SENSITIVE -> human resolution; do not choose a rule yourself.
Read engineering_rules.md before changes; record both version fields, repository_policy_id and file hashes.
Start read-only: verify HEAD/tree/status/refs and preserve existing tracked and untracked work.
Do not modify main/master, rewrite history, or include unrelated local files.
Review follows the risk tiers of the owner-approved autonomy charter (Low: diff proof; Medium/High: GPT-6.1 Sol High); ChatGPT is advisory only. Release-wide independent assessment occurs once per version by Sol under قاعدة المراجعة الدائمة.
Resolve end-of-version reviewer disagreements by source evidence, independent Sol adjudication, and the fixed GO/NO-GO gate; unresolved blockers stop release approval.
Reviewer silence, failure or model substitution is never approval; a reviewer who did not produce source-pinned output is not counted.
Obtain explicit human approval for the proposed material/sensitive scope before implementation; items listed in Annex A of the owner-approved autonomy charter count as explicit approval of exactly their scope.
Implement only approved scope; verify each item by regression tests and full pytest before its own named-files-only commit; perform a single Sol High review at version end and record provenance.
A local checkpoint requires separate explicit human authorization; review approval alone cannot create it.
These files authorize no push, merge, deploy, promotion or live action.
PROVENANCE_VERIFIED proves only the exact candidate provenance claimed by its verifier.
Completion requires appropriate tests/traces/logs/mutation/provenance bound to the reviewed candidate.
Self-assessment, votes and historical test counts cannot replace current evidence.
If new evidence falsifies the plan or a result is unexpected, stop and present evidence before further edits.
Fix the root cause with the smallest necessary delta; no opportunistic refactor or elegance work.
Classify every intended changed path under the pinned repository policy before editing.
Unknown classification is SENSITIVE; any sensitive change receives the highest review tier regardless of size.
Autonomy is policy-limited: the initial ROUTINE allowlist is empty; only read-only analysis is autonomous, except within the owner-approved autonomy charter and its limits.
Owner-approved autonomy charter: C:\Users\Jamal\ct_fix_runs\autonomy_charter_v3.md (sha256 bb2fcdc2cbf94819e9c5104bcef680e7fc33bf89cbbce1be0220d35d9fad7e1d); any other version grants nothing.
Subagents may research/read/cross-check only; they cannot write or decide safety, invariants or merge authority.
Lessons follow candidate -> review -> human promotion -> version -> retirement; never auto-update rules.
Human approval and explicit versioning are required for material permanent-rule changes/removal and lesson retirement.
Never silently change existing taxonomy/enum meanings; preserve persisted contracts and verify affected replay.
Inspect applicable loading overrides; missing required rules, ambiguity or conflict means STOP -> SENSITIVE -> human resolution.

## قاعدة المراجعة الدائمة

هذه قاعدة دائمة لجميع إصدارات مشروع CryptoTrader-Agents-Lab (v7 وv8 وما بعدها)، بناءً على اعتماد المالك الصريح، مع الحفاظ على قيود الأمان وحماية WIP. تعديل 2026-11-03 بقرار المالك: أُلغيت لجنة المراجعين السبعة، والمراجع المستقل الوحيد هو GPT-6.1 Sol High.

1. المراجعة المستقلة الشاملة للإصدار يجريها **GPT-6.1 Sol High** **مرة واحدة فقط في نهاية كل إصدار**، بعد إتمام البنود واختباراتها وcommits الخاصة بها. أما مراجعات البنود فتتبع جدول الخطر في وثيقة التفويض. لا تُنشأ مراجعات شكلية أو توقيعات لمراجع لم يعمل. ويُستثنى من ذلك أن يُستدعى Sol لأي موضوع أو مهمة يطلبها المالك في أي وقت، ولا يقتصر عمله على نظام التداول. وتُطبّق قواعد استقلالية المراجع والتحقق من الهوية والإعدادات والأدلة على جميع المهام، بينما تُطبّق متطلبات Git Tree وبصمات الملفات واختبارات البرمجيات على المهام التي تنطبق عليها بحسب طبيعتها، دون فرضها على الموضوعات العامة. وعند الشك في انطباقها يُرفع القرار للمالك.
2. الدورة الإلزامية لكل بند داخل الإصدار: **إصلاح محدود النطاق → اختبار انحدار يثبت الإصلاح → pytest كامل دون إخفاقات → commit مستقل بأسماء الملفات المعنية فقط → البند التالي**. لا يُجمع بندان في commit واحد، ولا ينتقل إلى البند التالي قبل تحقق هذه الدورة. ظهور فشل حقيقي أو حد أمان يلزم إغلاقه بأضيق تغيير وإعادة الاختبار، وليس تجاوزه.
3. بعد آخر بند، يُنشأ جدول إغلاق الإصدار: **البند ← SHA الـcommit المستقل ← نتيجة pytest الكامل**، مع البصمات والمخرجات الفعلية لكل بند.
4. تُنشأ **حزمة مراجعة واحدة** للنسخة النهائية، مرتبطة بالبصمات الفعلية وGit Tree الخاص بها، وتضم الشيفرة المتغيرة والاختبارات والأدلة وبرومبت Sol.
5. يُشغَّل **GPT-6.1 Sol High** مرة واحدة بنهاية الإصدار، بعد التحقق من النموذج ومستوى High من الإعدادات الفعلية أو سجل موثوق. يُحافظ على تقريره الأصلي ولا يُستبدل صامتًا.
6. بعد وصول تقرير Sol الحقيقي المرتبط ببصمات الإصدار، يصنّف Sol الاعتراضات مانعة أو غير مانعة، ويرفض ما لا يثبته سطر شيفرة أو اختبار قابل لإعادة التشغيل. الاعتراضات غير المانعة تُرحّل إلى backlog للإصدار التالي.
7. معيار **GO البحثي** ثابت: **صفر اعتراضات مانعة مثبتة + pytest كامل دون إخفاقات + إثبات Git Tree للنسخة**. عند NO-GO تُغلق الاعتراضات المحددة ويُعاد تحقق مقدم الاعتراض فقط من الإصلاح. لا يُضاف شرط اعتماد جديد دون قرار صريح من المالك.
8. بعد GO فقط، يُكتب **commit إغلاق الإصدار المعتمد** ويُثبت SHA الـcommit والـTree الفعلي ومطابقته لشجرة الإصدار المقصودة. لا يُعمل push أو merge أو نشر أو ترقية تشغيلية دون تصريح منفصل.
9. هذا AGENTS.md وسائر ملفات الحوكمة **مستبعدة من بصمات نطاق الكود الخاصة بـv6 وv7** ومن commits البنود البرمجية؛ عند تعديل الحوكمة يجري تثبيت مصدرها ومراجعتها مستقلاً عن حزمة الإصدار. حماية WIP أولوية: لا reset/checkout/clean، لا staging شامل، ولا تعديل أو commit للملفات غير المعتمدة.
10. امتثال السلامة دائم: Research-only لا يمنح صلاحية التداول الفعلي أو Paper/Testnet/Live، ولا OMS/Execution أو مفاتيح أو Billing، ومراجعة الإصدار ليست ترخيصًا لهذه العمليات. المنع الأمني لا يُتجاوز.

## توزيع المراجعين والمنصات

Owner decision 2026-11-03: the seven-reviewer committee approved on 2026-10-13 is retired. This section is authoritative for Codex and repository agents and matches the user-level Claude policy.

| Reviewer | REQUIRED platform/account | Role |
| --- | --- | --- |
| GPT-6.1 Sol High | OpenAI / Codex (official Codex CLI) | Independent review of Medium/High changes, defect discovery, final adjudication at the end of each version |

### Mandatory runtime verification and prior-review treatment

1. Verify the ACTUAL serving model (gpt-6.1-sol) and the High reasoning level from actual runtime settings or a trusted log BEFORE relying on any Sol review. Names alone are not sufficient. Do not silently substitute provider or model. If High cannot be verified, STOP that review and report; never label the reviewer as High without evidence.
2. Reviews previously completed by the former committee members or by Astra keep their original provenance; do not relabel or repeat them solely because of this change.
3. This routing does NOT override the engineering/release gate: each review must genuinely exist, be source/Git-Tree-pinned, and distinguish observed evidence from speculation. NO-GO stays in force when any required evidence or blocker closure is missing. No release approval commit just because routing was approved.
4. No real/live/testnet trading orders, no push/merge/deploy, no touching unrelated protected WIP, no reset/checkout/clean, never read, print, log, or copy secret values; using already-configured credentials through their official tools is allowed. Respect repo AGENTS.md and engineering_rules.md and explicit scope/authorization for sensitive files.
5. ChatGPT is advisory only and not a substitute for Sol. Cost/usage must be accurately attributed to the platform actually used.
