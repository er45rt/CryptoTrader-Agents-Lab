# src/crypto_trader/main.py
import os
import logging
import argparse
from pathlib import Path
from typing import Dict, Any, Optional
from dotenv import load_dotenv
from crypto_trader.crew import CryptoTraderCrew
from datetime import datetime

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
    if binance_base_url and not binance_base_url.startswith(("https://", "http://")):
        logger.error(
            "BINANCE_API_BASE_URL must start with http:// or https:// (got %r). "
            "Unset it to use the default official endpoint.",
            binance_base_url,
        )
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
    # Validate symbol format (basic check)
    if not isinstance(config['target_symbol'], str) or len(config['target_symbol']) < 5:
        logger.error(f"Invalid cryptocurrency symbol: {config['target_symbol']}")
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

def save_trading_plan(content: Any, output_dir: Path, filename: str) -> Path:
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
    with open(file_path, 'w', encoding='utf-8') as f:
        # Handle CrewOutput object by converting to string
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

def run_crew(inputs: Dict[str, Any], output_dir: Path) -> Optional[str]:
    """
    Initialize and run the CryptoTraderCrew with the given inputs.
    
    Args:
        inputs: Dictionary containing inputs for the crew
        output_dir: Directory to save output files
        
    Returns:
        Optional[str]: Result of the crew execution or None if an error occurred
    """
    crypto_crew_instance = CryptoTraderCrew().crew()
    
    logger.info(f"🚀 Kicking off the CryptoTraderCrew for target symbol: {inputs['target_symbol']}...")
    try:
        result = crypto_crew_instance.kickoff(inputs=inputs)
        
        logger.info("\n\n✅ Crew execution finished!")
        output_filename = f"trading_plan_{inputs['target_symbol']}_{inputs['filename_datetime']}.md"
        file_path = save_trading_plan(result, output_dir, output_filename)
        logger.info(f"📝 Trading Plan saved to: {file_path}")
        
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

def run():
    """
    Main function to run the CryptoTraderCrew.
    """
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
        return
    
    # Validate inputs
    if not validate_inputs(config):
        return
    
    # Prepare output directory
    output_dir = prepare_output_directory(config)
    
    # Create inputs for the crew
    inputs = create_inputs(config)
    
    # Run the crew
    result = run_crew(inputs, output_dir)
    
    # Display summary of results
    if result:
        logger.info("Trading plan generation completed successfully.")
        # Optional: Add a summary of the trading plan here if needed
    else:
        logger.error("Failed to generate trading plan.")

if __name__ == "__main__":
    run()
