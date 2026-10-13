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

## توزيع المراجعين والمنصات

This is the mandatory reviewer model, execution-provider and provenance routing, approved by the owner on 2026-10-13. The table is copied verbatim from the verified user-level Claude policy; this section is authoritative for Codex and repository agents.

| Reviewer | REQUIRED platform/account | Role |
| --- | --- | --- |
| GPT-6.1 Sol Medium | OpenAI / Codex | Defect discovery, dispute resolution, final adjudication |
| Claude Opus 5.5 | Anthropic / Claude | Architecture and governance |
| Claude Sonnet 5.5 | Anthropic / Claude | Safety, security, edge-case tests |
| Claude Opus 4.6 — Thinking required, subject to runtime verification — Google Antigravity only | Google Antigravity, NOT Claude CLI | Additional independent adversarial review |
| Claude Sonnet 4.6 — Thinking required, subject to runtime verification — Google Antigravity only | Google Antigravity, NOT Claude CLI | Source-code and vulnerability review |
| Gemini 3.1 Pro High | Google | Algorithms, mathematical checks, fingerprints |
| DeepSeek "Saeed" | DeepSeek account/service | Independent vulnerability and contradiction review |

### Mandatory runtime verification and prior-review treatment

1. Verify the ACTUAL serving model identity, model's supported thinking setting, platform, and account BEFORE invoking any reviewer. Names alone are not sufficient. Do not silently substitute provider, model, or route. If Google Antigravity is inaccessible, STOP its assigned review and report the obstacle; never route either 4.6 Thinking reviewer to Anthropic Claude CLI/API. If Thinking cannot be proven from runtime settings or a trusted log for either 4.6 reviewer, STOP that review and report; never run it without Thinking or label it as Thinking.
2. Use Saeed's strongest model ACTUALLY available to the account at the time, with its maximum supported reasoning configuration (if any). The owner expressly authorizes consuming existing PAID DeepSeek credit for reviews. Do not automatically top up or alter billing/credentials. Do not call Flash the strongest without checking availability.
3. The owner ACCEPTED earlier completed 4.6 reviews performed via Claude CLI notwithstanding their provider mismatch. Do not repeat them solely because of that historical provider mismatch. Preserve original provenance: do not relabel them as Google reviews.
4. This routing approval does NOT override the engineering/release gate: each independent report must genuinely exist, be source/Git-Tree-pinned, and distinguish observed evidence from speculation. GPT-6.1 Sol adjudicates only from verified reports. NO-GO stays in force when any required evidence or blocker closure is missing. No release approval commit just because routing was approved.
5. No real/live/testnet trading orders, no push/merge/deploy, no touching unrelated protected WIP, no reset/checkout/clean, never read, print, log, or copy secret values; using already-configured credentials through their official tools is allowed. Respect repo AGENTS.md and engineering_rules.md and explicit scope/authorization for sensitive files.
6. The assistant/ChatGPT is the coordinator and not a substitute for any of the seven external reviewers. Cost/usage must be accurately attributed to the platform actually used.
