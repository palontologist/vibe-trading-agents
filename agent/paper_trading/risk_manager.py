"""Paper trading risk manager for perp positions.

Enforces position limits, daily loss limits, and leverage constraints.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class PaperRiskManager:
    """Risk management for paper trading.

    Config keys:
      - max_position_size: max notional per position (default: 0.25 * equity)
      - max_total_exposure: max total notional (default: 1.0 * equity)
      - max_leverage: max allowed leverage (default: 5.0)
      - daily_loss_limit: max daily loss % (default: 5.0)
      - max_trades_per_day: max trades (default: 50)
      - min_trade_size: minimum trade size in USD (default: 10.0)
    """

    def __init__(self, config: dict):
        self.max_position_size = config.get("max_position_size", 0.25)
        self.max_total_exposure = config.get("max_total_exposure", 1.0)
        self.max_leverage = config.get("max_leverage", 5.0)
        self.daily_loss_limit = config.get("daily_loss_limit", 5.0)
        self.max_trades_per_day = config.get("max_trades_per_day", 50)
        self.min_trade_size = config.get("min_trade_size", 10.0)

        self._daily_pnl: Dict[str, float] = {}
        self._daily_trades: Dict[str, int] = {}
        self._last_reset: Optional[datetime] = None

    def _get_today_key(self) -> str:
        return datetime.utcnow().strftime("%Y-%m-%d")

    def _reset_daily_counters(self) -> None:
        today = self._get_today_key()
        if self._last_reset is None or self._last_reset.strftime("%Y-%m-%d") != today:
            self._daily_pnl[today] = 0.0
            self._daily_trades[today] = 0
            self._last_reset = datetime.utcnow()

    def check_trade_allowed(
        self,
        symbol: str,
        direction: int,
        target_weight: float,
        price: float,
        equity: float,
        current_positions: dict,
    ) -> tuple[bool, Optional[str]]:
        """Check if a trade passes risk rules.

        Returns:
            (allowed, reason) where reason is None if allowed.
        """
        self._reset_daily_counters()
        today = self._get_today_key()

        # Check daily trade limit
        if self._daily_trades.get(today, 0) >= self.max_trades_per_day:
            return False, f"Daily trade limit reached: {self.max_trades_per_day}"

        # Check leverage
        if abs(target_weight) * self.max_leverage > self.max_leverage:
            return False, f"Leverage exceeds max: {self.max_leverage}x"

        # Calculate proposed notional
        notional = abs(target_weight) * equity

        # Check minimum trade size
        if notional < self.min_trade_size:
            return False, f"Trade size ${notional:.2f} below minimum ${self.min_trade_size}"

        # Check position size limit
        if notional > equity * self.max_position_size:
            return False, (
                f"Position size ${notional:.2f} exceeds max "
                f"${equity * self.max_position_size:.2f} ({self.max_position_size * 100:.0f}% of equity)"
            )

        # Check total exposure
        total_exposure = sum(pos.size * price for sym, pos in current_positions.items() if sym != symbol)
        total_exposure += notional
        if total_exposure > equity * self.max_total_exposure:
            return False, (f"Total exposure ${total_exposure:.2f} exceeds max ${equity * self.max_total_exposure:.2f}")

        return True, None

    def update_after_trade(self, pnl: float = 0.0) -> None:
        """Update daily counters after a trade."""
        self._reset_daily_counters()
        today = self._get_today_key()
        self._daily_trades[today] = self._daily_trades.get(today, 0) + 1
        self._daily_pnl[today] = self._daily_pnl.get(today, 0.0) + pnl

    def check_daily_loss(self, equity: float) -> tuple[bool, Optional[str]]:
        """Check if daily loss limit breached.

        Returns:
            (allowed, reason) where reason is None if allowed.
        """
        self._reset_daily_counters()
        today = self._get_today_key()
        daily_pnl = self._daily_pnl.get(today, 0.0)
        loss_pct = abs(daily_pnl) / equity * 100 if equity > 0 else 0

        if loss_pct >= self.daily_loss_limit:
            return False, (f"Daily loss limit breached: {loss_pct:.2f}% >= {self.daily_loss_limit}%")
        return True, None

    def get_status(self) -> dict:
        """Get current risk status."""
        self._reset_daily_counters()
        today = self._get_today_key()
        return {
            "daily_trades": self._daily_trades.get(today, 0),
            "daily_pnl": round(self._daily_pnl.get(today, 0.0), 2),
            "max_trades": self.max_trades_per_day,
            "daily_loss_limit": self.daily_loss_limit,
            "max_position_size_pct": self.max_position_size * 100,
            "max_total_exposure_pct": self.max_total_exposure * 100,
            "max_leverage": self.max_leverage,
        }
