# src/crypto_trader/main.py
import sys
import os
import logging
import argparse
from pathlib import Path
from typing import Dict, Any, Optional, TYPE_CHECKING
from dotenv import load_dotenv
from crypto_trader.crew import CryptoTraderCrew
from crypto_trader.input_security import require_https_base_url, valid_market_symbol
from datetime import datetime

if TYPE_CHECKING:
    from crypto_trader.crew_decision_gateway import CrewAdvisoryReceipt, CrewDecisionGateway

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("crypto_trader")

# Default configuration.
# The default market-data provider is the official Binance public REST API
# (https://api.binance.com), which requires no API key or other credential.
# BINANCE_API_BASE_URL may override the host; BINANCE_HTTP_TIMEOUT and
# BINANCE_HTTP_MAX_ATTEMPTS tune the request timeout and retry bound.
DEFAULT_TARGET_SYMBOL = 'ETHUSDT'

DEFAULT_CONFIG = {
    'target_symbol': DEFAULT_TARGET_SYMBOL,
    'kline_interval': '4h',
    'kline_limit': 72,
    'output_dir': 'output'
}

def setup_environment() -> bool:
    """
    Set up the environment by loading environment variables from .env file.
    
    Returns:
        bool: True if critical environment variables are present, False otherwise
    """
    project_root = Path(__file__).resolve().parent.parent.parent
    dotenv_path = project_root / ".env"
    
    if dotenv_path.exists():
        load_dotenv(dotenv_path)
        logger.info(f".env loaded from: {dotenv_path}")
    else:
        logger.warning(f".env file not found at {dotenv_path}. Ensure API keys are set in your environment.")
    
    # The market-data layer uses the official Binance public REST API and needs
    # no API key. We only sanity-check an operator-supplied base URL override.
    binance_base_url = (os.getenv("BINANCE_API_BASE_URL") or "").strip()
    if binance_base_url:
        try:
            require_https_base_url(binance_base_url)
        except ValueError:
            logger.error("BINANCE_API_BASE_URL must be a valid HTTPS endpoint (no credentials).")
            return False

    if not os.getenv("SERPER_API_KEY"):
        logger.warning("SERPER_API_KEY not found. The NewsAndSentimentResearcherAgent might not function correctly.")

    return True

def validate_inputs(config: Dict[str, Any]) -> bool:
    """
    Validate user inputs for cryptocurrency trading.
    
    Args:
        config: Dictionary containing configuration parameters
        
    Returns:
        bool: True if inputs are valid, False otherwise
    """
    # Exact uppercase ASCII symbol; reject control chars, path characters and Unicode lookalikes.
    if not valid_market_symbol(config.get('target_symbol')):
        logger.error("Invalid cryptocurrency symbol: must match ^[A-Z0-9]{5,20}$")
        return False
    
    # Validate kline interval format
    valid_intervals = ['1m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w', '1M']
    if config['kline_interval'] not in valid_intervals:
        logger.error(f"Invalid kline interval: {config['kline_interval']}. Must be one of {valid_intervals}")
        return False
    
    # Validate kline limit (Binance typically has a maximum)
    if not 1 <= config['kline_limit'] <= 1000:
        logger.error(f"Invalid kline limit: {config['kline_limit']}. Must be between 1 and 1000.")
        return False
    
    return True

def prepare_output_directory(config: Dict[str, Any]) -> Path:
    """
    Create and prepare the output directory for storing results.
    
    Args:
        config: Dictionary containing configuration parameters
        
    Returns:
        Path: Path object pointing to the output directory
    """
    project_root = Path(__file__).resolve().parent.parent.parent
    output_dir = project_root / config['output_dir']
    output_dir.mkdir(exist_ok=True)
    logger.info(f"Output directory for trading plan: {output_dir}")
    return output_dir

def save_trading_plan(content: Any, output_dir: Path, filename: str, *, exclusive: bool = False) -> Path:
    """
    Save the trading plan to a file.
    
    Args:
        content: Content of the trading plan (string or CrewOutput object)
        output_dir: Directory to save the file in
        filename: Name of the file
        
    Returns:
        Path: Path to the saved file
    """
    file_path = output_dir / filename
    if exclusive:
        # Binary write preserves the exact SHA256-referenced UTF-8 bytes on
        # Windows as well (text mode would silently translate LF to CRLF).
        if not isinstance(content, str):
            raise TypeError("Durable Crew delivery artifact must be plain text.")
        with open(file_path, 'xb') as f:
            f.write(content.encode('utf-8'))
            f.flush()
            os.fsync(f.fileno())
    else:
        with open(file_path, 'w', encoding='utf-8') as f:
            if hasattr(content, '__str__'):
                f.write(str(content))
            else:
                f.write(content)
    return file_path

