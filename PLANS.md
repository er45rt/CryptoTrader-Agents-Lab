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

## C-03 — Backup, restore, and storage safety (not started)

Define an explicitly authorized local/external backup destination outside Git and outside the active DARC root; immutable SHA256-verified copies with complete manifest, backup completion markers and periodic verified restore drill. Address disk capacity/alerts, disk-full and power-loss tests, access controls and retention policy. Never prune or replace original DARC segments; never mistake a copied archive for a new market receipt.

Regression proof: failed/delayed copy, checksum mismatch, missing/corrupt segment, insufficient space, interrupted copy and hash-matched restore. Full offline pytest, separate C-03 commit.

## C-04 — Safe single-instance restart and Windows boot policy (not started)

Design *but do not enable* a boot supervisor with NTP verification before startup, fixed external absolute `--archive` path, `.collector.lock` ownership, process provenance, exit-code classification, bounded exponential retry/backoff and a circuit breaker (prevent startup burst amplification of Binance rate budget). Block restarts on corrupt archives, failed time sync, unsupported dependency versions, exhausted disk and missing healthy heartbeat. Keep all collector calls public GET and research-only.

Regression proof: reboot/crash simulator, startup lock contention, time unsynced, 429/retry-after, 418 ban, network unavailable, repeated process failure, exit codes 64/73/74/75/78 and clean stop, no duplicate writes; full offline pytest, separate C-04 commit.

## Gates and sequence

Each C-0x is completed **one by one**: smallest approved change → focused offline regression test → full offline pytest (0 failed) → own named-files-only Git commit → next. Do not bundle multiple items into one commit, touch unrelated WIP, or run git reset/checkout/clean/push/merge. This plan **does not authorize executing its items**. Request explicit operational approval and required independent review for each sensitive implementation.

Continuous GO requires **all** C-01 through C-04 evidence, reproducible recovery and restore drill, compliant rate budget, actual monitored pilot with durable `clean_stop`, reliable clock verification, relevant code/tree/hash binding, then a **separate express human authorization** to deploy/enable at boot. Until then: **CONTINUOUS NO-GO**. Stage3A v8 remains its final edition; no Stage3A v9.
