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

1. **BinancePublicTool**: Custom tool for accessing official Binance public market data (klines, price ticker, 24hr stats) with request timeout, bounded retry with backoff, and response validation. Public/read-only endpoints only.
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

### Phase 1 migration note

The default market-data provider is the official Binance public REST API. `RAPIDAPI_KEY` and `RAPIDAPI_BINANCE_HOST` are no longer used and can be removed from any local `.env`. The tool only calls public market-data endpoints (`/api/v3/klines`, `/api/v3/ticker/price`, `/api/v3/ticker/24hr`); it never places orders and never uses authenticated trading endpoints. The LLM configuration and the Serper-based news research are unchanged.

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
no schema guardrail; the market analyst's structured payload comes from the validated Binance
tool.

## Dependencies

- crewai: Framework for multi-agent AI systems
- requests: For API interactions
- pydantic: For data validation
- python-dotenv: For environment variable management

## Production Nautilus execution boundary (Phase 4)

NautilusTrader is the execution/simulation substrate *beneath* this system, never our domain
language. It was selected on Spike A/Spike B evidence: runtime ingress viability, deterministic
queue-aware simulation, bounded partial-fill behaviour, exact static latencies, a working public
`FillModel` extension point, and 5/5 identical fresh-process semantic hashes.

- **Supported engine version:** `nautilus_trader==1.231.0`, pinned exactly. The production
  baseline is Python `>=3.12,<3.13` (owner decision, Gate 1) — the engine requires
  `>=3.12,<3.15` and Spike A/B were run on 3.12.10.
- **What we own:** the event contracts (`crypto_trader/events`), the LLM layer, the agents, the
  engine-independent execution boundary (`crypto_trader/execution/ports.py`), and all decision
  and risk semantics.
- **What Nautilus owns:** order matching, simulated venue/account state, simulated latency and
  fill mechanics, and the vendor data/instrument primitives used by the adapter.
- **Anti-corruption rule:** engine imports are confined to `crypto_trader/execution/nautilus/`.
  `crypto_trader/events`, `crypto_trader/llm`, `crypto_trader/crew.py` and `crypto_trader/tools`
  must contain zero Nautilus references, enforced by tests. Nothing engine-native ever leaves
  the adapter: observations and Phase 3 events are plain, typed, engine-independent values.
- **Queue-aware simulation guard:** queue-aware behaviour requires the tested public combination
  `queue_position=True`, `liquidity_consumption=True`, `book_type=BookType.L2_MBP`. The adapter
  fails fast (`QueueAwareConfigurationError`) when a caller requests queue-aware behaviour with
  any other configuration, because omitting the explicit L2 book type silently disables
  queue-ahead modelling.
- **Material limitation — dynamic latency is PARTIAL:** only static, separately configurable
  insert/update/cancel latencies exist in 1.231.0. There is no proven public dynamic or
  state-dependent Python latency hook, and none is claimed.
- **L2 limitations:** hidden/iceberg liquidity and true exchange queue rank are not observable
  from L2 market-by-price data. This is deterministic queue-aware simulation, not exchange
  ground truth, and it does not reconstruct the real Binance queue.
- **Upgrade policy:** the engine stays pinned. Any upgrade requires a dedicated branch, a
  Spike/regression rerun (queue, partial-fill and latency checks), boundary tests, and explicit
  owner approval. Nautilus is never auto-upgraded.
- **Pre-live gaps still unresolved:** TOCTOU can only be narrowed, never eliminated; an
  ambiguous post-submission outcome remains `UNKNOWN_PENDING_RECONCILIATION` with no blind
  retry; the priority safety lane and emergency break-glass path are not implemented
  (PRE-LIVE MANDATORY GAP); there is no recovery/reconciliation runtime, no production risk
  engine, no persistent event store, and no authenticated or live exchange connectivity.

### Execution boundary surface

```text
our application / domain            crypto_trader.execution (ports.py: engine-independent)
        |                                       |
        +--> ExecutionRequest / ExecutionObservation / typed errors
                                                |
                                  crypto_trader.execution.nautilus
                                     guard  -> version + queue-config fail-fast
                                     inbound  -> Nautilus public order/book/trade types
                                     outbound -> our observations + Phase 3 events
                                     runtime  -> start / submit / run / stop lifecycle
```

Phase 4 deliberately does **not** implement production risk, decision validity/staleness gates,
an OMS, a persistent event store, recovery/reconciliation, a safety lane, break-glass, or any
live/authenticated venue connectivity.

