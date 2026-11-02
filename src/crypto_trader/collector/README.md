# Phase 1 collector

This package is a dedicated process for immutable raw receipt evidence. It is
never loaded by analysis agents or trading-control layers. Continuous collection
is not yet authorized. Phase 2 normalization and Phase 3 index/replay are absent.

The executable interface is `python -m crypto_trader.collector`. The safe review
mode `--check-startup` imports dependencies, waits through the guarded grace, and
exits without opening an archive or polling. Collection would require an explicit
absolute `--archive` outside Git and `--host-time-sync-acknowledged`, and separate
operational authorization. No path fallback, credentials or new HTTP client exists.

## Bootstrap policy collector-bootstrap-1

Before importing adapter, service, CrewAI or LiteLLM, the entrypoint activates a
CPython audit hook, then sets only these process-local flags:
CREWAI_DISABLE_TELEMETRY=true, OTEL_SDK_DISABLED=true,
CREWAI_DISABLE_TRACKING=true, LITELLM_LOCAL_MODEL_COST_MAP=True.
Inherited and set values of only those four flags, installed versions, interpreter,
prefixes and module origins form bootstrap provenance. Other environment values
are not recorded. Version metadata pins CrewAI 0.201.0 / LiteLLM 1.74.9; changed
versions require revalidation. Preloaded dependencies fail closed.

No external startup connection is authorized, including the provider. Only
literal 127.0.0.1 and ::1 are exempt, for Windows/internal communication.
Hostnames including localhost, other loopbacks and private networks are denied.
Any detected attempt latches failure even when dependency code swallows the error:
no collection, no healthy start, nonzero exit. If startup fails before the archive
exists, stderr carries evidence; no journal is available then.

The guard covers CPython socket.connect (including connect_ex) and
socket.getaddrinfo events, including Python-thread calls. It is not universal
network proof. Windows Proactor literal-IP ConnectEx, native DNS resolvers,
UDP and native clients may bypass it. Optional aiodns/pycares versions are recorded.
Five seconds after imports observes deferred background work; no finite interval
proves the absence of arbitrary later tasks. Flags are primary prevention.

Check-only retains the guard through dependency atexit handlers: an exit latch
registered before imports runs after them and forces exit 78 on a swallowed
attempt. Exit status is the verdict; stdout alone is insufficient. After the latch,
late daemon/finalizer activity is outside this proof. Successful service startup
disarms the hook atomically after grace; the irremovable hook then stays inert.
Startup reserves closed standard descriptors onto the null device and captures
an independent, non-inheritable stderr duplicate above fd2 for latch diagnostics.
Archive lock/open/integrity paths repeat the reservation so a closed stdio slot
cannot become an archive handle. Reservation is not atomic against concurrent
descriptor closure by other threads. Initially closed stderr produces no latch
JSON but still exits nonzero; a healthy exit closes the duplicate. The parent must
drain stderr: compact latch output can still block if the pipe is full. Service
fatal paths write no terminal diagnostic; only durable gap and nonzero exit.
Real validation uses fresh processes and the actual project venv, never bundled
Python with dependency PYTHONPATH replacement.

## Cadence and budget

Only ETHUSDT; approved raw_delivery and synchronous observer; no normalization.
Current OI and current funding: TTL 30s, cadence 45s, actual margin 15s.
Funding interval metadata and Global L/S: TTL 60s, cadence 90s, margin 30s.
Every cadence must strictly exceed actual TTL plus minimum margin 5s.
Next deadline is completion monotonic time + cadence; no catch-up or manual cache
clearing. History polling is omitted: it adds no provable prior availability.

Steady RPM = 60*(2/45+2/90) = 4. Retry-average RPM <=8.
Half-open 60s attempt bound = 2*(2*ceil(60/45)+2*ceil(60/90)) =12.
Startup is four calls / at most eight attempts, already included in that bound.
Local ceiling remains 30 RPM; steady headroom is 86.67%, at least required20%.
Intervals over24 steady RPM fail closed. No automatic restart wrapper: repeated
restarts create additional startup bursts and reset the process-local limiter.
Provider limits apply across processes; analysis agent requests are locally
separate. No HTTP calls occur in bootstrap or heartbeat.

## Lock and durability