def create_inputs(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create inputs dictionary for the crew with current datetime information.
    
    Args:
        config: Dictionary containing configuration parameters
        
    Returns:
        Dict: Dictionary containing all inputs for the crew
    """
    now = datetime.now()
    current_datetime_display_str = now.strftime("%Y-%m-%d %H:%M:%S %Z")
    filename_datetime_str = now.strftime("%Y%m%d_%H%M%S")
    logger.info(f"Current Datetime for this run: {current_datetime_display_str}")
    
    return {
        'target_symbol': config['target_symbol'],
        'kline_interval': config['kline_interval'],
        'kline_limit': config['kline_limit'],
        'current_datetime': current_datetime_display_str,
        'filename_datetime': filename_datetime_str
    }

def run_crew(
    inputs: Dict[str, Any], output_dir: Path,
    *, advisory_gateway: "CrewDecisionGateway | None" = None,
    return_origin_receipt: bool = False,
) -> Any:
    """
    Initialize and run the CryptoTraderCrew with the given inputs.
    
    Args:
        inputs: Dictionary containing inputs for the crew
        output_dir: Directory to save output files
        
    Returns:
        Legacy Crew output by default; an inert typed receipt only on explicit
        trusted host request, or None on an execution error.
    """
    if type(return_origin_receipt) is not bool:
        from crypto_trader.production.errors import ProductionStateError
        raise ProductionStateError("return_origin_receipt must be an explicit boolean.")
    if return_origin_receipt:
        from crypto_trader.production.errors import ProductionStateError
        from crypto_trader.crew_decision_gateway import CrewDecisionGateway
        if type(advisory_gateway) is not CrewDecisionGateway:
            raise ProductionStateError(
                "origin receipt requires an explicit trusted CrewDecisionGateway."
            )
    # The optional gateway is research-only and cannot submit an order. A host
    # delivery identity is durably reserved BEFORE the actual CrewAI kickoff.
    attempt = (None if advisory_gateway is None else
               advisory_gateway.delivery_ledger.begin(output_dir, symbol=inputs['target_symbol']))
    crypto_crew_instance = CryptoTraderCrew().crew()
    
    logger.info(f"🚀 Kicking off the CryptoTraderCrew for target symbol: {inputs['target_symbol']}...")
    try:
        result = crypto_crew_instance.kickoff(inputs=inputs)
        
        logger.info("\n\n✅ Crew execution finished!")
        if attempt is None:
            output_filename = f"trading_plan_{inputs['target_symbol']}_{inputs['filename_datetime']}.md"
            file_path = save_trading_plan(result, output_dir, output_filename)
        else:
            output_filename = f"trading_plan_{attempt.symbol}_{attempt.delivery_id}.md"
            raw_plan = str(result)
            file_path = save_trading_plan(raw_plan, output_dir, output_filename, exclusive=True)
            advisory_gateway.delivery_ledger.complete(attempt, raw_plan, file_path)
            # Advisory origin events only; NEVER prepare, admit, submit, or trade.
            issued_intent = advisory_gateway.issue(
                raw_plan, source_delivery_id=attempt.delivery_id,
            )
        logger.info(f"📝 Trading Plan saved to: {file_path}")

        if return_origin_receipt:
            from crypto_trader.crew_decision_gateway import CrewAdvisoryReceipt
            return CrewAdvisoryReceipt(
                raw_plan=raw_plan, intent=issued_intent,
                source_delivery_id=attempt.delivery_id,
            )
        return result
    except Exception as e:
        logger.error(f"An error occurred during crew execution: {e}", exc_info=True)
        return None

def parse_arguments():
    """
    Parse command line arguments.
    
    Returns:
        argparse.Namespace: Parsed arguments
    """
    parser = argparse.ArgumentParser(description="Crypto Trader - Generate trading plans using AI")
    parser.add_argument("--symbol", "-s", type=str, default=DEFAULT_CONFIG['target_symbol'],
                        help=f"Cryptocurrency trading symbol (default: {DEFAULT_CONFIG['target_symbol']})")
    parser.add_argument("--interval", "-i", type=str, default=DEFAULT_CONFIG['kline_interval'],
                        help=f"Kline interval (default: {DEFAULT_CONFIG['kline_interval']})")
    parser.add_argument("--limit", "-l", type=int, default=DEFAULT_CONFIG['kline_limit'],
                        help=f"Number of klines to fetch (default: {DEFAULT_CONFIG['kline_limit']})")
    parser.add_argument("--output", "-o", type=str, default=DEFAULT_CONFIG['output_dir'],
                        help=f"Output directory (default: {DEFAULT_CONFIG['output_dir']})")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    
    return parser.parse_args()

def run(
    *, advisory_gateway: "CrewDecisionGateway | None" = None,
    return_origin_receipt: bool = False,
):
    """Run the CLI; only a trusted host can explicitly request an inert origin receipt."""
    if type(return_origin_receipt) is not bool or (
        return_origin_receipt and advisory_gateway is None
    ):
        from crypto_trader.production.errors import ProductionStateError
        raise ProductionStateError(
            "origin receipt requires an explicit trusted advisory gateway and boolean flag."
        )
    # Parse command line arguments
    args = parse_arguments()
    
    # Set logging level based on verbosity
    if args.verbose:
        logger.setLevel(logging.DEBUG)
        logger.debug("Verbose logging enabled")
    
    # Create configuration from arguments
    config = {
        'target_symbol': args.symbol,
        'kline_interval': args.interval,
        'kline_limit': args.limit,
        'output_dir': args.output
    }
    
    # Setup environment
    if not setup_environment():
        sys.exit(1)
    
    # Validate inputs
    if not validate_inputs(config):
        sys.exit(1)
    
    # Prepare output directory
    output_dir = prepare_output_directory(config)
    
    # Create inputs for the crew
    inputs = create_inputs(config)
    
    # Preserve the legacy call contract; return a typed receipt only on explicit host opt-in.
    if return_origin_receipt:
        result = run_crew(
            inputs, output_dir, advisory_gateway=advisory_gateway,
            return_origin_receipt=True,
        )
    elif advisory_gateway is None:
        result = run_crew(inputs, output_dir)
    else:
        # Only the trusted host may supply a provenance gateway; never submit here.
        result = run_crew(inputs, output_dir, advisory_gateway=advisory_gateway)
    
    # Display summary of results
    if result is None:
        logger.error("Failed to generate trading plan.")
        sys.exit(1)

    logger.info("Trading plan generation completed successfully.")
    if return_origin_receipt:
        return result  # Inert, typed origin receipt; never an admission or submission.
    # Optional: Add a summary of the trading plan here if needed

if __name__ == "__main__":
    run()