## Persistent event journal and replay (Phase 5)

The application keeps its **own durable history** above Nautilus. Nautilus internal state is
not the source of truth for AI decisions, trade intents, risk decisions, validity
dispositions, lifecycle audit, or correlation history.

- **Storage abstraction + local implementation:** `crypto_trader/persistence` exposes an
  engine-independent `EventJournal` protocol and a SQLite implementation using the standard
  library (`sqlite3`) in WAL mode with explicit transactions. No Redis, Kafka, or
  microservices; no pickle and no arbitrary code deserialization.
- **Append-only and idempotent:** `event_id` is the primary key, so re-delivering the same
  logical event is a no-op. Re-appending *different* content for an existing `event_id` fails
  closed (`EventIdConflictError`) rather than overwriting history.
- **Lossless payloads:** the full Phase 3 envelope JSON is stored and decoded back through the
  Phase 3 registry, so unknown event types and malformed payloads fail visibly instead of
  being guessed.
- **Indexed for the workflows we need:** correlation, event type/schema version, occurred-at
  ordering, symbol/sequence and order history.
- **Snapshots are acceleration only:** derived application state can be snapshotted, and every
  snapshot records the exact journal position it represents (last event id, ordering key, last
  sequence number). The journal remains authoritative.
- **Replay is deterministic:** the same journal (or snapshot + tail) rebuilds the same state in
  the same order; consumers are idempotent by `event_id`, so at-least-once delivery produces
  exactly one application effect. A crash between journal append and consumer application is
  recovered on restart without double-applying.

Phase 5 does not add an event bus, a distributed store, or any new domain event contract.

## Decision validity / staleness gate (Phase 6)

Deterministic validity runs **before** Hard Risk: AI/strategy decision → trade intent →
validity → (later phases: Hard Risk → execution boundary).

- **Freshness starts at the ORIGINAL market observation.** `market_event_ts` travels with the
  intent; age never resets when the model finishes or when the intent is created.
- **Dispositions (fixed vocabulary):** `ACCEPTED`, `EXPIRED` (explicit deadline passed),
  `STALE` (age from the original observation exceeded the policy), `MARKET_MOVED` (time-valid
  but the reference market moved beyond the strategy tolerance), `SUPERSEDED` (a newer intent
  for the same decision scope exists), `BACKPRESSURE_REJECTED` (bounded capacity exhausted).
  Precedence: EXPIRED → STALE → MARKET_MOVED → SUPERSEDED → BACKPRESSURE_REJECTED.
- **Explicit configuration, fail closed:** `load_policy({...})` requires `policy_id`,
  `max_age_seconds` and `max_market_move_bps` (optionally `valid_until_seconds`); missing or
  invalid configuration, unknown keys, naive timestamps, or a missing current market
  observation are all rejected rather than defaulted (a missing observation fails closed as
  `STALE` / `market_state_unavailable`).
- **Providers:** engine-independent `MarketStateProvider` with `LiveMarketStateProvider`
  (caller-supplied feed; no networking in this layer) and `ReplayMarketStateProvider`
  (deterministic frozen sequence) for replayable decisions.
- **Bounded only:** `BoundedPendingIntents` rejects with `BACKPRESSURE_REJECTED` when full and
  evicts intents whose age exceeds the policy, so a stale intent can never wait indefinitely
  and execute later.
- **Journaled:** every validity decision is appended to the Phase 5 journal as a NEW additive
  contract, `validity.disposition.recorded` (the existing Phase 3 schemas are unchanged;
  `risk.decision.recorded` is never overloaded with staleness).
- **TOCTOU is only narrowed:** local validity checks reduce the race window up to the last
  locally controlled admission point; they are not atomic with venue acceptance.

## Deterministic Hard Risk (Phase 7)

Risk is deterministic and non-LLM: the model never approves its own risk. The engine evaluates
typed inputs only and every rule carries an explicit unit.

- **Explicit units, no implicit conversions:** `*_quote` limits/measurements are
  `Decimal` amounts in the quote currency (e.g. USDT); `*_pct` values are `Decimal` percent
  (0-100); counts are integers. Every rule result records `actual`, `limit` and `unit`.
