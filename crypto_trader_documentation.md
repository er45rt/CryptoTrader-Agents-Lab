# CryptoTrader Documentation

## Overview

CryptoTrader is an AI-driven cryptocurrency trading plan generator powered by [crewAI](https://crewai.com). The system employs multiple AI agents that collaborate to analyze cryptocurrency market data, research news and sentiment, and develop actionable trading strategies.

## Project Structure

```
crypto_trader/
├── output/            # Directory for storing generated trading plans
├── .venv/             # Python virtual environment
├── knowledge/         # Knowledge base for the project
├── src/               # Source code
│   └── crypto_trader/
│       ├── config/
│       │   ├── agents.yaml  # Agent configurations
│       │   └── tasks.yaml   # Task configurations
│       ├── tools/
│       │   └── binance_public_tool.py    # Tool for official Binance public market data
│       ├── crew.py    # Main crew definition with agents and tasks
│       ├── main.py    # Entry point and CLI interface
│       └── __init__.py
├── tests/             # Unit tests directory
├── README.md          # Project documentation
├── pyproject.toml     # Project configuration
└── .gitignore         # Git ignore file
```

## Installation and Setup

### Prerequisites
- Python 3.10 or higher (< 3.13)
- UV for dependency management

### Installation Steps
1. Install UV:
```bash
pip install uv
```

2. Clone the repository and navigate to the project directory

3. Install dependencies:
```bash
crewai install
```

4. Configure API keys:
   - Add `OPENAI_API_KEY` to `.env` file (for LLM access)
   - Add `SERPER_API_KEY` to `.env` file (for web search capabilities)
   - No Binance credentials are needed: market data comes from the official
     Binance public REST API (`https://api.binance.com`). Optionally set
     `BINANCE_API_BASE_URL`, `BINANCE_HTTP_TIMEOUT`, or `BINANCE_HTTP_MAX_ATTEMPTS`.

## Core Components

### AI Agents

The system employs four specialized AI agents:

1. **Binance Data Analyst**
   - Role: Fetch and process cryptocurrency market data from Binance
   - Tools: BinancePublicTool (official Binance public REST API, read-only)
   - LLM: centralized factory profile `deepseek_market` (DeepSeek)

2. **News and Sentiment Researcher**
   - Role: Research and analyze news and market sentiment
   - Tools: SerperDevTool, ScrapeWebsiteTool
   - LLM: centralized factory profile `deepseek_news` (DeepSeek)

3. **Crypto Analyst**
   - Role: Analyze market data and news to provide comprehensive insights
   - LLM: centralized factory profile `deepseek_analyst` (DeepSeek)

4. **Trading Plan Strategist**
   - Role: Develop actionable trading plans based on analysis
   - LLM: centralized factory profile `deepseek_canary` (DeepSeek, output schema-validated)

### Tasks

The agents collaborate on a sequence of tasks:

1. **Fetch Binance Klines Task**: Retrieve historical candlestick data
2. **Fetch Binance Price Ticker Task**: Get current price information
3. **Fetch Binance 24hr Stats Task**: Get 24-hour market statistics
4. **Research News and Sentiment Task**: Gather and analyze recent news
5. **Analyze Crypto Data Task**: Synthesize market data and news
6. **Develop Trading Plan Task**: Create an actionable trading strategy

### Tools

The project uses custom and third-party tools:

1. **BinancePublicTool**: Custom tool for accessing official Binance public market data with request timeout, bounded retry with backoff, and response validation
2. **SerperDevTool**: For web searching capabilities (from crewai_tools)
3. **ScrapeWebsiteTool**: For extracting information from web pages (from crewai_tools)

## Usage

### Basic Usage

Run the project with default configuration:

```bash
crewai run
```

This will:
1. Generate a trading plan for the default cryptocurrency (ETHUSDT)
2. Save the report to the `output/` directory

### Advanced Usage

Customize the execution with command-line arguments:

```bash
python -m src.crypto_trader.main --symbol BTCUSDT --interval 1h --limit 100 --output custom_output --verbose
```

Available parameters:
- `--symbol` or `-s`: Cryptocurrency trading symbol (default: ETHUSDT)
- `--interval` or `-i`: Kline interval (default: 4h)
- `--limit` or `-l`: Number of klines to fetch (default: 72)
- `--output` or `-o`: Output directory (default: output)
- `--verbose` or `-v`: Enable verbose logging

## Output

The system generates a comprehensive trading plan in Markdown format containing:

1. Market data analysis (price, volume, trends)
2. News and sentiment analysis
3. Support and resistance levels
4. Entry and exit points
5. Risk management strategies
6. Profit targets

The output is saved to a file named `trading_plan_{target_symbol}_{datetime}.md` in the output directory.

## Extending the Project

### Adding New Agents

1. Define the agent in `src/crypto_trader/config/agents.yaml`
2. Implement the agent class in `src/crypto_trader/crew.py`
3. Add associated tasks in `src/crypto_trader/config/tasks.yaml`

### Adding New Tools

1. Create a new tool in `src/crypto_trader/tools/`
2. Implement the tool following the crewAI BaseTool pattern
3. Add the tool to the appropriate agent in `src/crypto_trader/crew.py`

## Troubleshooting

Common issues:

1. **API Key Errors**: Ensure all required API keys are properly set in the `.env` file
2. **Dependency Issues**: Make sure UV is installed and dependencies are properly locked
3. **Rate Limiting**: Binance returns HTTP 429 with a `Retry-After` header when throttled. The tool retries within a bounded budget and otherwise reports a structured error; tune with `BINANCE_HTTP_MAX_ATTEMPTS`.

The documented default target symbol is `ETHUSDT`, matching `DEFAULT_TARGET_SYMBOL` in `src/crypto_trader/main.py` (the single source of truth). The market-data layer uses the official Binance public REST API; `RAPIDAPI_KEY` and `RAPIDAPI_BINANCE_HOST` are no longer used.

## Centralized LLM layer (Phase 2)

LLM access is centralized in `src/crypto_trader/llm/`:

- `config.py` — named, environment-driven profiles. Each agent has its own profile:
  `deepseek_canary` (Trading Plan Strategist), `deepseek_analyst` (Comprehensive Crypto
  Analyst), `deepseek_market` (Binance Market Data Analyst) and `deepseek_news` (News and
  Sentiment Researcher). All four target the official DeepSeek API
  (`deepseek/deepseek-chat`) and read `DEEPSEEK_API_KEY`; they are the **active** profiles
  (`ACTIVE_PROFILE_NAMES`). `deepseek_reasoner` is defined for future use but is deliberately
  **not active** yet — no agent, task, or runtime path references it. Credentials are never
  embedded, printed, or logged.
- `factory.py` — `get_llm(profile)` returns a CrewAI LLM wrapped with a centralized timeout,
  provider-level rate limiting, and bounded retry with exponential backoff. Failures raise a
  typed `LLMProviderError`. A `builder` argument provides dependency injection for tests.
- `console.py` — Windows console hardening. CrewAI prints error panels containing non-ASCII
  characters; on a strict cp1252 console that write can raise `UnicodeEncodeError` while
  reporting a provider failure. Provider failures are now never masked: the encoding error is
  retained as context and the original provider error is re-raised, while our own diagnostics
  stay ASCII-only. Console handlers are only relaxed for streams that cannot encode the text,
  and are restored immediately afterwards.
- `schemas.py` — a Pydantic `TradingPlan` schema plus the guardrail that validates the
  strategist's machine-readable JSON block. Invalid output triggers one bounded repair and
  then fails closed instead of passing bad fields downstream.

All four agents are migrated. The **Crypto Trading Plan Strategist** resolves its LLM through
`get_llm("deepseek_canary")` and its output is schema-validated; the **Comprehensive Crypto
Analyst** uses `get_llm("deepseek_analyst")`; the **Binance Market Data Analyst** uses
`get_llm("deepseek_market")` while keeping its Binance public tool wiring; and the **News and
Sentiment Researcher** uses `get_llm("deepseek_news")` while keeping its existing Serper
search and scrape tools. No agent carries an agent-level `llm:` setting any more. The analyst,
market and news outputs are consumed as LLM context rather than machine-parsed, so they carry
no schema guardrail.

## Dependencies

- crewai: Framework for multi-agent AI systems
- requests: For API interactions
- pydantic: For data validation
- python-dotenv: For environment variable management

## Production Nautilus execution boundary (Phase 4)

NautilusTrader sits *beneath* this system as execution/simulation infrastructure; it is never
our domain language. Selection was evidence-based (Spike A: runtime ingress and load viability;
Spike B: queue-aware simulation fidelity, partial fills, static latencies, a public `FillModel`
extension point, and deterministic repeatability).

- **Supported engine version:** `nautilus_trader==1.231.0`, pinned exactly; production baseline
  Python `>=3.12,<3.13`.
- **Ownership split:** we own the event contracts, the LLM layer, agents, the engine-independent
  execution port, and all decision/risk semantics. Nautilus owns matching, simulated
  venue/account state, simulated latency/fill mechanics and vendor data primitives.
- **Anti-corruption boundary:** `nautilus_trader` imports are confined to
  `crypto_trader/execution/nautilus/`; `crypto_trader/events`, `crypto_trader/llm`,
  `crypto_trader/crew.py` and `crypto_trader/tools` contain zero Nautilus references (test
  enforced). Domain payloads never hold engine objects.
- **Queue-aware configuration requirement:** `queue_position=True`,
  `liquidity_consumption=True`, `book_type=BookType.L2_MBP`. Any other combination requested as
  queue-aware fails fast instead of silently degrading to unrealistic fills.
- **Dynamic latency:** PARTIAL — static per-command insert/update/cancel latency only; no public
  dynamic/state-dependent Python hook in 1.231.0.
- **L2 limits:** hidden liquidity and true queue rank are not observable; no exchange-ground
  truth claim.
- **Upgrade policy:** pin, dedicated branch, Spike/regression rerun, boundary tests, explicit
  owner approval; never auto-upgrade.
- **Pre-live gaps:** TOCTOU only narrowed; `UNKNOWN_PENDING_RECONCILIATION` for ambiguous
  post-submission outcomes with no blind retry; safety lane / break-glass and
  recovery/reconciliation not implemented (PRE-LIVE MANDATORY GAP).

### Execution boundary modules

| Module | Role |
|---|---|
| `execution/ports.py` | engine-independent requests, observations, port protocol, lifecycle states |
| `execution/errors.py` | typed boundary errors (fail-closed conditions) |
| `execution/fake.py` | deterministic in-memory port proving dependency injection |
| `execution/nautilus/guard.py` | supported-version and queue-aware-configuration fail-fast guards |
| `execution/nautilus/inbound.py` | our request/market data → Nautilus public types (exact Decimals) |
| `execution/nautilus/outbound.py` | Nautilus state → our observations + Phase 3 contracts (explicit map) |
| `execution/nautilus/runtime.py` | start/submit/run/stop lifecycle; no inference in engine callbacks |

## Persistent event journal and replay (Phase 5)

`crypto_trader/persistence` provides the durable application/domain history that sits above
Nautilus and is authoritative for decisions, intents, risk/validity outcomes, lifecycle audit
and correlation history.

| Module | Role |
|---|---|
| `journal.py` | `EventJournal` protocol + `SQLiteEventJournal` (WAL, transactions, append-only, idempotent by `event_id`) |
| `snapshots.py` | `SnapshotStore` for derived state with the recorded journal position (acceleration only) |
| `replay.py` | `IdempotentConsumer` + `rebuild_state` (snapshot + deterministic ordered tail replay) |
| `errors.py` | typed storage errors (`EventIdConflictError`, `JournalSchemaError`, `SnapshotError`) |

Guarantees: lossless Phase 3 payloads decoded through the registry; indexed correlation/type/
version/ordering queries; schema-version fail-closed on unknown databases; no pickle; no
arbitrary code deserialization; no engine or LLM dependency; snapshot + replay-tail rebuild;
crash-between-append-and-apply recovery; at-least-once delivery with exactly-once effects.

## Decision validity / staleness gate (Phase 6)

`crypto_trader/validity` implements the deterministic gate that runs between a trade intent and
Hard Risk.

| Module | Role |
|---|---|
| `market_state.py` | `MarketStateProvider` protocol, `LiveMarketStateProvider`, `ReplayMarketStateProvider`, validated `MarketObservation` |
| `policy.py` | `ValidityPolicy` + `load_policy` (explicit thresholds, fail closed on missing/invalid configuration) |
| `gate.py` | `DecisionValidityGate` (pure function of its inputs) and `to_phase3_event` |
| `pending.py` | `BoundedPendingIntents` (bounded capacity, stale-intent eviction) |
| `journaling.py` | `record_validity_decision` (journals every decision, idempotent by event id) |

Additive contract: `validity.disposition.recorded` (payload: disposition, reason_code, intent_id,
policy_id, decision_age_seconds, market_event_ts, decision_completed_at, market_move_bps, notes).
Existing Phase 3 schemas are untouched. Freshness is measured from the original market
observation; replaying the same market-state sequence with the same intent yields the same
disposition. TOCTOU is narrowed, not eliminated.

## Deterministic Hard Risk (Phase 7)

`crypto_trader/risk` implements the deterministic, non-LLM risk layer that runs after validity
and before the execution boundary.

| Module | Role |
|---|---|
| `limits.py` | `RiskLimits` + `load_limits` (explicit units; missing/invalid configuration fails closed) |
| `state.py` | `RiskStateSnapshot` (quote-currency Decimals, signed positions), `PendingIntentRegistry` (duplicate/conflict protection) |
| `engine.py` | `HardRiskEngine` (13 deterministic rules, explicit units, fixed order) + Phase 3 translation |
| `journaling.py` | `record_risk_decision` (semantic outcome + detailed audit, idempotent by event id) |

Additive contract: `risk.evaluation.recorded` (limits_version, intent identity, symbol/side,
quantity/price/notional, decision, reason codes, per-rule results with units, reduce-only flag,
risk actions). The existing `risk.decision.recorded` contract carries the semantic outcome.
Breaches halt new risk only; the engine never liquidates.

## Recovery + reconciliation (Phase 8)

`crypto_trader/recovery` implements the deterministic restart-recovery and reconciliation
foundation: restore snapshot -> replay journal tail -> rebuild pending state -> reconcile against
the execution/venue view -> resolve only with evidence -> journal the outcome.

| Module | Role |
|---|---|
| `models.py` | `LocalOrderState`, `VenueOrderReport`, `ReconciliationAdjustment`, `ReconciliationOutcome` |
| `policy.py` | `ReconciliationPolicy` + `load_policy` (fail-closed, no ambiguous defaults) |
| `engine.py` | `ReconciliationEngine` (pure function of its inputs; no wall clock, no I/O, no retry) |
| `reconstruction.py` | `OrderStateReconstructor` + `reconstruct_order_states` (journal replay, JSON snapshot) |
| `journaling.py` | `record_reconciliation` (journals `recovery.state.changed`, idempotent by event id) |
| `runtime.py` | `RestartRecovery` + `VenueStateSource` / `StaticVenueStateSource` / `ReconciliationSources` |

Semantics: a submission that may have been sent with no venue evidence is
`unknown_pending_reconciliation` (never silently failed/rejected, never blindly retried); a crash
before the recorded submission attempt is `never_submitted`; a local view ahead of the venue is
`local_ahead_of_venue`; consistent duplicate reports are deduplicated while conflicting ones are
ambiguous; an unrecognised venue order is escalated. Every adjustment carries source, before
state, after state and a reason code. No new event contract is introduced - the Phase 3
`recovery.state.changed` contract is reused unchanged.

## Safety lane + kill switch + break-glass foundation (Phase 9)

`crypto_trader/safety` implements the deterministic operational safety controls that sit above the
risk layer and below the operator.

| Module | Role |
|---|---|
| `modes.py` | `SafetyMode` + capability matrix (pure function of the mode) |
| `controls.py` | `SafetyController` (deterministic transitions, kill switch, break-glass posture, fail-closed restore) |
| `lane.py` | `PrioritySafetyLane` + `LaneCommand` / `LanePriority` (safety-first, bounded normal lane) |
| `journaling.py` | `record_safety_state` / `record_safety_transitions` (no event for an idempotent no-op) |
| `breakglass.py` | `BreakGlassPort` interface + `SimulatedBreakGlass` (no I/O, no venue connectivity) |
| `integration.py` | `risk_limits_for_safety` (mode -> Phase 7 `RiskLimits`, no parallel rule set) |

Additive contract: `safety.state.changed` (mode, previous_mode, reason_code, reason, trigger,
kill_switch_engaged). Derived capabilities are deliberately NOT stored in the event, so the record
cannot drift from the policy.

Guarantees: a persisted state that is missing, incomplete, unknown-version or self-inconsistent
restores into `recovery_required` with the kill switch engaged; repeated identical requests are
idempotent no-ops; safety commands bypass the AI/strategy backlog and are never dropped; break-glass
forces `degraded` -> `recovery_required` and requires reconciliation plus an explicit operator
return to `normal`. Live break-glass remains a pre-live mandatory gap.

## Shadow + paper trading (Phase 10)

`crypto_trader/paper` runs the real architecture with ZERO real orders.

| Module | Role |
|---|---|
| `modes.py` | `TradingMode` (`shadow` / `paper`) + the tested queue-aware configuration guard |
| `config.py` | `SessionConfig` + `load_session_config` (explicit, fail-closed, secret-free) |
| `feed.py` | `MarketObservation`, `FrozenMarketDataFeed` (deterministic), `PublicMarketDataFeed` (injected public fetch) |
| `decisions.py` | `StrategyDecision` + injected `DecisionSource` (the AI layer is never embedded) |
| `session.py` | `TradingSession` (decision pipeline, journaling, resume index, simulated pass) |
| `simulation.py` | `ExecutionSimulator` protocol + `SimulationOutcome` + `ScriptedSimulationRunner` |
| `accounting.py` | `PositionLedger` (deterministic average-cost simulated accounting) |
| `report.py` | `RunReport`, `report_from_journal`, JSON export + semantic hash |

Infrastructure binding: `crypto_trader/execution/nautilus/simulation.py` (`NautilusSimulationRunner`)
runs the approved requests through the offline queue-aware simulator, re-validates the engine
version and the tested queue-aware settings, and returns OUR observations plus Phase 3 contracts.

Design note: the decision pass produces the approved requests and the simulated pass then replays
them, so simulated fills reach the ledger after the decision pass; intra-session risk feedback is a
known limitation of this foundation. Phase 10 adds no event contract. No authenticated exchange
capability, credential or live venue exists anywhere on this path.

## Binance Spot Testnet execution (Phase 11)

`crypto_trader/execution/binance_testnet` is the authenticated Spot Testnet adapter (infrastructure
layer). Scope: testnet only, spot only.

| Module | Role |
|---|---|
| `endpoints.py` | testnet-only endpoint policy + explicit mainnet blocklist |
| `credentials.py` | environment-only credentials, redacted repr/dict, no persistence |
| `signing.py` | HMAC-SHA256 query signing + log redaction helpers |
| `clock.py` | server offset, `recvWindow` clamp, drift guard |
| `transport.py` | `Transport` protocol, real requests transport, scripted double, log-safe requests |
| `rate_limit.py` | weight bucket, deterministic backoff, 429/418 handling |
| `client.py` | signed REST client (order/cancel/status/open orders/cancel-all), idempotency, ambiguity |
| `models.py` | engine-independent orders/fills/account/environment + symbol rules |
| `translate.py` | venue state -> Phase 3 contracts and recovery domain state (deterministic ids) |
| `runtime.py` | execution port + Phase 8 venue view + safety-controller integration |

Authorised test/live split: the offline suite exercises every behaviour with a scripted transport;
`test_testnet_live_integration.py` holds the opt-in live tests (`CRYPTO_TRADER_RUN_TESTNET=1`) and is
skipped by default. Live results are reported separately from the offline regression.

Phase 11 deliberately narrows the older "no authenticated capability anywhere" guards to this one
directory (`test_testnet_scope.py` proves the exemption is narrow, testnet-only and secret-safe);
those guards still cover the rest of the production tree. No event contract was added - testnet
lifecycle events reuse `order.state.changed` / `execution.fill.recorded`.

## Live-readiness audit (Phase 12)

`crypto_trader/audit` implements the technical live-readiness audit. It is offline-only and
read-only with respect to external systems: no mainnet endpoint, no real capital, no withdrawal and
no credential value is ever read.

| Module | Role |
|---|---|
| `scenarios.py` | 15 fault-injection scenarios over the real components (+ scripted transport/engine) |
| `checks.py` | static/dynamic readiness checks (pins, isolation, mainnet/withdrawal absence, docs) |
| `matrix.py` | `ReadinessItem`, `AuditReport`, the mechanical verdict and the markdown matrix |

Scenario coverage: restart + journal replay, duplicate delivery, stale AI response, backpressure
exhaustion, risk breach (halt-new-risk only), unknown order result, cancel failure, network
interruption, partial fill during downtime, reconciliation mismatch, kill switch under load,
restart while halted, corrupt/unsupported event rejection, paper pipeline end-to-end and offline
queue-aware Nautilus execution.

Verdict policy: `TECHNICALLY READY FOR OWNER REVIEW` requires every required matrix item and every
scenario to pass; retained limitations (L2 iceberg/queue-rank blindness, PARTIAL dynamic latency,
TOCTOU narrowing, live break-glass gap, withdrawal-disabled live key) are reported as
*informational* rows and never inflate the verdict. Technical readiness is NOT live authorization.

## Operational pre-live break-glass hardening (Phase 13)

`crypto_trader/breakglass` closes the Phase 12 PRE-LIVE MANDATORY GAP. The Phase 9
interface/simulation foundation is unchanged and still exists; Phase 13 adds the operational control
path beside it (offline/scripted infrastructure and Binance SPOT TESTNET only).

| Module | Role |
|---|---|
| `commands.py` | `OperatorCommand` + `OperatorCommandType`, strict parsing, credential-shaped text rejection, semantic `command_digest` |
| `ownership.py` | single-writer lease, monotonic fencing terms, explicit emergency takeover, `WriterGuard` |
| `state.py` | durable safety-state cache + deterministic journal replay + fail-closed restore |
| `epoch.py` | the break-glass epoch derived from the journal (escalation path, reconciliation, acknowledgement, closure) |
| `venue.py` | `EmergencyVenuePort` + Spot Testnet emergency control (cancel-open only, pre-flight verified) |
| `control.py` | `BreakGlassControl`: the single deterministic ingress, journaling, gates, observability |
| `cli.py` | the operator CLI (`python -m crypto_trader.breakglass.cli status\|run`) |
| `audit/` | the Phase 13 audit: 35 scenarios, 16 checks, mechanical verdict, retained limitations |

Command semantics: `halt_new_risk`, `cancel_open`, `reduce_only`, `enter_degraded`,
`force_recovery_required` are the five operational actions; `reconcile`, `acknowledge` and
`restore_normal` are the lifecycle commands that gate any return to `normal`. Every action engages
the break-glass epoch: new risk is impossible, reconciliation is mandatory and the operator
acknowledgement is mandatory. `cancel_open` cancels through the safety path; an unconfirmed venue
outcome escalates `cancel_open` -> `degraded` -> `recovery_required` and is never retried. The
safety ladder only ever escalates: a command that would be less restrictive is refused with
`downgrade_refused_monotonic_escalation`.

Ownership/fencing: exactly one writer lease exists. The runtime claims a normal lease at startup; an
operator break-glass action takes the emergency lease with `term + 1`, so the normal writer's token
is stale and its writes are refused (`StaleWriterFenceError`). Restoring normal is an explicit
takeover back to a normal lease with a fresh term, which invalidates the emergency token. With no
lease at all, no writer may trade.

Persistence: the event journal is authoritative and holds the additive
`safety.operator_command.recorded` contract plus the Phase 9 `safety.state.changed` transitions. The
`safety_state.json` cache is written only AFTER the journal append and is cross-checked against the
journal; a missing cache is rebuilt by replay, and a corrupt or contradictory cache restores
`recovery_required` with the kill switch engaged - fail closed, never silently repaired.

Verdict policy: `BREAK-GLASS READY FOR OWNER REVIEW` requires every required check and every
scenario to pass; retained limitations (the opt-in authenticated testnet validation, and the
absence of any automated liquidation) are reported separately as *informational*. Break-glass
readiness is NOT live authorization: no mainnet endpoint, no real capital and no withdrawal.

## Production runtime boundary + writer fencing (Phase 14)

`crypto_trader/production` closes the two technical blockers from the Final Owner Review: there was
no production endpoint path, and the normal-writer lease plus the persisted safety state were not
consulted at any production decision boundary.

| Module | Role |
|---|---|
| `environment.py` | explicit `testnet` / `mainnet` selection, endpoint allowlist/guard, production disabled by default, owner-authorization gate |
| `errors.py` | typed, fail-closed production-runtime errors |
| `fence.py` | the final safety + writer-fence gate: re-reads the journal, the persisted safety state and the ownership record on every call |
| `runtime.py` | `ProductionRuntime`: fail-closed startup, lease claim, and the two-step decision path (`prepare_intent` / `submit_prepared`) |

Environment selection: the default is `testnet`; an unknown value fails closed; the production
environment exists as an explicit path but `MAINNET_ENABLED` is `False`, so requesting it raises
before any client, credential or request. A testnet environment refuses a production host and a
production environment refuses a testnet host, so the two can never be confused.

Startup: resolve environment -> load persisted safety state (refuse unless `normal`, kill switch
released and no break-glass epoch open) -> rebuild order state from the journal and refuse while
reconciliation is unresolved -> claim the single authoritative normal writer lease with a fresh
fencing term -> verify the held fence -> start the execution boundary (which re-verifies the venue
endpoint, clock and least-privilege permissions).

Order path: Trade Intent -> Decision Validity -> Deterministic Hard Risk -> persisted safety /
writer-fence gate -> execution boundary -> venue adapter. The fence is re-validated immediately
before the venue call and is never cached; an emergency break-glass takeover mints the next term and
makes the running writer stale, and an AI/strategy decision can neither change the safety posture
nor bypass the gate. Phase 14 reuses the Phase 5/6/7/8/9/11/13 components and adds no event contract.

Scope: Binance SPOT TESTNET only; no production/mainnet order, no real capital, no withdrawal and no
credential value. Readiness is NOT live authorization.

## Mainnet read-only dry-run + canary guards (Phase 15)

`crypto_trader/mainnet` implements the final pre-live MAINNET READ-ONLY dry-run and the guards for a
future Controlled Live Canary. It cannot submit, cancel or replace a mainnet order, and it is NOT
live trading authorization.

| Module | Role |
|---|---|
| `credentials.py` | dedicated read-only key pair from `BINANCE_MAINNET_READONLY_API_KEY` / `BINANCE_MAINNET_READONLY_API_SECRET` only |
| `endpoints.py` | mainnet host guard plus an explicit read-only GET path allowlist and a mutation denylist |
| `permissions.py` | the strict signed `apiRestrictions` read-only contract and its fail-closed validation |
| `filters.py` | dynamic symbol filters (NOTIONAL/MIN_NOTIONAL, LOT_SIZE, PRICE_FILTER, tickSize, stepSize, status) |
| `envelope.py` | the locked pilot envelope, the spread/execution price rules, the LIMIT+FOK canary policy and the no-chasing rule |
| `nosubmit.py` | the internal deterministic HARD NO-SUBMIT gate and the mode vocabulary (no LIVE member) |
| `client.py` | authenticated read-only REST client (GET-only helper, no method parameter) |
| `faults.py` | immediate-rejection vs post-send-ambiguity fault classification and injection |
| `soak.py` | durable soak state and the twelve mechanical exit criteria |
| `dryrun.py` | the dry-run pipeline ending in the hard no-submit gate |
| `limitations.py` | the carried-forward FOK, dry-run and residual limitations |

Permission profile: `enableReading` true and `enableWithdrawals`, `enableSpotAndMarginTrading`,
`enableMargin`, `enableFutures`, `enableVanillaOptions` and `enableFixApiTrade` false, each as a real
boolean. Missing/malformed fields, unexpected writable capabilities and parsing ambiguity fail
closed, and the profile is re-validated at every would-be execution boundary.

Pipeline: market snapshot -> decision -> trade intent -> decision validity -> deterministic hard
risk -> safety/writer-fence gate -> OMS dry-run representation -> HARD NO-SUBMIT GATE, recording
`WOULD_SUBMIT` or `WOULD_REJECT` and never calling a mainnet order endpoint.

Pilot envelope (reference only, not permission to trade): capital 300 SAR, max order 50 SAR, max
open exposure 150 SAR, daily loss halt 15 SAR, max 2 open orders, BTCUSDT/ETHUSDT, SPOT only, no
margin/futures/leverage/borrowing/withdrawals. Price rules: `max_spread_bps = 30`,
`max_normal_slippage_bps = 15` (a maximum allowed normal execution deviation, not an expectation)
and `absolute_price_bound_bps = 50` (the future catastrophic execution ceiling). Entry policy:
price-bounded LIMIT + FOK only, single-use market snapshots, no chasing, no IOC/GTC fallback; the
first 10 canary trades are a review checkpoint only, and fewer than 5 completed FOK fills in a
review window is `INSUFFICIENT_EXECUTION_SAMPLE`, never an automatic widening of the 15 bps limit.

DOCUMENTED GAP: a read-only dry-run sends no orders, so empirical mainnet post-submit
reconciliation is NOT validated in this phase; mainnet reconciliation here is logic-level plus
read-only account-state validation, with actual post-submit ambiguity still covered by Binance
Testnet evidence, deterministic post-send timeout/reset fault injection and the recovery tests.
Phase 15 also narrows the Phase 12 "no mainnet capability" item to "no mainnet MUTATION capability",
with the read-only adapter exempted explicitly and its narrowness proven by `test_mainnet_scope.py`.

## License

See LICENSE file for details.