A nonblocking OS lock on archive/.collector.lock is held for the whole session:
Windows msvcrt.LK_NBLCK locks byte0 of an O_BINARY descriptor, seeking byte0 before
lock/unlock. POSIX uses flock. A second live owner fails without polling or
archive-record writes; distinct roots are independent. The OS releases the lock on
process death, possibly after a short delay; never steal it by PID/delete lockfile.

Each session writes a fresh exclusive UUID segment, never reopens/truncates prior
records. Every bootstrap, start, attempt, outcome, observation, failure, gap,
heartbeat and clean stop is appended, userspace-flushed and fsynced before proceeding.
Unbuffered FileIO avoids retrying pending failed bytes during close; explicit flush
still precedes fsync. Before_transport must complete that ordering before transport.
Observer latency includes serialization and durable append. A central1s limit
(one eighth of adapter8s waiter budget) is a defect, never a durability exemption.

Adapter observer catches BaseException; throwing cannot veto sends or retries.
On observer journal/latency failure, try a durable gap in a fresh segment if needed,
then directly os._exit nonzero in this dedicated process. No callback return,
further request or clean stop. If even gap persistence fails, exit74 immediately.
Other fatal paths return nonzero without clean stop; normal signals finish the
current adapter call then durably record stop and release the lock.

## Physical format and recovery

Version1 frame: 8-byte DARC0001, uint64 big-endian payload length, 32-byte SHA256
of magic+length+payload, canonical UTF8 JSON, 8-byte DARCEND1. Payload cap2MiB fits
approved1MiB decoded raw bodies expanded losslessly to base64 plus metadata.
The body means exact Requests decoded content bytes, not compressed wire bytes.

The bounded physical integrity validator classifies complete_valid,
partial_trailing or corrupt; it is not a replay reader. Restart preserves all
old bytes, classifies tails and writes a new session/segment with recovery gaps.
No deletion, age pruning or destructive rotation. External backup is required
because causal raw observations are non-recoverable. Local fsync is an OS/storage
durability contract, not a guarantee against faulty hardware/power loss. Windows
has no portable directory fsync here; trusted local durable storage is required.
Filesystem aliases or hostile replacement of the archive path are outside the
single-root locking guarantee; do not use concurrent alias paths/network shares.

## Causal and health evidence

New raw_observation requires network_owner, capture_reason=None, HTTP200, valid
provenance fetched_at, retained body and durable observer identity bindings.
Additional conservative gate: result.reason=None. Clock gate: raw_observation (and
available_at) requires response.clock_quality == "http_date_window_consistent" exactly.
Any other, unknown future or missing quality writes no raw_observation and no
available_at; it persists failed_poll with reason exactly "untrusted_clock_quality"
(response evidence, including the quality, is retained) followed by collection_gap
reason "untrusted_clock_quality" carrying clock_quality and response_id when present.
Prior records are never altered; a later trusted poll recovers normally. response_id identifies physical
responses, never value equality. Cache/shared deliveries preserve original
response_id and receipt time in operational records, never mint new observations.
Every call inspects observer_diagnostics; instrumentation failure is a durable gap
even if market retrieval succeeds. Failed calls preserve structured operational
reasons and decided_at; no fabricated market values or causal fetched_at.

available_at=fetched_at means raw response receipt by this collector. It does not
prove historical row publication time (including Global L/S rows). No causal
feature replay/adoption evidence is produced here.

Every wall timestamp has same-process monotonic nanoseconds/source, never UTC or
provider time. Persist wall-minus-monotonic residuals; central2s conservative alarm
for clock discontinuity, retain smaller residuals too. HTTP Date, RTT, clock quality
and approved window [Date-2s, Date+1s+ceil(RTT)+2s] are retained unchanged.
Host NTP/time synchronization is required for trusted causal archival; operator
acknowledgement is not verification. No OS time changes, provider time endpoint or
premiumIndex.time-as-wall-clock substitution.

Heartbeat every60s records last successful receipt per metric and failure/gap state.
Missing heartbeats imply possible outage. Partial last frames and absent clean stop
retain evidence of crashes; complete frames alone cannot prove missing-end safety.

V1 = UNRESOLVED
Strict A/B adoption gate = NOT YET ELIGIBLE
Testnet = BLOCKED pending Futures Mutation Audit Hardening