- **Configuration-driven, fail closed:** `load_limits({...})` requires every limit
  (`max_order_notional_quote`, `max_symbol_exposure_quote`, `max_gross_exposure_quote`,
  `max_net_exposure_quote`, `max_strategy_exposure_quote`, `loss_limit_quote`,
  `max_drawdown_pct`, `concentration_limit_pct`, `max_open_orders`, `max_open_positions`,
  `limits_version`) and rejects unknown keys or invalid values. No capital numbers are invented.
- **Rules:** trading halt, duplicate intent, conflicting intent, reduce-only behaviour,
  open orders/positions, order notional, symbol/strategy/gross/net exposure, loss limit,
  drawdown, concentration — evaluated in a fixed deterministic order with stable reason codes.
- **Safety semantics:** a loss-limit or drawdown breach only halts **new risk**
  (`halt_new_risk`); the engine never instructs liquidation or unwind — that policy belongs to
  the safety phase. `reduce_only_mode` blocks exposure-increasing orders.
- **Stale state rejection:** with `max_state_age_seconds` configured, an old or timestamp-less
  risk-state snapshot fails closed.
- **Journaled:** each evaluation writes the existing Phase 3 `risk.decision.recorded` outcome
  plus a NEW additive `risk.evaluation.recorded` audit (limits version, inputs, per-rule
  results, reason codes, risk actions). Existing Phase 3 schemas are unchanged.

## Recovery + reconciliation (Phase 8)

`crypto_trader/recovery` is the deterministic restart-recovery foundation that sits above the
Phase 5 journal and below nothing else: it reconciles OUR application view against an
execution/venue view without either silently overwriting the other.

- **One writer, evidence everywhere else:** our journal is the only writer of local order state;
  the venue/execution view is *evidence*, not authority. Reconciliation never silently corrects
  either side.
- **Ambiguous submissions stay ambiguous:** a submission that may have been sent but has no
  venue evidence becomes `UNKNOWN_PENDING_RECONCILIATION`. A network timeout is NEVER converted
  to failed/rejected, and there is **no blind retry**.
- **Crash-before-send is provable:** because the submission-attempt marker must be journaled
  BEFORE the send, a crash before that send resolves to `NEVER_SUBMITTED` (canceled from journal
  evidence only). A created order with no venue evidence is never left hanging.
- **Explicit adjustments only:** every correction is a `ReconciliationAdjustment` carrying its
  `source`, the `before` state, the `after` state and a `reason_code`. There is no silent state
  correction anywhere.
- **Local-ahead blocks new risk:** `LOCAL_AHEAD_OF_VENUE` is reported, not overwritten, and
  blocks new risk until evidence appears. Two conflicting venue sources are ambiguous; two
  agreeing sources are deduplicated.
- **Unknown external orders are escalated**, never adopted and never ignored (fail closed).
- **Journaled** through the existing Phase 3 `recovery.state.changed` contract
  (`detected` -> `reconciling` -> `resolved` | `failed`). Phase 8 adds **no** new event contract.

## Safety lane + kill switch + break-glass foundation (Phase 9)

`crypto_trader/safety` implements the deterministic operational safety controls. The mode is the
only state and every capability is a pure function of it:

`normal`, `reduce_only`, `halt_new_risk`, `cancel_open`, `degraded`, `recovery_required`.

- **Priority safety lane:** halt / cancel-open / kill-switch commands travel on their own lane and
  are always drained before strategy/AI work, so they never wait behind an inference backlog. The
  normal lane is bounded and fails closed with backpressure; the safety lane never drops a
  command. Commands are de-duplicated by `command_id`.
- **Kill switch:** deterministic, observable, journaled, restart-persistent and fail-closed. An
  unreadable, incomplete, unknown-version or self-inconsistent persisted state restores into
  `recovery_required` with the kill switch engaged - never into `normal`.
- **Idempotent actions:** re-requesting the current mode (or re-engaging the kill switch) is a
  no-op that produces **no** duplicate event.
- **Halt blocks new risk through the existing deterministic engine:** `risk_limits_for_safety`
  derives the Phase 7 limits from the mode (`reduce_only` -> `reduce_only_mode`;
  everything else / kill switch -> `trading_halted`). No second, parallel risk rule set.
- **Break-glass foundation only:** an engine-independent `BreakGlassPort` plus a simulated
  implementation that performs no I/O. Engaging break-glass forces `degraded` ->
  `recovery_required` and prohibits new trading until reconciliation plus an explicit operator
  return to `normal`. No venue connectivity, credentials or authenticated call exists here; the
  live break-glass path remains a **PRE-LIVE MANDATORY GAP**.
