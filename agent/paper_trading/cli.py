"""CLI entrypoint for paper trading.

Usage:
    python -m paper_trading run --config paper_config.json
    python -m paper_trading run --wallet 0x1EA37E2Fb76Aa396072204C90fcEF88093CEb920 --symbols BTC-USDT,ETH-USDT
    python -m paper_trading status  # Show current paper trading status
    python -m paper_trading stop   # Stop running paper trading
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from paper_trading.autonomous_trader import AutonomousPaperTrader, demo_signal_function
from paper_trading.wallet_connector import WalletConnector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def load_config(path: str) -> dict:
    """Load config from JSON file."""
    with open(path) as f:
        return json.load(f)


def create_default_config(wallet_address: str, symbols: list) -> dict:
    """Create default paper trading config."""
    return {
        "wallet_address": wallet_address,
        "symbols": symbols,
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
            "min_trade_size": 10.0,
        },
        "run_dir": "./paper_runs",
    }


def cmd_run(args):
    """Run paper trading."""
    if args.config:
        config = load_config(args.config)
    else:
        wallet = args.wallet or os.getenv("WALLET_ADDRESS")
        if not wallet:
            print("Error: --wallet or WALLET_ADDRESS env var required")
            sys.exit(1)

        symbols = args.symbols.split(",") if args.symbols else ["BTC-USDT"]
        config = create_default_config(wallet, symbols)

    # Override with CLI args
    if args.interval:
        config["interval"] = args.interval
    if args.leverage:
        config["leverage"] = args.leverage
    if args.initial_cash:
        config["initial_cash"] = float(args.initial_cash)
    if args.run_dir:
        config["run_dir"] = args.run_dir

    trader = AutonomousPaperTrader(config)

    if args.once:
        result = trader.run_once()
        print(json.dumps(result, indent=2))
    else:
        interval = args.interval_seconds or 300
        trader.run(interval_seconds=interval)


def cmd_status(args):
    """Show paper trading status."""
    wallet = args.wallet or os.getenv("WALLET_ADDRESS")
    if wallet:
        connector = WalletConnector(wallet)
        print(connector.get_wallet_summary())
    else:
        print("No wallet configured")


def main():
    parser = argparse.ArgumentParser(description="Paper Trading CLI")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Run command
    run_parser = subparsers.add_parser("run", help="Run paper trading")
    run_parser.add_argument("--config", help="Path to config JSON file")
    run_parser.add_argument("--wallet", help="Wallet address (or set WALLET_ADDRESS env var)")
    run_parser.add_argument("--symbols", default="BTC-USDT", help="Comma-separated symbols")
    run_parser.add_argument("--interval", default="5m", help="Bar interval (1m/5m/15m/1H/4H/1D)")
    run_parser.add_argument("--leverage", type=float, help="Leverage (default: 3.0)")
    run_parser.add_argument("--initial-cash", type=float, help="Initial capital (default: 10000)")
    run_parser.add_argument("--interval-seconds", type=int, help="Seconds between cycles (default: 300)")
    run_parser.add_argument("--run-dir", default="./paper_runs", help="Output directory")
    run_parser.add_argument("--once", action="store_true", help="Run single cycle and exit")

    # Status command
    status_parser = subparsers.add_parser("status", help="Show wallet/trading status")
    status_parser.add_argument("--wallet", help="Wallet address")
    status_parser.add_argument(
        "--chain", default="base", help="Chain: base, polygon, arbitrum, optimism, ethereum, bsc, avalanche"
    )

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "status":
        chain = getattr(args, "chain", "base")
        wallet = args.wallet or os.getenv("WALLET_ADDRESS")
        if wallet:
            connector = WalletConnector(wallet, chain=chain)
            print(connector.get_wallet_summary())
        else:
            print("No wallet configured")
        return
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
