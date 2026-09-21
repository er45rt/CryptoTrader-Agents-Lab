# src/crypto_trader/crew.py
import logging
from typing import Dict, List, Optional, Any
from crewai import Agent, Task, Crew, Process
from crewai.project import CrewBase, agent, crew, task

# Import existing and new tools
from .tools.binance_public_tool import BinancePublicTool
from crewai_tools import SerperDevTool, ScrapeWebsiteTool

# Centralized LLM layer (Phase 2). Only the canary agent uses it for now; the
# remaining agents keep their existing YAML-configured model until migrated.
from .llm import ANALYST_PROFILE_NAME, CANARY_PROFILE_NAME, get_llm, trading_plan_guardrail

# Configure logging
logger = logging.getLogger("crypto_trader.crew")

class ToolManager:
    """
    Manages the creation and caching of tool instances.
    This allows for better dependency injection and testing.
    """
    _instance = None
    _tools: Dict[str, Any] = {}
    
    @classmethod
    def get_instance(cls):
        """Get the singleton instance of ToolManager"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def get_binance_tool(self) -> BinancePublicTool:
        """Get or create the Binance public market data tool instance"""
        if 'binance' not in self._tools:
            try:
                self._tools['binance'] = BinancePublicTool()
                logger.debug("BinancePublicTool initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize BinancePublicTool: {e}")
                raise
        return self._tools['binance']
    
    def get_serper_tool(self) -> SerperDevTool:
        """Get or create the SerperDev search tool instance"""
        if 'serper' not in self._tools:
            try:
                self._tools['serper'] = SerperDevTool()
                logger.debug("SerperDevTool initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize SerperDevTool: {e}")
                raise
        return self._tools['serper']
    
    def get_scrape_tool(self) -> ScrapeWebsiteTool:
        """Get or create the website scraping tool instance"""
        if 'scrape' not in self._tools:
            try:
                self._tools['scrape'] = ScrapeWebsiteTool()
                logger.debug("ScrapeWebsiteTool initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize ScrapeWebsiteTool: {e}")
                raise
        return self._tools['scrape']

@CrewBase
class CryptoTraderCrew():
    """
    A crew of AI agents that collaborate to analyze cryptocurrency data,
    research market sentiment, and develop trading strategies.
    
    This crew is designed to provide a comprehensive analysis and trading plan
    for a specific cryptocurrency by combining technical analysis, news sentiment,
    and strategic planning.
    """
    agents_config = 'config/agents.yaml'
    tasks_config = 'config/tasks.yaml'
    
    def __init__(self, verbose: bool = True):
        """
        Initialize the CryptoTraderCrew.
        
        Args:
            verbose: Whether to enable verbose output from agents and tasks
        """
        self.verbose = verbose
        self.tool_manager = ToolManager.get_instance()
        logger.debug(f"CryptoTraderCrew initialized with verbose={verbose}")

    @agent
    def binance_data_analyst(self) -> Agent:
        """
        Creates an agent specialized in fetching and analyzing Binance market data.
        
        This agent is responsible for retrieving historical price data, current market
        prices, and 24-hour statistics for the specified cryptocurrency.
        
        Returns:
            Agent: The configured BinanceDataAnalyst agent
        """
        try:
            return Agent(
                config=self.agents_config['binance_data_analyst'],
                tools=[self.tool_manager.get_binance_tool()],
                verbose=self.verbose
            )
        except Exception as e:
            logger.error(f"Failed to create binance_data_analyst agent: {e}")
            raise

    @agent
    def news_and_sentiment_researcher(self) -> Agent:
        """
        Creates an agent specialized in researching news and market sentiment.
        
        This agent is responsible for finding and analyzing recent news articles,
        social media trends, and overall market sentiment related to the target
        cryptocurrency.
        
        Returns:
            Agent: The configured NewsAndSentimentResearcher agent
        """
        try:
            return Agent(
                config=self.agents_config['news_and_sentiment_researcher'],
                tools=[
                    self.tool_manager.get_serper_tool(),
                    self.tool_manager.get_scrape_tool()
                ],
                verbose=self.verbose
            )
        except Exception as e:
            logger.error(f"Failed to create news_and_sentiment_researcher agent: {e}")
            raise

    @agent
    def crypto_analyst(self) -> Agent:
        """
        Creates an agent specialized in analyzing cryptocurrency data and trends.
        
        This agent synthesizes the market data and news sentiment to produce a
        comprehensive analysis of the cryptocurrency's current state and potential
        future movements.
        
        Returns:
            Agent: The configured CryptoAnalyst agent
        """
        try:
            return Agent(
                config=self.agents_config['crypto_analyst'],
                # Phase 2 (agent 2): the analyst resolves its provider/model from the
                # centralized LLM factory instead of hardcoding it.
                llm=get_llm(ANALYST_PROFILE_NAME),
                verbose=self.verbose
            )
        except Exception as e:
            logger.error(f"Failed to create crypto_analyst agent: {e}")
            raise

    @agent
    def trading_plan_strategist(self) -> Agent:
        """
        Creates an agent specialized in developing trading strategies and plans.
        
        This agent takes the analysis from the CryptoAnalyst and formulates a
        concrete trading plan with entry/exit points, risk management strategies,
        and potential profit targets.
        
        Returns:
            Agent: The configured TradingPlanStrategist agent
        """
        try:
            return Agent(
                config=self.agents_config['trading_plan_strategist'],
                # Canary migration: the strategist resolves its provider/model from the
                # centralized LLM factory (named profile) instead of hardcoding it.
                llm=get_llm(CANARY_PROFILE_NAME),
                verbose=self.verbose
            )
        except Exception as e:
            logger.error(f"Failed to create trading_plan_strategist agent: {e}")
            raise

    @task
    def fetch_binance_klines_task(self) -> Task:
        """
        Creates a task for fetching historical kline (candlestick) data from Binance.
        
        This task retrieves historical price data for technical analysis, including
        open, high, low, close prices and volume information.
        
        Returns:
            Task: The configured fetch_binance_klines task
        """
        try:
            return Task(
                config=self.tasks_config['fetch_binance_klines_task'],
                agent=self.binance_data_analyst()
            )
        except Exception as e:
            logger.error(f"Failed to create fetch_binance_klines_task: {e}")
            raise

    @task
    def fetch_binance_price_ticker_task(self) -> Task:
        """
        Creates a task for fetching current price ticker information from Binance.
        
        This task retrieves the current market price and related information
        for the target cryptocurrency.
        
        Returns:
            Task: The configured fetch_binance_price_ticker task
        """
        try:
            return Task(
                config=self.tasks_config['fetch_binance_price_ticker_task'],
                agent=self.binance_data_analyst()
            )
        except Exception as e:
            logger.error(f"Failed to create fetch_binance_price_ticker_task: {e}")
            raise
    
    @task
    def fetch_binance_24hr_stats_task(self) -> Task:
        """
        Creates a task for fetching 24-hour market statistics from Binance.
        
        This task retrieves key market statistics for the past 24 hours, including
        price changes, trading volume, and high/low prices.
        
        Returns:
            Task: The configured fetch_binance_24hr_stats task
        """
        try:
            return Task(
                config=self.tasks_config['fetch_binance_24hr_stats_task'],
                agent=self.binance_data_analyst()
            )
        except Exception as e:
            logger.error(f"Failed to create fetch_binance_24hr_stats_task: {e}")
            raise

    @task
    def research_news_and_sentiment_task(self) -> Task:
        """
        Creates a task for researching recent news and market sentiment.
        
        This task searches for and analyzes recent news articles, social media trends,
        and market sentiment related to the target cryptocurrency.
        
        Returns:
            Task: The configured research_news_and_sentiment task
        """
        try:
            return Task(
                config=self.tasks_config['research_news_and_sentiment_task'],
                agent=self.news_and_sentiment_researcher()
            )
        except Exception as e:
            logger.error(f"Failed to create research_news_and_sentiment_task: {e}")
            raise

    @task
    def analyze_crypto_data_task(self) -> Task:
        """
        Creates a task for analyzing cryptocurrency data from multiple sources.
        
        This task synthesizes the market data, price information, and news sentiment
        to produce a comprehensive analysis of the cryptocurrency's current state and
        potential future movements.
        
        Returns:
            Task: The configured analyze_crypto_data task
        """
        try:
            return Task(
                config=self.tasks_config['analyze_crypto_data_task'],
                agent=self.crypto_analyst(),
                context=[
                    self.fetch_binance_klines_task(),
                    self.fetch_binance_price_ticker_task(),
                    self.fetch_binance_24hr_stats_task(),
                    self.research_news_and_sentiment_task()
                ]
            )
        except Exception as e:
            logger.error(f"Failed to create analyze_crypto_data_task: {e}")
            raise

    @task
    def develop_trading_plan_task(self) -> Task:
        """
        Creates a task for developing a trading plan based on the cryptocurrency analysis.
        
        This task takes the analysis from the CryptoAnalyst and formulates a concrete
        trading plan with entry/exit points, risk management strategies, and potential
        profit targets.
        
        Returns:
            Task: The configured develop_trading_plan task
        """
        try:
            return Task(
                config=self.tasks_config['develop_trading_plan_task'],
                agent=self.trading_plan_strategist(),
                context=[self.analyze_crypto_data_task()],
                # Structured-output enforcement: invalid plans get one bounded repair
                # attempt, then the task fails closed rather than passing bad fields on.
                guardrail=trading_plan_guardrail,
                guardrail_max_retries=1
            )
        except Exception as e:
            logger.error(f"Failed to create develop_trading_plan_task: {e}")
            raise

    @crew
    def crew(self) -> Crew:
        """
        Creates and configures the complete CryptoTraderCrew.
        
        This method sets up the entire crew with all agents and tasks, and configures
        the process to run tasks sequentially.
        
        Returns:
            Crew: The fully configured CryptoTraderCrew
        """
        try:
            return Crew(
                agents=[
                    self.binance_data_analyst(),
                    self.news_and_sentiment_researcher(),
                    self.crypto_analyst(),
                    self.trading_plan_strategist()
                ],
                tasks=[
                    self.fetch_binance_klines_task(),
                    self.fetch_binance_price_ticker_task(),
                    self.fetch_binance_24hr_stats_task(),
                    self.research_news_and_sentiment_task(),
                    self.analyze_crypto_data_task(),
                    self.develop_trading_plan_task()
                ],
                process=Process.sequential,
                verbose=self.verbose
            )
        except Exception as e:
            logger.error(f"Failed to create crew: {e}")
            raise

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        """
        Validates input parameters for the crew.
        
        Args:
            inputs: Dictionary containing input parameters
            
        Returns:
            bool: True if inputs are valid, False otherwise
            
        Raises:
            ValueError: If required inputs are missing or invalid
        """
        required_keys = ['target_symbol', 'kline_interval', 'kline_limit']
        
        # Check for required keys
        for key in required_keys:
            if key not in inputs:
                error_msg = f"Missing required input: {key}"
                logger.error(error_msg)
                raise ValueError(error_msg)
        
        # Validate symbol format
        if not isinstance(inputs['target_symbol'], str) or len(inputs['target_symbol']) < 5:
            error_msg = f"Invalid cryptocurrency symbol: {inputs['target_symbol']}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        # Additional validations could be added here
        
        return True
