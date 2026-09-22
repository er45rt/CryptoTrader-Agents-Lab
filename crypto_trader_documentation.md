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

## License

See LICENSE file for details.