- **Journaled** through the NEW additive Phase 9 contract `safety.state.changed`. The Phase 3
  `recovery.state.changed` semantics are untouched.

## Shadow + paper trading (Phase 10)

`crypto_trader/paper` runs the real architecture with ZERO real orders, in two explicit modes.
Both run the real decision path (public market observation -> injected AI/strategy decision ->
decision validity -> deterministic hard risk -> safety gate) and journal every stage through the
existing Phase 3 contracts.

- **SHADOW:** execution stops before order placement and records what *would* have been submitted.
  `shadow_submits` additionally routes the would-be orders through the simulator (still no live
  venue).
- **PAPER:** the approved requests go to the injected simulated execution substrate and the
  resulting lifecycle/fill events are journaled. ZERO authenticated exchange trading: no
  credential, no signature, no order endpoint, no live venue client.
- **Queue-aware guard:** paper mode requires the tested queue-aware configuration
  (`queue_position=True`, `liquidity_consumption=True`, `book_type=L2_MBP`); anything else fails
  closed instead of silently degrading to unrealistic fills. The Nautilus adapter re-validates the
  same combination.
- **Engine boundary:** the session depends only on the `ExecutionSimulator` protocol. The Nautilus
  implementation lives in `execution/nautilus/simulation.py`; nothing in `paper/` imports the
  engine, and the report contains no engine types.
- **Run report:** decisions, validity outcomes (incl. stale/superseded/backpressure), risk
  outcomes, safety events, recovery events, simulated orders/fills, positions, realized/unrealized
  P&L and equity - plus the retained limitations. Deterministic `semantic_hash`, JSON export.
- **Restart + replay:** `resume_index()` derives the resume point from the journal and every id is
  derived from the session id plus the market reference, so re-processing never double-applies.
  `report_from_journal` rebuilds a recorded session's semantics from the journal.
- **Not a certification:** simulated accounting is not a performance claim and not a safety
  certification. Retained limitations: L2 cannot reveal hidden/iceberg liquidity or true queue
  rank; dynamic latency is PARTIAL; the decision pass precedes the simulated pass.

## Binance Spot Testnet execution (Phase 11)

`crypto_trader/execution/binance_testnet` is the authenticated **Spot Testnet** adapter. Scope is
testnet only: no mainnet, no futures/options/margin, no withdrawals, no real capital.

- **Testnet-only endpoints:** the base URL is validated at construction against an explicit mainnet
  blocklist; a mainnet (or any non-testnet) URL raises `MainnetForbiddenError` /
  `TestnetEndpointError`. The blocklist itself is the only place a mainnet name appears.
- **Credentials:** read from `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET` only, never
  logged, journaled, persisted, repr'd or echoed. `to_redacted_dict()` and `HttpRequest.safe_repr()`
  exist so requests can be observed without exposing the key or the signature.
- **Least privilege is verified, not assumed:** `verify_environment()` fails closed if
  withdrawals are enabled or trading is disabled, and records the testnet identity.
- **Ambiguity is never disguised:** a timeout, connection failure or 5xx on a *write* raises
  `AmbiguousOrderError` (`UNKNOWN_PENDING_RECONCILIATION`); it is never blindly retried and never
  locally marked failed. `recover_order()` adopts an order that already exists at the venue without
  re-sending it, and Phase 8 reconciliation resolves it from venue evidence.
- **Idempotency:** `newClientOrderId` is derived deterministically from the intent id, so a
  duplicate submission (including after a restart) cannot create a second venue order.
- **Rate limits and reconnects:** a weight bucket with deterministic backoff, `Retry-After`
  honoured on 429, IP bans (418) stop the run, and reads retry while writes do not.
- **Time synchronization:** an explicit clock offset with a `recvWindow` clamped to the documented
  maximum; `-1021` re-synchronizes once for reads and fails closed for writes; excessive drift
  refuses to sign.
- **Journaled:** every lifecycle/fill event is appended through the existing Phase 3 contracts, so
  testnet history lives in the same journal as paper/shadow history. No new event contract.
- **Offline vs live:** the offline regression suite is fully green with the live tests skipped.
  Live testnet tests are opt-in via `CRYPTO_TRADER_RUN_TESTNET=1` and are reported separately; a
  testnet outage is never disguised as an application PASS. **Testnet success does not authorize
  mainnet.**

