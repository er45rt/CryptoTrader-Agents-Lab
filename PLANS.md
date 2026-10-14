# Owner-approved project priority order — research roadmap

The five-step order was approved earlier and reconfirmed on 2026-10-13. It records priorities; it does not assert that every earlier milestone is closed, and it grants no implementation, trading, or deployment authority.

1. **Causal timing of decisions, orders and fills.** Establish correct temporal causality in offline simulation.
2. **B1–B8 approved corrections.** Complete and verify the previously approved fixes within their established scope: focused regressions, full offline pytest and named-files-only commits per item; independent seven-reviewer assessment once at the end of the version, not after every item.
3. **Portfolio, risk, ledger, reconciliation and recovery.** Following priorities 1 and 2, this is the next area to investigate after the sealed Paper V8 research closure. Begin with a read-only M0 evidence inventory (no code changes): compare Paper P-03/P-04/P-06 against outstanding requirements (see the 'Paper item order' section below). Examine a source-observed risk-event contract conflict, pinned to code commit bf5c39a8d0df00f388ff19a76e393e2085dffd55: src/crypto_trader/risk/engine.py lines 234, 449, 466 emit base_quantity, which src/crypto_trader/events/contracts.py line 625 does not admit. This observation is not an authority to change risk code.
4. **Strategy approval gate.** Trial Registry, walk-forward, blind holdout, Probability of Backtest Overfitting (PBO) and Deflated Sharpe Ratio (DSR). This is a research validation gate, not an operational-readiness or trading-enablement gate.
5. **Strategy evaluation, then offline Paper.** Evaluate only strategies that have passed the priority 4 research validation gate; only then collect additional offline Paper research evidence. The earlier Paper V8 research GO (closure commit 291a041c65d9082105ea20a19527544a631d4b94, reviewed Code Tree 745da9716c215d9f71082a15eedbc49a4d4e01c1) is a sealed historical decision, not an external-broker or live authorization.

**Sequence and separation:** The priority order is 1 → 2 → 3 → 4 → 5. Priority 3 precedes new priority 4/5 work; the owner will choose its next approved scope after M0. DerivArchive C-01 through C-04 remains a separately scoped side track, not the next default release; unattended collection remains NO-GO under the 'DerivArchive — continuous operation readiness plan' section below, and there is no Stage3A v9 (see its 'Gates and sequence' section). Sensitive changes and commits require their own human approval and evidence under the pinned policy: repository_policy_id cryptotrader-agents-lab, governance_schema_version 1, repository_policy_version 2 (engineering_rules.md at commit 291a041c65d9082105ea20a19527544a631d4b94). This plan grants no Push, Merge, Deploy, Testnet, Mainnet, Live, unattended collection, or automatic strategy promotion.

---
# DerivArchive — continuous operation readiness plan

Status: **NO-GO for unattended or continuous collection**, as of 2026-10-12. The only currently approved live data activity is a bounded, monitored, one-hour ETHUSDT public-data pilot under `--pilot-seconds 3600`. No trading credentials, orders, Testnet, Mainnet, OMS, execution, or promotion.

## Evidence and scope

