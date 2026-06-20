"""Entry point for python -m paper_trading.realtime_orchestrator."""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

from paper_trading.realtime_orchestrator import RealtimeAutonomousTrader, create_default_config

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    config = create_default_config()

    # Override from env
    if os.getenv("SYMBOLS"):
        config["symbols"] = [s.strip() for s in os.getenv("SYMBOLS").split(",")]
    if os.getenv("TICK_INTERVAL"):
        config["tick_interval_seconds"] = int(os.getenv("TICK_INTERVAL"))
    if os.getenv("INITIAL_CASH"):
        config["initial_cash"] = float(os.getenv("INITIAL_CASH"))
    if os.getenv("LEVERAGE"):
        config["leverage"] = float(os.getenv("LEVERAGE"))
    if os.getenv("MAX_POSITIONS"):
        config["max_positions"] = int(os.getenv("MAX_POSITIONS"))
    if os.getenv("LIVE_MODE", "").lower() in ("true", "1", "yes"):
        config["live_mode"] = True

    trader = RealtimeAutonomousTrader(config)
    trader.run()
