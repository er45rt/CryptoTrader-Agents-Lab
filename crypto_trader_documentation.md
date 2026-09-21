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
   - LLM: nvidia_nim/meta/llama-3.3-70b-instruct

2. **News and Sentiment Researcher**
   - Role: Research and analyze news and market sentiment
   - Tools: SerperDevTool, ScrapeWebsiteTool
   - LLM: nvidia_nim/meta/llama-3.3-70b-instruct

3. **Crypto Analyst**
   - Role: Analyze market data and news to provide comprehensive insights
   - LLM: nvidia_nim/deepseek-ai/deepseek-r1

4. **Trading Plan Strategist**
   - Role: Develop actionable trading plans based on analysis
   - LLM: nvidia_nim/deepseek-ai/deepseek-r1

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

## Dependencies

- crewai: Framework for multi-agent AI systems
- requests: For API interactions
- pydantic: For data validation
- python-dotenv: For environment variable management

## License

See LICENSE file for details.
