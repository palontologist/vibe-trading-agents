"""Wallet Insights Tool for Vibe-Trading Agent.

Read-only wallet analysis for trading strategy recommendations.
Similar to sapience-dual-agent but integrated with vibe-trading's AI.

Security: This tool is READ-ONLY. It never stores or uses private keys.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)


class WalletInsightsTool(BaseTool):
    """Analyze wallet and provide trading strategy insights.

    Connects to Base network to read wallet state, then provides
    AI-powered trading recommendations based on:
    - Current portfolio allocation
    - Market conditions
    - Risk profile
    - Historical performance (if available)
    """

    name = "wallet_insights"
    description = (
        "Analyze a Base wallet and provide perp trading strategy recommendations. "
        "Reads wallet balances, analyzes portfolio, and suggests optimal strategies. "
        "Completely read-only - no private keys needed."
    )
    parameters = {
        "type": "object",
        "properties": {
            "wallet_address": {
                "type": "string",
                "description": "Base wallet address (e.g., 0x1EA37E2Fb76Aa396072204C90fcEF88093CEb920)",
            },
            "risk_profile": {
                "type": "string",
                "description": "Risk tolerance: conservative, moderate, aggressive",
                "default": "moderate",
            },
            "focus_assets": {
                "type": "string",
                "description": "Assets to focus on (e.g., 'BTC,ETH,SOL')",
                "default": "BTC,ETH",
            },
            "timeframe": {
                "type": "string",
                "description": "Trading timeframe: scalping, day, swing, position",
                "default": "day",
            },
        },
        "required": ["wallet_address"],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        wallet_address = kwargs.get("wallet_address", "")
        risk_profile = kwargs.get("risk_profile", "moderate")
        focus_assets = kwargs.get("focus_assets", "BTC,ETH")
        timeframe = kwargs.get("timeframe", "day")

        if not wallet_address:
            return json.dumps({"status": "error", "error": "wallet_address required"})

        try:
            from paper_trading.wallet_connector import WalletConnector

            # Read wallet state
            wallet = WalletConnector(wallet_address)
            balances = wallet.get_all_balances()
            portfolio_value = wallet.get_portfolio_value_usd()

            # Analyze portfolio
            analysis = self._analyze_portfolio(balances, portfolio_value)

            # Generate recommendations
            recommendations = self._generate_recommendations(analysis, risk_profile, focus_assets, timeframe)

            return json.dumps(
                {
                    "status": "ok",
                    "wallet": wallet_address,
                    "timestamp": datetime.utcnow().isoformat(),
                    "portfolio": {
                        "balances": balances,
                        "estimated_value_usd": round(portfolio_value, 2),
                        "allocation": analysis["allocation"],
                    },
                    "analysis": {
                        "health_score": analysis["health_score"],
                        "risk_level": analysis["risk_level"],
                        "diversification": analysis["diversification"],
                        "liquidity": analysis["liquidity"],
                    },
                    "recommendations": recommendations,
                    "suggested_strategies": self._get_strategy_templates(risk_profile, timeframe),
                },
                indent=2,
                default=str,
            )

        except Exception as exc:
            logger.exception("Wallet insights failed")
            return json.dumps({"status": "error", "error": str(exc)})

    def _analyze_portfolio(self, balances: Dict[str, float], total_value: float) -> Dict:
        """Analyze portfolio composition and health."""
        # Calculate allocation percentages
        allocation = {}
        if total_value > 0:
            eth_price = 3000  # Approximate ETH price for calculation
            for token, amount in balances.items():
                if token == "ETH" or token == "WETH":
                    value = amount * eth_price
                elif token == "USDC":
                    value = amount
                else:
                    value = amount  # Unknown tokens
                allocation[token] = {
                    "amount": round(amount, 6),
                    "value_usd_approx": round(value, 2),
                    "percentage": round(value / total_value * 100, 2) if total_value > 0 else 0,
                }

        # Health metrics
        usdc_pct = allocation.get("USDC", {}).get("percentage", 0)
        eth_pct = allocation.get("ETH", {}).get("percentage", 0) + allocation.get("WETH", {}).get("percentage", 0)

        # Health score (0-100)
        health_score = 50
        if usdc_pct > 20:  # Good cash reserve
            health_score += 20
        if eth_pct < 80:  # Not over-concentrated
            health_score += 20
        if total_value > 1000:  # Reasonable size
            health_score += 10

        # Risk level
        risk_level = "moderate"
        if eth_pct > 90:
            risk_level = "high"  # Over-concentrated
        elif usdc_pct > 80:
            risk_level = "low"  # Very conservative

        # Diversification
        diversification = "poor" if len([t for t in allocation if allocation[t]["percentage"] > 5]) < 2 else "good"

        # Liquidity
        liquidity = "high" if usdc_pct > 10 else "low"

        return {
            "allocation": allocation,
            "health_score": min(health_score, 100),
            "risk_level": risk_level,
            "diversification": diversification,
            "liquidity": liquidity,
        }

    def _generate_recommendations(
        self, analysis: Dict, risk_profile: str, focus_assets: str, timeframe: str
    ) -> List[Dict]:
        """Generate trading recommendations."""
        recommendations = []

        # Based on portfolio composition
        if analysis["liquidity"] == "low":
            recommendations.append(
                {
                    "priority": "high",
                    "type": "risk_management",
                    "message": "Low stablecoin reserves. Consider keeping 10-20% in USDC for opportunities.",
                    "action": "reduce_position_size",
                }
            )

        if analysis["diversification"] == "poor":
            recommendations.append(
                {
                    "priority": "medium",
                    "type": "diversification",
                    "message": "Portfolio is concentrated. Consider spreading across multiple assets.",
                    "action": "diversify",
                }
            )

        # Based on risk profile
        if risk_profile == "conservative":
            recommendations.append(
                {
                    "priority": "medium",
                    "type": "strategy",
                    "message": "Consider delta-neutral strategies or covered calls to generate yield.",
                    "suggested_leverage": 1.0,
                    "max_position_size": 0.15,
                }
            )
        elif risk_profile == "aggressive":
            recommendations.append(
                {
                    "priority": "medium",
                    "type": "strategy",
                    "message": "High leverage perp trading with tight stop losses. Focus on momentum.",
                    "suggested_leverage": 3.0,
                    "max_position_size": 0.4,
                }
            )
        else:  # moderate
            recommendations.append(
                {
                    "priority": "medium",
                    "type": "strategy",
                    "message": "Trend following with 2-3x leverage. Scale in/out based on momentum.",
                    "suggested_leverage": 2.0,
                    "max_position_size": 0.25,
                }
            )

        # Based on timeframe
        timeframe_recs = {
            "scalping": "Use 1m-5m charts. High frequency, small profits.",
            "day": "Use 15m-1H charts. Close positions before sleep.",
            "swing": "Use 4H-Daily charts. Hold for 2-7 days.",
            "position": "Use Daily-Weekly. Macro trends, low leverage.",
        }
        recommendations.append(
            {
                "priority": "low",
                "type": "timeframe",
                "message": timeframe_recs.get(timeframe, "Use multiple timeframes for confirmation."),
            }
        )

        return recommendations

    def _get_strategy_templates(self, risk_profile: str, timeframe: str) -> List[Dict]:
        """Get strategy templates suitable for the profile."""
        templates = []

        if risk_profile == "conservative":
            templates.extend(
                [
                    {
                        "name": "Cash & Carry Arbitrage",
                        "description": "Long spot + short perp to capture funding rate",
                        "leverage": 1.0,
                        "risk": "low",
                    },
                    {
                        "name": "Delta Neutral Yield",
                        "description": "Earn funding rates while hedging directional risk",
                        "leverage": 1.0,
                        "risk": "low",
                    },
                ]
            )
        elif risk_profile == "moderate":
            templates.extend(
                [
                    {
                        "name": "Trend Following",
                        "description": "Enter on pullbacks in established trends",
                        "leverage": 2.0,
                        "risk": "medium",
                    },
                    {
                        "name": "Breakout Trading",
                        "description": "Enter on key level breakouts with volume confirmation",
                        "leverage": 2.0,
                        "risk": "medium",
                    },
                ]
            )
        else:  # aggressive
            templates.extend(
                [
                    {
                        "name": "Momentum Scalping",
                        "description": "High frequency trades on momentum bursts",
                        "leverage": 3.0,
                        "risk": "high",
                    },
                    {
                        "name": "News/Event Trading",
                        "description": "Trade volatility around major events",
                        "leverage": 3.0,
                        "risk": "high",
                    },
                ]
            )

        return templates


class PortfolioMonitorTool(BaseTool):
    """Monitor wallet portfolio over time and track performance."""

    name = "portfolio_monitor"
    description = (
        "Monitor wallet portfolio changes and performance over time. "
        "Tracks P&L, allocation drift, and alerts on significant changes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "wallet_address": {
                "type": "string",
                "description": "Base wallet address",
            },
            "alerts": {
                "type": "boolean",
                "description": "Show active alerts and warnings",
                "default": True,
            },
        },
        "required": ["wallet_address"],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        wallet_address = kwargs.get("wallet_address", "")
        alerts = kwargs.get("alerts", True)

        if not wallet_address:
            return json.dumps({"status": "error", "error": "wallet_address required"})

        try:
            from paper_trading.wallet_connector import WalletConnector

            wallet = WalletConnector(wallet_address)
            balances = wallet.get_all_balances()
            value = wallet.get_portfolio_value_usd()

            # Generate alerts
            alert_list = []
            if alerts:
                if balances.get("USDC", 0) < 100:
                    alert_list.append(
                        {
                            "level": "warning",
                            "message": "Low USDC balance. Consider adding stablecoin reserves.",
                        }
                    )

                eth_total = balances.get("ETH", 0) + balances.get("WETH", 0)
                if eth_total * 3000 > value * 0.9:  # >90% in ETH
                    alert_list.append(
                        {
                            "level": "warning",
                            "message": "High ETH concentration. Consider diversification.",
                        }
                    )

            return json.dumps(
                {
                    "status": "ok",
                    "wallet": wallet_address,
                    "timestamp": datetime.utcnow().isoformat(),
                    "current_value_usd": round(value, 2),
                    "balances": balances,
                    "alerts": alert_list,
                    "recommendation": "Use wallet_insights tool for detailed strategy recommendations."
                    if not alert_list
                    else "Review alerts above.",
                },
                indent=2,
            )

        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)})