## Live-readiness audit (Phase 12)

`crypto_trader/audit` is the technical live-readiness audit. It is fully offline: it injects faults
into the REAL components (journal, validity, hard risk, safety, recovery, paper session, the
testnet adapter with a scripted transport and the offline Nautilus simulator). No mainnet order, no
real capital and no withdrawal is involved.

- **Fault injection (15 scenarios):** process restart + journal replay, duplicate event delivery,
  stale AI response, backpressure exhaustion (slow AI worker), deterministic risk breach, unknown
  order result, cancel failure, simulated network interruption, partial fill during downtime,
  reconciliation mismatch, kill switch while the AI queue is loaded, shutdown/restart while halted,
  corrupt/unsupported event rejection, the full paper pipeline, and offline queue-aware Nautilus
  execution.
- **Readiness matrix (15 required items):** engine pin and Python baseline, engine-import
  confinement, domain-layer isolation, no mainnet capability, no withdrawal capability, auditor has
  no network capability, environment-only credentials, deterministic risk, and the documented
  limitations and policies (TOCTOU, dynamic-latency PARTIAL, break-glass gap, upgrade policy,
  UNKNOWN_PENDING_RECONCILIATION, testnet-≠-mainnet).
- **Mechanical verdict:** `TECHNICALLY READY FOR OWNER REVIEW` only when every required item and
  every scenario passes; otherwise `NOT READY`. Retained limitations are reported as
  *informational*, never as passes.
- **Readiness is not authorization:** a `TECHNICALLY READY` verdict does NOT authorize live
  trading. Jamal remains the only decider for any live/mainnet phase.

## Operational break-glass hardening (Phase 13)

