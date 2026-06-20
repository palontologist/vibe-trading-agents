"""Autonomous paper trading loop.

Connects wallet, fetches real-time data, generates signals, and executes
paper trades in a continuous loop with risk management.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from paper_trading.wallet_connector import WalletConnector
from paper_trading.paper_engine import PaperTradingEngine
from paper_trading.risk_manager import PaperRiskManager
from backtest.loaders.ccxt_loader import DataLoader as CCXTLoader

logger = logging.getLogger(__name__)


class AutonomousPaperTrader:
    """Main autonomous paper trading orchestrator.

    Usage:
        config = {
            "wallet_address": "0x1EA37E2Fb76Aa396072204C90fcEF88093CEb920",
            "symbols": ["BTC-USDT", "ETH-USDT"],
            "interval": "5m",
            "initial_cash": 10000.0,
            "leverage": 3.0,
            "signal_fn": my_signal_function,  # Returns dict symbol -> weight
            "risk_config": {"max_position_size": 0.3, "daily_loss_limit": 5.0},
            "paper_mode": True,  # Always True for safety
        }
        trader = AutonomousPaperTrader(config)
        trader.run()  # Runs indefinitely
    """

    def __init__(self, config: dict):
        self.config = config
        self.wallet_address = config.get("wallet_address", "")
        self.symbols = config.get("symbols", ["BTC-USDT"])
        self.interval = config.get("interval", "5m")
        self.signal_fn = config.get("signal_fn")

        if not self.signal_fn:
            raise ValueError("signal_fn is required - must return dict symbol -> weight")

        # Initialize wallet connector (read-only)
        self.wallet = (
            WalletConnector(
                self.wallet_address,
                chain=config.get("chain", "base"),
            )
            if self.wallet_address
            else None
        )

        # Initialize paper trading engine
        engine_config = {
            "initial_cash": config.get("initial_cash", 10000.0),
            "leverage": config.get("leverage", 3.0),
            "wallet_address": self.wallet_address,
            "run_dir": config.get("run_dir", "./paper_runs"),
            "maker_rate": config.get("maker_rate", 0.0002),
            "taker_rate": config.get("taker_rate", 0.0005),
            "slippage": config.get("slippage", 0.0005),
            "initial_wallet_value": config.get("initial_cash", 10000.0),
        }
        self.engine = PaperTradingEngine(engine_config)

        # Initialize risk manager
        risk_config = config.get("risk_config", {})
        self.risk_manager = PaperRiskManager(risk_config)

        # Data loader
        self.loader = CCXTLoader()

        # State
        self._running = False
        self._last_prices: Dict[str, float] = {}
        self._last_update: Optional[datetime] = None

        # Setup logging
        run_dir = Path(engine_config["run_dir"])
        run_dir.mkdir(parents=True, exist_ok=True)

        # Signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        logger.info("Received signal %s, shutting down...", signum)
        self._running = False

    def fetch_current_prices(self) -> Dict[str, float]:
        """Fetch latest prices for all symbols.

        Returns:
            Dict mapping symbol -> current price.
        """
        prices = {}
        for symbol in self.symbols:
            try:
                # Use CCXT to get current ticker
                exchange = self.loader._get_exchange()
                ccxt_symbol = symbol.replace("-", "/").upper()
                ticker = exchange.fetch_ticker(ccxt_symbol)
                prices[symbol] = ticker["last"]
                logger.debug("Price %s: %.2f", symbol, prices[symbol])
            except Exception as exc:
                logger.warning("Failed to fetch price for %s: %s", symbol, exc)
                # Fallback to last known price
                if symbol in self._last_prices:
                    prices[symbol] = self._last_prices[symbol]

        self._last_prices.update(prices)
        return prices

    def fetch_recent_data(self) -> Dict[str, pd.DataFrame]:
        """Fetch recent OHLCV data for signal generation.

        Returns:
            Dict mapping symbol -> DataFrame.
        """
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=7)  # Last 7 days

        try:
            data = self.loader.fetch(
                self.symbols,
                start_date.strftime("%Y-%m-%d"),
                end_date.strftime("%Y-%m-%d"),
                interval=self.interval,
            )
            return data
        except Exception as exc:
            logger.error("Failed to fetch data: %s", exc)
            return {}

    def generate_signals(self, data_map: Dict[str, pd.DataFrame]) -> Dict[str, float]:
        """Generate trading signals using user-provided function.

        Args:
            data_map: Symbol -> OHLCV DataFrame.

        Returns:
            Dict mapping symbol -> target weight (-1.0 to 1.0).
        """
        try:
            signals = self.signal_fn(data_map)
            # Normalize signals
            total_weight = sum(abs(w) for w in signals.values())
            if total_weight > 1.0:
                signals = {s: w / total_weight for s, w in signals.items()}
            return signals
        except Exception as exc:
            logger.error("Signal generation failed: %s", exc)
            return {}

    def execute_signals(self, signals: Dict[str, float], prices: Dict[str, float]) -> List[Dict]:
        """Execute paper trades for generated signals.

        Args:
            signals: Symbol -> target weight.
            prices: Symbol -> current price.

        Returns:
            List of executed trades.
        """
        executed = []

        # Calculate current equity
        current_equity = self.engine.capital
        for sym, pos in self.engine.positions.items():
            if sym in prices:
                current_equity += pos.size * prices[sym]

        # Check daily loss limit
        allowed, reason = self.risk_manager.check_daily_loss(current_equity)
        if not allowed:
            logger.warning("Risk check failed: %s", reason)
            return []

        for symbol, target_weight in signals.items():
            if symbol not in prices:
                continue

            price = prices[symbol]
            direction = 1 if target_weight > 0 else (-1 if target_weight < 0 else 0)

            # Risk check
            allowed, reason = self.risk_manager.check_trade_allowed(
                symbol, direction, target_weight, price, current_equity, self.engine.positions
            )
            if not allowed:
                logger.info("Trade blocked for %s: %s", symbol, reason)
                continue

            # Execute paper trade
            trade = self.engine.execute_paper_trade(
                symbol=symbol,
                direction=direction,
                target_weight=target_weight,
                price=price,
                equity=current_equity,
            )

            if trade:
                executed.append(trade)
                self.risk_manager.update_after_trade(trade.get("pnl", 0.0))
                logger.info(
                    "Paper trade executed: %s %s %.4f @ %.2f",
                    trade["action"],
                    trade["symbol"],
                    trade["size"],
                    trade["price"],
                )

        return executed

    def run_cycle(self) -> Dict[str, Any]:
        """Run one complete trading cycle.

        Returns:
            Dict with cycle results.
        """
        start_time = datetime.utcnow()

        # 1. Fetch data
        logger.info("Fetching data for symbols: %s", self.symbols)
        data = self.fetch_recent_data()
        if not data:
            return {"error": "No data fetched", "timestamp": start_time.isoformat()}

        # 2. Generate signals
        signals = self.generate_signals(data)
        logger.info("Generated signals: %s", signals)

        # 3. Fetch current prices
        prices = self.fetch_current_prices()
        if not prices:
            return {"error": "No prices fetched", "timestamp": start_time.isoformat()}

        # 4. Execute trades
        trades = self.execute_signals(signals, prices)

        # 5. Log equity snapshot
        self.engine.log_equity_snapshot()

        # 6. Get summary
        summary = self.engine.get_summary()

        # 7. Get wallet info if available
        wallet_info = {}
        if self.wallet:
            try:
                wallet_info = self.wallet.get_all_balances()
            except Exception as exc:
                logger.warning("Failed to fetch wallet info: %s", exc)

        cycle_result = {
            "timestamp": start_time.isoformat(),
            "signals": signals,
            "prices": prices,
            "trades_executed": len(trades),
            "trades": trades,
            "summary": summary,
            "wallet_balances": wallet_info,
            "risk_status": self.risk_manager.get_status(),
        }

        logger.info("Cycle complete: %s", json.dumps(summary, indent=2))
        return cycle_result

    def run(self, interval_seconds: int = 300):
        """Run the autonomous trading loop.

        Args:
            interval_seconds: Seconds between cycles (default 300 = 5 minutes).
        """
        self._running = True
        logger.info("=" * 60)
        logger.info("Starting autonomous paper trading")
        logger.info("Wallet: %s", self.wallet_address or "N/A")
        logger.info("Symbols: %s", self.symbols)
        logger.info("Interval: %ds", interval_seconds)
        logger.info("Initial capital: %.2f", self.config.get("initial_cash", 10000.0))
        logger.info("=" * 60)

        cycle_count = 0
        while self._running:
            try:
                cycle_count += 1
                logger.info("--- Cycle #%d ---", cycle_count)
                result = self.run_cycle()

                # Save cycle result
                run_dir = Path(self.config.get("run_dir", "./paper_runs"))
                cycle_file = run_dir / "cycles.jsonl"
                with open(cycle_file, "a") as f:
                    f.write(json.dumps(result) + "\n")

            except Exception as exc:
                logger.error("Cycle failed: %s", exc, exc_info=True)

            # Wait for next cycle
            if self._running:
                logger.info("Sleeping for %ds...", interval_seconds)
                time.sleep(interval_seconds)

        logger.info("Autonomous trading stopped after %d cycles", cycle_count)
        self._print_final_summary()

    def _print_final_summary(self):
        """Print final trading summary."""
        summary = self.engine.get_summary()
        logger.info("=" * 60)
        logger.info("FINAL PAPER TRADING SUMMARY")
        logger.info("=" * 60)
        logger.info("Total trades: %d", summary["total_trades"])
        logger.info("Open positions: %d", summary["open_positions"])
        logger.info("Final capital: %.2f", summary["capital"])
        logger.info("Total PnL: %.2f", summary["total_pnl"])
        logger.info("Win rate: %.1f%%", summary["win_rate"])
        logger.info("Total commission: %.4f", summary["total_commission"])

        if summary["positions"]:
            logger.info("Open positions:")
            for sym, pos in summary["positions"].items():
                logger.info("  %s: %.4f (direction: %d, entry: %.2f)", sym, pos["size"], pos["direction"], pos["entry"])
        logger.info("=" * 60)

    def run_once(self) -> Dict[str, Any]:
        """Run a single cycle and return results.

        Useful for testing.
        """
        return self.run_cycle()


def demo_signal_function(data_map: Dict[str, pd.DataFrame]) -> Dict[str, float]:
    """Example signal function: simple momentum strategy.

    Buy when price is above 20-period MA, sell when below.
    """
    signals = {}
    for symbol, df in data_map.items():
        if len(df) < 20:
            continue

        ma20 = df["close"].rolling(20).mean().iloc[-1]
        current_price = df["close"].iloc[-1]

        if current_price > ma20 * 1.01:  # 1% above MA
            signals[symbol] = 0.5  # 50% long
        elif current_price < ma20 * 0.99:  # 1% below MA
            signals[symbol] = -0.5  # 50% short
        else:
            signals[symbol] = 0.0

    return signals


if __name__ == "__main__":
    # Example usage
    config = {
        "wallet_address": "0x1EA37E2Fb76Aa396072204C90fcEF88093CEb920",
        "symbols": ["BTC-USDT", "ETH-USDT"],
        "interval": "5m",
        "initial_cash": 10000.0,
        "leverage": 3.0,
        "signal_fn": demo_signal_function,
        "risk_config": {
            "max_position_size": 0.3,
            "max_total_exposure": 0.8,
            "max_leverage": 5.0,
            "daily_loss_limit": 5.0,
            "max_trades_per_day": 20,
        },
        "run_dir": "./paper_runs",
    }

    trader = AutonomousPaperTrader(config)

    # Run once for testing
    result = trader.run_once()
    print(json.dumps(result, indent=2))
