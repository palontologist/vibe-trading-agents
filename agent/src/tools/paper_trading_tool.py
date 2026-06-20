"""Paper trading tool for Vibe-Trading agent.

Allows the AI agent to:
  - Check wallet balances on Base
  - Run paper trading simulations
  - View trading status and history
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)


class WalletBalanceTool(BaseTool):
    """Check wallet balances on Base network (read-only)."""

    name = "wallet_balance"
    description = "Check the balance of a Base wallet address. Returns ETH, USDC, and WETH balances."
    parameters = {
        "type": "object",
        "properties": {
            "wallet_address": {
                "type": "string",
                "description": "Base wallet address (e.g., 0x1EA37E2Fb76Aa396072204C90fcEF88093CEb920)",
            }
        },
        "required": ["wallet_address"],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        wallet_address = kwargs.get("wallet_address", "")
        if not wallet_address:
            return json.dumps({"status": "error", "error": "wallet_address required"})

        try:
            from paper_trading.wallet_connector import WalletConnector

            connector = WalletConnector(wallet_address)
            balances = connector.get_all_balances()

            return json.dumps(
                {
                    "status": "ok",
                    "wallet": wallet_address,
                    "balances": balances,
                    "total_value_usd_approx": connector.get_portfolio_value_usd(),
                },
                indent=2,
            )
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)})


class PaperTradingTool(BaseTool):
    """Run paper trading simulation for perp trading."""

    name = "paper_trade"
    description = (
        "Run a paper trading simulation for crypto perpetuals. "
        "Simulates trades with real-time market data and tracks performance. "
        "Requires wallet_address, symbols (comma-separated), and optional parameters."
    )
    parameters = {
        "type": "object",
        "properties": {
            "wallet_address": {
                "type": "string",
                "description": "Base wallet address",
            },
            "symbols": {
                "type": "string",
                "description": "Comma-separated trading pairs (e.g., 'BTC-USDT,ETH-USDT')",
                "default": "BTC-USDT",
            },
            "initial_cash": {
                "type": "number",
                "description": "Starting capital in USD",
                "default": 10000.0,
            },
            "leverage": {
                "type": "number",
                "description": "Leverage multiplier",
                "default": 3.0,
            },
            "interval": {
                "type": "string",
                "description": "Data interval (1m, 5m, 15m, 1H, 4H, 1D)",
                "default": "5m",
            },
            "cycles": {
                "type": "integer",
                "description": "Number of trading cycles to simulate (default: 1 for testing)",
                "default": 1,
            },
            "strategy": {
                "type": "string",
                "description": "Strategy type: 'momentum' (MA crossover), 'rsi', or 'custom'",
                "default": "momentum",
            },
        },
        "required": ["wallet_address"],
    }
    repeatable = False
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        wallet_address = kwargs.get("wallet_address", "")
        symbols = kwargs.get("symbols", "BTC-USDT")
        initial_cash = float(kwargs.get("initial_cash", 10000.0))
        leverage = float(kwargs.get("leverage", 3.0))
        interval = kwargs.get("interval", "5m")
        cycles = int(kwargs.get("cycles", 1))
        strategy = kwargs.get("strategy", "momentum")

        if not wallet_address:
            return json.dumps({"status": "error", "error": "wallet_address required"})

        try:
            from paper_trading.autonomous_trader import AutonomousPaperTrader

            # Strategy selector
            if strategy == "momentum":
                signal_fn = self._momentum_strategy
            elif strategy == "rsi":
                signal_fn = self._rsi_strategy
            else:
                signal_fn = self._momentum_strategy

            config = {
                "wallet_address": wallet_address,
                "symbols": [s.strip() for s in symbols.split(",")],
                "interval": interval,
                "initial_cash": initial_cash,
                "leverage": leverage,
                "signal_fn": signal_fn,
                "risk_config": {
                    "max_position_size": 0.3,
                    "max_total_exposure": 0.8,
                    "max_leverage": 5.0,
                    "daily_loss_limit": 5.0,
                    "max_trades_per_day": 20,
                    "min_trade_size": 10.0,
                },
                "run_dir": "./paper_runs",
            }

            trader = AutonomousPaperTrader(config)

            # Run specified number of cycles
            results = []
            for i in range(cycles):
                result = trader.run_once()
                results.append(result)

            # Get final summary
            summary = trader.engine.get_summary()

            return json.dumps(
                {
                    "status": "ok",
                    "wallet": wallet_address,
                    "cycles_run": cycles,
                    "summary": summary,
                    "last_cycle": results[-1] if results else None,
                    "log_files": {
                        "trades": str(Path("./paper_runs/trades.jsonl").absolute()),
                        "equity": str(Path("./paper_runs/equity.jsonl").absolute()),
                    },
                },
                indent=2,
                default=str,
            )

        except Exception as exc:
            logger.exception("Paper trading failed")
            return json.dumps({"status": "error", "error": str(exc)})

    @staticmethod
    def _momentum_strategy(data_map: Dict[str, pd.DataFrame]) -> Dict[str, float]:
        """Simple momentum strategy: buy above MA, sell below."""
        signals = {}
        for symbol, df in data_map.items():
            if len(df) < 20:
                continue
            ma20 = df["close"].rolling(20).mean().iloc[-1]
            current = df["close"].iloc[-1]
            if current > ma20 * 1.01:
                signals[symbol] = 0.5
            elif current < ma20 * 0.99:
                signals[symbol] = -0.5
            else:
                signals[symbol] = 0.0
        return signals

    @staticmethod
    def _rsi_strategy(data_map: Dict[str, pd.DataFrame]) -> Dict[str, float]:
        """Simple RSI strategy: oversold buy, overbought sell."""
        signals = {}
        for symbol, df in data_map.items():
            if len(df) < 15:
                continue
            delta = df["close"].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            current_rsi = rsi.iloc[-1]

            if current_rsi < 30:
                signals[symbol] = 0.5
            elif current_rsi > 70:
                signals[symbol] = -0.5
            else:
                signals[symbol] = 0.0
        return signals


class TradingStatusTool(BaseTool):
    """Check paper trading status and history."""

    name = "trading_status"
    description = (
        "View paper trading performance summary and recent trades. "
        "Shows PnL, win rate, open positions, and recent trade history."
    )
    parameters = {
        "type": "object",
        "properties": {
            "run_dir": {
                "type": "string",
                "description": "Paper trading run directory",
                "default": "./paper_runs",
            },
            "limit": {
                "type": "integer",
                "description": "Number of recent trades to show",
                "default": 10,
            },
        },
        "required": [],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        run_dir = kwargs.get("run_dir", "./paper_runs")
        limit = int(kwargs.get("limit", 10))

        try:
            run_path = Path(run_dir)
            trades_file = run_path / "trades.jsonl"
            equity_file = run_path / "equity.jsonl"

            # Read recent trades
            recent_trades = []
            if trades_file.exists():
                with open(trades_file) as f:
                    lines = f.readlines()
                    for line in lines[-limit:]:
                        recent_trades.append(json.loads(line))

            # Read latest equity
            latest_equity = None
            if equity_file.exists():
                with open(equity_file) as f:
                    lines = f.readlines()
                    if lines:
                        latest_equity = json.loads(lines[-1])

            # Calculate stats
            all_trades = []
            if trades_file.exists():
                with open(trades_file) as f:
                    for line in f:
                        all_trades.append(json.loads(line))

            open_trades = [t for t in all_trades if t.get("action") == "open"]
            close_trades = [t for t in all_trades if t.get("action") == "close"]
            pnl_list = [t.get("pnl", 0) for t in close_trades]

            total_pnl = sum(pnl_list)
            wins = len([p for p in pnl_list if p > 0])
            win_rate = (wins / len(pnl_list) * 100) if pnl_list else 0

            return json.dumps(
                {
                    "status": "ok",
                    "summary": {
                        "total_trades": len(open_trades),
                        "closed_trades": len(close_trades),
                        "total_pnl": round(total_pnl, 2),
                        "win_rate": round(win_rate, 1),
                        "avg_pnl": round(total_pnl / len(pnl_list), 2) if pnl_list else 0,
                    },
                    "latest_equity": latest_equity,
                    "recent_trades": recent_trades,
                },
                indent=2,
                default=str,
            )

        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)})