`crypto_trader/breakglass` closes the Phase 12 PRE-LIVE MANDATORY GAP ("live break-glass path
remains a PRE-LIVE MANDATORY GAP") by turning the Phase 9 interface/simulation foundation into a
real, operator-initiated control path. Scope is unchanged: offline/scripted infrastructure and
Binance SPOT TESTNET only, no real capital, no withdrawal, no liquidation.

- **Deterministic, LLM-free ingress:** `BreakGlassControl` accepts only a validated
  `OperatorCommand`. No model may decide, approve, translate, delay or execute a break-glass action,
  and the package imports nothing from CrewAI, the LLM layer, the crew definition or the paper
  path. The operator CLI (`python -m crypto_trader.breakglass.cli`) runs in its own process, so it
  works while the AI queue/backlog is saturated.
- **Supported actions:** `halt_new_risk`, `cancel_open`, `reduce_only`, `enter_degraded`,
  `force_recovery_required`, plus the lifecycle commands `reconcile`, `acknowledge` and
  `restore_normal`. There is NO panic liquidation and no automatic market sell: the path can cancel
  open orders and apply safety policy, nothing else. `reduce_only` is a deterministic policy mode -
  no exchange-native reduce-only flag is invented for Binance Spot.
- **Fail-closed progression:** break-glass engagement moves the safety state into the requested
  mode (more restrictively only: the ladder never downgrades) and an unconfirmed venue-side action
  escalates `cancel_open` -> `degraded` -> `recovery_required`. New risk is impossible while the
  epoch is open, and a restart restores the exact restricted posture. NORMAL is never restored
  automatically: reconciliation AND an explicit operator acknowledgement are mandatory first, and
  only then does the operator restore release the kill switch.
- **Single authoritative writer + fencing:** `crypto_trader/breakglass/ownership.py` holds one
  writer lease with a monotonic fencing term. An operator break-glass action performs an explicit
  emergency takeover (`term + 1`), which invalidates the normal runtime's token so it can never
  trade concurrently with emergency control; the operator restore mints a fresh normal lease and
  invalidates the emergency one. With no lease held, no writer may trade.
- **Operator command record (auditable + idempotent):** every request is journaled once through the
  NEW additive contract `safety.operator_command.recorded` with `command_id`, `command_type`,
  `requested_at`, the operator label/identity, reason, control-channel source, previous and
  resulting safety states, `accepted`/`rejected` disposition, deterministic reason code, the
  semantic `command_digest`, the mode path, the venue disposition, the reconciliation status, the
  epoch flags and the fencing term (`correlation_id`/`causation_id` travel on the Phase 3
  envelope). Re-delivering the same `command_id` is an idempotent no-op; reusing it for different
  content is refused. No secret is ever journaled - the command model rejects credential-shaped
  text, and the key pair is only ever read from the environment by the testnet adapter.
- **Venue-side control:** `cancel_open` cancels through the Phase 11 Spot Testnet adapter after a
  pre-flight that verifies the endpoint is the documented Spot Testnet host, the clock is
  synchronized, the withdrawal capability is unavailable/disabled, and the safety state permits the
  operation. An ambiguous cancel/send outcome is NEVER retried: it becomes
  `UNKNOWN_PENDING_RECONCILIATION` and requires Phase 8 reconciliation.
- **Audit:** `crypto_trader.breakglass.audit` runs 35 offline operational/fault-injection scenarios
  and 16 checks and derives the verdict `BREAK-GLASS READY FOR OWNER REVIEW` or
  `BREAK-GLASS NOT READY` mechanically. Retained limitations (e.g. the opt-in authenticated
  testnet validation) are reported as *informational* and are never counted as passes.
- **Readiness is not authorization:** break-glass readiness does NOT authorize live or mainnet
  trading. No mainnet endpoint is configured, no real capital is used, and no withdrawal capability
  exists anywhere on this path.

## Production runtime boundary + writer fencing (Phase 14)

`crypto_trader/production` closes the two technical blockers from the Final Owner Review: there was
no production endpoint path, and the normal-writer lease plus the persisted safety state were never
consulted at a production decision boundary.

- **Explicit environment selection, production disabled by default:** `resolve_environment()`
  resolves `testnet` (the default) or `mainnet` from an explicit enum, never from a free-form host.
  `MAINNET_ENABLED` is `False`, so a production environment is refused before any client is
  constructed, any credential is read or any request is attempted - even when an authorization flag
  and a base URL are supplied. Testnet and production endpoints cannot be confused: a testnet
  environment refuses a production host and a production environment refuses a testnet host. The
  runtime hard-codes no production URL; a future, owner-authorized phase supplies one.
- **Fail-closed startup:** the runtime resolves the environment, loads the persisted safety state
  and refuses to start unless the posture is `normal` with the kill switch released and no
  break-glass epoch open; it rebuilds order state from the journal and refuses to start while
  reconciliation is unresolved; then it claims the single authoritative normal writer lease (a fresh
  fencing term) and verifies the held fence is current before serving any traffic. A missing,
  unreadable or contradictory ownership/safety state fails closed.
- **Final fence before every submission:** `prepare_intent()` runs Decision Validity and then the
  deterministic Hard Risk engine and returns a preparation that grants NO authority.
  `submit_prepared()` re-reads the authoritative journal, the persisted safety state and the current
  ownership record IMMEDIATELY before the venue call, refuses a stale writer fence, an open
  break-glass epoch, an engaged kill switch or any non-`normal` posture, and only then submits.
  There is no cached authorization window.
- **Enforced order path:** Trade Intent -> Decision Validity -> Deterministic Hard Risk -> persisted
  safety / writer-fence gate -> execution boundary -> venue adapter. Risk, sizing and execution
  authority never move into the LLM layer, and an AI/strategy decision cannot change the safety
  posture or bypass the final gate.
- **Reuse, not reimplementation:** the runtime integrates the real Phase 11 Spot Testnet execution
  port, the Phase 8 recovery/reconciliation runtime, the Phase 9 safety controller, the Phase 13
  break-glass ownership/fencing control, the Phase 5 journal, the Phase 6 validity gate and the
  Phase 7 Hard Risk engine. Phase 14 adds **no** event contract.
- **Evidence:** focused offline integration and fault tests cover lease ownership, competing and
  stale writers, corrupt/missing leases, persisted degraded and recovery-required postures, takeover
  during an in-flight decision, restart persistence, reconciliation-plus-acknowledgement gating, AI
  backpressure, ambiguity without blind retry, deterministic journal replay, cancel-open through the
  safety path and the production-disabled/withdrawal/secret invariants - plus an opt-in authenticated
  Spot Testnet run of the production runtime itself.
- **Scope:** Binance SPOT TESTNET only. No production/mainnet order, no real capital, no withdrawal
  and no credential value is involved; an ambiguous submission still becomes
  `UNKNOWN_PENDING_RECONCILIATION` and is never blindly retried. Phase 14 readiness is NOT live
  authorization: LIVE TRADING IS NOT AUTHORIZED and no mainnet order may be placed.

## License

See LICENSE file for details. 