- Historical Binance public data are stored outside Git in `C:\Users\Jamal\DerivArchive\backfill\` as JSON; their unknown historical publication time makes them **research-only**, not causal/replay evidence. Do not combine backfill JSON with immutable DARC receipt records.
- One-shot public ETHUSDT backfill with rolling-retention safety margin: commit `cd4d7801470d5b90a0dd8e200d40545052b989bd`. Successful evidence file: `C:\Users\Jamal\DerivArchive\backfill\backfill-report-1791796862113.json` (7,534 funding records, 8,628 OI and 8,628 long/short records; no observed gaps in captured windows).
- Supervised one-hour collector stop bound: commit `778c4c81757f1053b491e873485a5388438c5fde`. Pilot completion must be evidenced by a valid terminal `clean_stop`, observed duration, and a byte-valid DARC archive.
- No auto-start Windows task, service, or restart wrapper is approved; **do not configure these while NO-GO**.

## C-01 — Daily monitoring and archive coverage (not started)

Implement a **read-only** DARC inspector with bounded frame validation, checksum verification, record type counts, newest observation and heartbeat by metric, actual elapsed collection hours, visible collection gaps, provider failures, and explicit stale/missing-heartbeat warnings. Separate `no evidence` from `zero gaps`. Write a dated JSON/text report outside immutable segments.

Regression proof: complete/empty/partial/corrupt frames; wrong symbols and timestamps; exact expected 45s/90s polling windows; midnight crossing; deliberate skipped polling and heartbeat; no trading imports or writes. Run full offline pytest; commit only the C-01 tool and test files; preserve immutable archive.

## C-02 — Recovery and integrity after crash (not started)

Introduce read-only crash audit and fail-closed recovery evidence: identify clean vs abrupt shutdown, poisoned/corrupt/truncated segments, monotonic clock discontinuities, old segment verification and fresh exclusive segment on restart. Persist an external provenance manifest with Git Tree, source SHA256, file checksum, operator time sync status, and verifiable reason for each failure. Never truncate, resume, delete, infer causal truth from, or silently skip old DARC segments.

Regression proof: interrupted frame, fsync failure, duplicate writer, bad historical segment, stale or absent heartbeat, clock jump, failed gap record; require full offline pytest and separate exact-path C-02 commit.

## C-03 — Backup, restore, and storage safety (capacity/compression probe only; backup automation NOT approved)

Define an explicitly authorized local/external backup destination outside Git and outside the active DARC root; immutable SHA256-verified copies with complete manifest, backup completion markers and periodic verified restore drill. Address disk capacity/alerts, disk-full and power-loss tests, access controls and retention policy. Never prune or replace original DARC segments; never mistake a copied archive for a new market receipt.

**Measured 2026-10-12, Windows:** free C: 62,811,582,464 bytes (Disk 0, TS128GMSA720 SSD); free D: 411,945,062,400 bytes (physically distinct Disk 1, WDC WD10JPVT HDD). Raw ETHUSDT pilot archive: 15,580,678 bytes / 3600 s, equivalent to ~374 MB/day, 11.22 GB/30 days and 136.5 GB/365 days per symbol **uncompressed** (decimal GB); overhead and additional symbols not included. C: holds ~5.6 such 30-day symbol-months before reserve. Proposal only: `D:\DerivArchive-Backups` for secondary local immutable backups, plus another encrypted off-host/offsite copy; D: protects against C: disk failure but not common-host loss. No destination is yet provisioned or authorized as an operational backup job.

**Zstandard level-3 non-destructive probe:** `D:\DerivArchive-C03-Probe\probe-report.json`; isolated `pilot-copy.darc` 15,580,678 B -> `.zst` 104,570 B (99.33% smaller, 149:1); decompressed 15,580,678 B with exact original SHA256 `6fe6d97ba1ff4ac706f93ff5883135bc8ed7befd27d211dd5f46e30bfa48c9f7`. Compressed SHA256 `fe498a1c69d17d55207a88984e9fb51be35b083f01c0e930409269fca0d6a79c`. These results apply **only to this one highly compressible pilot**, never assume that ratio as the capacity budget. Originals untouched; full C-03 and CONTINUOUS remain NO-GO.

Regression proof: failed/delayed copy, checksum mismatch, missing/corrupt segment, insufficient space, interrupted copy and hash-matched restore. Full offline pytest, separate C-03 commit.

## C-04 — Safe single-instance restart and Windows boot policy (not started)

Design *but do not enable* a boot supervisor with NTP verification before startup, fixed external absolute `--archive` path, `.collector.lock` ownership, process provenance, exit-code classification, bounded exponential retry/backoff and a circuit breaker (prevent startup burst amplification of Binance rate budget). Block restarts on corrupt archives, failed time sync, unsupported dependency versions, exhausted disk and missing healthy heartbeat. Keep all collector calls public GET and research-only.

Regression proof: reboot/crash simulator, startup lock contention, time unsynced, 429/retry-after, 418 ban, network unavailable, repeated process failure, exit codes 64/73/74/75/78 and clean stop, no duplicate writes; full offline pytest, separate C-04 commit.

## Gates and sequence

Each C-0x is completed **one by one**: smallest approved change → focused offline regression test → full offline pytest (0 failed) → own named-files-only Git commit → next. Do not bundle multiple items into one commit, touch unrelated WIP, or run git reset/checkout/clean/push/merge. This plan **does not authorize executing its items**. Request explicit operational approval and required independent review for each sensitive implementation.

Continuous GO requires **all** C-01 through C-04 evidence, reproducible recovery and restore drill, compliant rate budget, actual monitored pilot with durable `clean_stop`, reliable clock verification, relevant code/tree/hash binding, then a **separate express human authorization** to deploy/enable at boot. Until then: **CONTINUOUS NO-GO**. Stage3A v8 remains its final edition; no Stage3A v9.

---

# Paper + NautilusTrader — offline acceptance and item sequence

**Status: PAPER NO-GO pending P-01 to P-05.** V8 research GO does not grant Paper/live deployment. The user authorized implementation and tests of the **offline simulated** paper path only. No external venue connection, credentials, signed requests, production OMS/Execution, Testnet/Mainnet orders or network-based broker paper endpoint. Every submitted order must remain wholly inside the NautilusTrader 1.231.0 simulator against frozen synthetic/public-shaped market input.

## Paper acceptance criteria — defined before implementation

The **Paper research GO** gate requires all of: (1) a hard, tested authenticated-I/O boundary and explicit mode/queue settings; (2) genuine deterministic offline NautilusTrader L2_MBP queue-aware engine fills and partial/unfilled cases, with validated quantities/fees; (3) temporal eligibility of observations, decisions and fills plus fail-closed stale/gap/overlap handling, with realized/unrealized equity/risk reconciliation; (4) append-only journal crash/resume/replay correctness, stable event identities and zero duplicate fills; (5) a reproducible end-to-end offline ETHUSDT fixture with independently checked journal/report totals and code/tree/fixture hashes; (6) focused red/green proof per changed item, **full offline pytest exit 0 before each named-path-only commit**, and a once-at-end independently sourced review/adjudication with zero proven blockers. A passing mock-only test, a dry-run intent or a claim of fills is not sufficient evidence. A Paper research GO is **not** permission to trade or enable any external adapter.

## Paper item order

- **P-01 (boundary/config):** verify explicit mode, no authenticated client, no external order method reachability; reject unsafe queue-aware overrides and unknown/malformed config. Negative tests must fail closed.
- **P-02 (Nautilus execution):** pin real 1.231.0 adapter, L2_MBP queue position and liquidity consumption, deterministic filled/partially-filled/unfilled fixtures; no external requests.
- **P-03 (causality/accounting):** observations, decisions and fill timestamps strictly ordered; stale/gap and overlapping inputs rejected; fees, inventory, reservations and exposure reconcile to the journal.
- **P-04 (crash/recovery):** simulate interruption and resumed replay against persisted journal; reject missing engine state when pending temporal fills exist; prevent duplicate event/fill IDs.
- **P-05 (end-to-end evidence and final review):** freeze a complete offline ETHUSDT run and independent reconciliation of positions, equity, journal, report, hashes; then run one end-of-paper independent reviewer batch and adjudication. No GO without accepted evidence.
- **P-06 (durable replay quantities):** persist an unambiguous causal link from each simulated order state to the existing intent event, recover accepted/unfilled leaves and partial-fill leaves from the journal after an injected crash, and reject unverifiable legacy quantities. Implemented in `d798b3cf5abc3b36847bba0034dbb6bda801afbe`; offline full pytest: 2,881 passed, 21 skipped, 0 failed.
- **P-07 (legacy-resume disposition):** explicitly distinguish the legacy scripted batch-test replay path from the approved native Nautilus temporal path. No runtime fix is authorized or necessary in the current Paper/Nautilus scope; see the scope proof below.

## P-07 - legacy scripted resume is outside the approved Nautilus Paper scope

The production-independent `ScriptedSimulationRunner` in `src/crypto_trader/paper/simulation.py` is an **offline deterministic test double**, not the real Nautilus L2_MBP execution engine. The only non-test use is a scripted scenario in `src/crypto_trader/audit/scenarios.py`, itself an offline audit fixture; this is not an approved broker-facing Paper runtime. The legacy `TradingSession.run(start_index=...)`/`resume_index()` batch mode can reprocess a recorded window without duplicate stable event IDs, but cannot prove exact post-crash engine queue, pending-order, or risk-peak recovery. In particular, a crash during legacy execution-stage journaling may leave a durable approved intent with incomplete order/fill evidence. Such a cursor is not evidence of safe execution resumption.

The approved **Nautilus** path is `NautilusSimulationRunner` implementing `TemporalExecutionSimulator` in `src/crypto_trader/execution/nautilus/simulation.py`, driven by `TradingSession.run()`'s `start_temporal/advance_until/submit_temporal/finish_temporal` flow. This path **rejects** nonzero `start_index` with `PaperStateError` unless engine/journal/risk peak recovery is separately verified and implemented. P-06 verifies offline **journal read-back** of persisted partial quantities after interruption; it does **not** authorize restarting an unverified native engine or submitting an order again. Legacy batch-resume deficiencies remain outside the current Nautilus Paper acceptance candidate; any future promotion requires an independently approved P-07 implementation scope and regression proof, not a silent fallback.

## Deferred P-05 fixture improvements (backlog; no silent release waiver)

These items were explicitly deferred by the owner for a **later, separately approved** research iteration; they are not claimed implemented or tested here:

- Validate fixture `schema_version` and consume declared `decision` values rather than duplicating scripted buy/hold choices in the test.
- Pass fixture `starting_equity_quote` explicitly into the session instead of relying on a matching hard-coded test default.
- Use integer timestamp arithmetic for nanosecond fixture offsets rather than `datetime.timestamp()` floating-point multiplication.
- Expand replay reconciliation to include reconstructed inventory, fees, realized/unrealized PnL, outstanding leaves and equity, not just fills and order states; retain independent journal-to-report calculations.
- Review portable fixture byte hashing across `core.autocrlf` configurations and consider a separately authorized `.gitattributes` rule if needed; never silently re-normalize the already pinned fixture.

Deferral does not change the immutable P-05 fixture SHA256, bypass verified safety invariants, or imply independent reviewer acceptance. Any reviewer-proven release blocker remains a blocker pending the standard adjudication gate.

For each P-0x: **smallest bounded fix -> targeted red/green test -> complete offline pytest -> independent named-files-only commit -> next item**. Preserve all protected WIP and immutable DARC data. If any defect or governance conflict falsifies the plan, record it rather than lowering the acceptance gate. Paper has no auto-start or broker authorization.

---

# R-03a — Durable submission / Intent provenance

## R-03a-1a — Accepted Locally بقرار المالك

**قرار المالك النهائي بتاريخ 2026-10-14: R-03a-1a = Accepted Locally** للـCommitين `b9952bc007685d812740dde94485a05832a44bdb` و`d07f7b68a2a07199740abe8639164a773d3dec93`، وشجرة التصحيح `072d8ec59688e31e4ce752fad41ccf0eb4564e0b`.

يشمل القبول Durable Attempt وAt-Most-Once POST، وSQLite WAL/FULL Genesis، وCAS وWriter Fence، وRecovery/Reconciliation، والتعرض داخل Hard Risk، ومطابقة الهوية الصريحة وربط Paper المشتق. **لا يشمل القبول إثبات منشأ Intent** أو إغلاق R-03a-1b، ولا يمنح تصريح Testnet/Live/Mainnet أو إغلاق الإصدار.

## مهام إلزامية قبل Testnet — معلقة، دون تفويض تنفيذ

- **جاهزية SQLite قبل Attempt:** في `ports.py`، فحص جاهزية SQLite FULL قبل حفظ Attempt لأي `SQLiteEventJournal`، لمنع تسجيل محاولة وهمية، بما في ذلك Journal غير مثبت التأسيس. **الحالة: منفذة ومقبولة محليًا بقرار المالك**؛ Commit الإصلاح: `2804f215193cde5d20421339557b746b1a9ebd54`.
- **كتّاب Journal ونافذة الانقطاع:** حصر الكتّاب الآخرين في Journal الإرسال، وتحليل خطر توقف الأداة بعد حفظ Attempt وقبل POST.
- **أسباب أخطاء الاختبارات:** تشديد Assertions لاختبارات collision وeconomics وdowngraded_writer وnormal_origin للتحقق من نوع الخطأ وسببه الصحيح.
- **إجراء تأسيس Journal:** توثيق إجراء المشغّل عند انقطاع عملية تأسيس Journal.
- **سبب رفض Intent القديم:** تحديث `test_r03_old_persisted_intent_without_attempt_is_abandoned_after_restart` للتحقق من سبب الرفض الزمني الحقيقي، دون الادعاء بأنه يثبت مطابقة الهوية.
- **عقود الهوية الصريحة:** توثيق وإثبات إلزامية `payload.intent_id` وصحة قيمته في Attempted وRisk V1/V2 وValidity، مع اختبار رفض البيانات غير الصالحة ضمن مرحلة التنفيذ المناسبة.

## مانع إضافي قبل Live — معلّق

- قياس أداء فحص أدلة الجلسة عند نمو Journal، وتحديد معالجة آمنة إذا ظهرت مشكلة توسع.

## البنود المفتوحة والبوابات

- **R-03a-1b — Intent Provenance Binding:** مفتوح ومانع إلزامي لـLive/Mainnet؛ يُصمم مع منتج Intents الفعلي ونموذج الثقة ومصدر الهوية الملزم. دليل الفجوة محفوظ خارج Commit التصحيح في `C:/Users/Jamal/.codex/visualizations/2026/10/14/01a13834-4016-7ee1-93cd-163af2e0817e/r03a1a-corrective-20261014/r03a1b-contract-event-remaining.patch`، SHA256 `cdc537e226bbb4e1597deb3c07cbeaa066bffa1a24046cba32070438d45fe14a`. يبقى تعديل اختبار R-03a-1b غير الملتزم به محفوظًا كما هو، دون Stash أو Staging.
- **R-03a-2:** تصميم فقط، دون تنفيذ.
- **Testnet / Live / Mainnet:** NO-GO.
- **الإصدار النهائي:** NO-GO.

هذا القسم يسجل قرار المالك والمهام المؤجلة فقط؛ لا يغير المهام السابقة ولا يبدأ تنفيذ أي مهمة معلقة. كل تنفيذ لاحق يحتاج نطاقًا معتمدًا وأدلته؛ لا Push/Merge/Deploy أو أوامر تداول بهذا التحديث.
