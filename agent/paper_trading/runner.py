"""Standalone paper trading runner.

Usage:
    cd /home/palontologist/Downloads/dev/Vibe-Trading/agent
    python -m paper_trading.runner --wallet 0x1EA37E2Fb76Aa396072204C90fcEF88093CEb920 --symbols BTC-USDT,ETH-USDT --once
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def demo_signal_function(data_map):
    """Example signal function: simple momentum strategy."""
    import pandas as pd

    signals = {}
    for symbol, df in data_map.items():
        if len(df) < 20:
            continue
        ma20 = df["close"].rolling(20).mean().iloc[-1]
        current_price = df["close"].iloc[-1]
        if current_price > ma20 * 1.01:
            signals[symbol] = 0.5
        elif current_price < ma20 * 0.99:
            signals[symbol] = -0.5
        else:
            signals[symbol] = 0.0
    return signals


def main():
    parser = argparse.ArgumentParser(description="Paper Trading Runner")
    parser.add_argument("--wallet", default=os.getenv("WALLET_ADDRESS"), help="Wallet address")
    parser.add_argument("--symbols", default="BTC-USDT", help="Comma-separated symbols")
    parser.add_argument("--interval", default="5m", help="Bar interval")
    parser.add_argument("--leverage", type=float, default=3.0, help="Leverage")
    parser.add_argument("--initial-cash", type=float, default=10000.0, help="Initial capital")
    parser.add_argument("--once", action="store_true", help="Run single cycle")
    parser.add_argument("--interval-seconds", type=int, default=300, help="Seconds between cycles")
    args = parser.parse_args()

    if not args.wallet:
        print("Error: --wallet or WALLET_ADDRESS env var required")
        sys.exit(1)

    from paper_trading.autonomous_trader import AutonomousPaperTrader

    config = {
        "wallet_address": args.wallet,
        "symbols": args.symbols.split(","),
        "interval": args.interval,
        "initial_cash": args.initial_cash,
        "leverage": args.leverage,
        "signal_fn": demo_signal_function,
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

    if args.once:
        result = trader.run_once()
        print(json.dumps(result, indent=2, default=str))
    else:
        trader.run(interval_seconds=args.interval_seconds)


if __name__ == "__main__":
    main()
