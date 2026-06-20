"""Growth Framework Launcher.

Entry point for running the $10 -> $10K growth strategy.

Usage:
    cd agent
    python -m paper_trading.growth.launcher

Environment variables:
    INITIAL_CASH=10       Starting capital ($)
    LEVERAGE=10           Leverage multiplier
    TARGET_EQUITY=10000   Target equity ($)
    TARGET_DAYS=10        Days to reach target
    TICK_INTERVAL=15      Seconds between crypto ticks
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from paper_trading.growth.orchestrator import GrowthOrchestrator


def main():
    """Launch the growth framework."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("growth_trader.log"),
        ],
    )

    config = {
        "initial_cash": float(os.environ.get("INITIAL_CASH", "10")),
        "leverage": float(os.environ.get("LEVERAGE", "10")),
        "target_equity": float(os.environ.get("TARGET_EQUITY", "10000")),
        "target_days": int(os.environ.get("TARGET_DAYS", "10")),
        "crypto_interval": int(os.environ.get("TICK_INTERVAL", "15")),
        "run_dir": os.environ.get("RUN_DIR", "./paper_runs/growth"),
        "strategy": {
            "min_confidence": 45,
            "min_confluence": 0.25,
            "signal_weights": {
                "sentiment": 0.25,
                "momentum": 0.25,
                "breakout": 0.20,
                "rotation": 0.15,
                "factor": 0.15,
            },
        },
        "portfolio": {
            "initial_cash": float(os.environ.get("INITIAL_CASH", "10")),
            "leverage": float(os.environ.get("LEVERAGE", "10")),
            "max_positions": 5,
            "max_position_pct": 0.80,
            "kelly_fraction": 0.25,
        },
        "risk": {
            "max_daily_loss_pct": 0.20,
            "max_drawdown_pct": 0.30,
            "max_position_loss_pct": 0.03,
            "trailing_stop_pct": 0.015,
            "max_hold_seconds": 6 * 3600,
            "max_positions": 5,
        },
    }

    orchestrator = GrowthOrchestrator(config)
    orchestrator.run()


if __name__ == "__main__":
    main()
