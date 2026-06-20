"""Paper Trading module for Vibe-Trading.

Simulates autonomous perp trading with real-time market data
while tracking performance against wallet state on EVM chains.
"""

__version__ = "0.2.0"

__all__ = [
    "WalletConnector",
    "PaperTradingEngine",
    "PaperRiskManager",
    "AutonomousPaperTrader",
    "LLMSignalEngine",
    "OrderManager",
    "SigningWallet",
    "LiveExecutor",
    "AutonomousOrchestrator",
    "AgentScorecard",
    "DarwinianWeightManager",
    "CIOAgent",
    "AutoresearchEngine",
    "CohortRegimeDetector",
    "ReflexivityEngine",
    "LiveOnChainExecutor",
    "GmxV2Executor",
    "EtherealClient",
    "CexPerpExecutor",
    "HyperliquidExecutor",
]


def __getattr__(name):
    if name == "HyperliquidExecutor":
        from paper_trading.hyperliquid_executor import HyperliquidExecutor

        return HyperliquidExecutor
    elif name == "CexPerpExecutor":
        from paper_trading.cex_perp_executor import CexPerpExecutor

        return CexPerpExecutor
    elif name == "EtherealClient":
        from paper_trading.ethereal_client import EtherealClient

        return EtherealClient
    elif name == "GmxV2Executor":
        from paper_trading.gmx_v2_executor import GmxV2Executor

        return GmxV2Executor
    elif name == "LiveOnChainExecutor":
        from paper_trading.live_onchain_executor import LiveOnChainExecutor

        return LiveOnChainExecutor
    elif name == "WalletConnector":
        from paper_trading.wallet_connector import WalletConnector

        return WalletConnector
    elif name == "PaperTradingEngine":
        from paper_trading.paper_engine import PaperTradingEngine

        return PaperTradingEngine
    elif name == "PaperRiskManager":
        from paper_trading.risk_manager import PaperRiskManager

        return PaperRiskManager
    elif name == "AutonomousPaperTrader":
        from paper_trading.autonomous_trader import AutonomousPaperTrader

        return AutonomousPaperTrader
    elif name == "LLMSignalEngine":
        from paper_trading.llm_signal_engine import LLMSignalEngine

        return LLMSignalEngine
    elif name == "OrderManager":
        from paper_trading.order_manager import OrderManager

        return OrderManager
    elif name == "SigningWallet":
        from paper_trading.signing_wallet import SigningWallet

        return SigningWallet
    elif name == "LiveExecutor":
        from paper_trading.live_executor import LiveExecutor

        return LiveExecutor
    elif name == "AutonomousOrchestrator":
        from paper_trading.autonomous_orchestrator import AutonomousOrchestrator

        return AutonomousOrchestrator
    elif name == "AgentScorecard":
        from paper_trading.agent_scorecard import AgentScorecard

        return AgentScorecard
    elif name == "DarwinianWeightManager":
        from paper_trading.darwinian_weights import DarwinianWeightManager

        return DarwinianWeightManager
    elif name == "CIOAgent":
        from paper_trading.cio_agent import CIOAgent

        return CIOAgent
    elif name == "AutoresearchEngine":
        from paper_trading.autoresearch import AutoresearchEngine

        return AutoresearchEngine
    elif name == "CohortRegimeDetector":
        from paper_trading.cohort_regime import CohortRegimeDetector

        return CohortRegimeDetector
    elif name == "ReflexivityEngine":
        from paper_trading.reflexivity_engine import ReflexivityEngine

        return ReflexivityEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
