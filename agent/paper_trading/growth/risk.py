"""Risk management for aggressive growth portfolio.

Enforces:
  - Trailing stops (1.5% from peak)
  - Hard stops (3% loss)
  - 6-hour maximum hold time
  - 20% daily loss limit
  - Correlation-based position limits
  - Max position count (7)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class RiskCheck:
    """Result of a risk check."""
    allowed: bool
    reason: str = ""
    action: str = ""  # "HOLD", "REDUCE", "CLOSE", "BLOCK"
    urgency: int = 0  # 0=info, 50=warning, 100=critical


class GrowthRiskManager:
    """Risk manager for maximum-aggression growth portfolio.

    Risk Budget:
      - Max daily loss: 20% of equity
      - Max drawdown: 30% (circuit breaker)
      - Max single position loss: 3%
      - Max hold time: 6 hours
      - Trailing stop: 1.5%
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}

        # Risk limits
        self.max_daily_loss_pct = self.config.get("max_daily_loss_pct", 0.20)
        self.max_drawdown_pct = self.config.get("max_drawdown_pct", 0.30)
        self.max_position_loss_pct = self.config.get("max_position_loss_pct", 0.03)
        self.trailing_stop_pct = self.config.get("trailing_stop_pct", 0.015)
        self.max_hold_seconds = self.config.get("max_hold_seconds", 6 * 3600)  # 6 hours
        self.max_positions = self.config.get("max_positions", 7)

        # Correlation limits
        self.max_correlated_positions = self.config.get("max_correlated_positions", 3)

        # State
        self._daily_loss = 0.0
        self._daily_start_equity = 0.0
        self._circuit_breaker = False
        self._halt_until = 0.0

        # Correlation groups
        self.correlation_groups = {
            "btc_group": {"BTC", "ETH"},
            "alt_l1": {"SOL", "NEAR", "SUI"},
            "defi": {"AAVE", "LINK", "RENDER"},
            "meme": {"DOGE", "WIF", "JUP"},
            "commodity": {"XAU", "XAG"},
        }

    def reset_daily(self, equity: float) -> None:
        """Reset daily risk tracking."""
        self._daily_loss = 0.0
        self._daily_start_equity = equity
        self._circuit_breaker = False

    def check_pre_trade(
        self,
        symbol: str,
        side: str,
        notional: float,
        equity: float,
        open_positions: Dict[str, Any],
    ) -> RiskCheck:
        """Pre-trade risk check."""
        # Circuit breaker
        if self._circuit_breaker:
            return RiskCheck(False, "Circuit breaker active", "BLOCK", 100)

        # Halt check
        if time.time() < self._halt_until:
            return RiskCheck(False, "Trading halted", "BLOCK", 100)

        # Position count
        if len(open_positions) >= self.max_positions:
            return RiskCheck(False, f"Max positions ({self.max_positions})", "BLOCK", 80)

        # Already in this asset
        if symbol in open_positions:
            return RiskCheck(False, f"Already positioned in {symbol}", "BLOCK", 70)

        # Daily loss limit
        if self._daily_start_equity > 0:
            daily_loss_pct = abs(min(0, self._daily_loss)) / self._daily_start_equity
            if daily_loss_pct >= self.max_daily_loss_pct:
                return RiskCheck(False, f"Daily loss limit ({daily_loss_pct*100:.1f}%)", "HALT", 100)

        # Correlation check
        group_count = self._count_correlated(symbol, open_positions)
        if group_count >= self.max_correlated_positions:
            return RiskCheck(
                False,
                f"Too many correlated positions ({group_count} in same group)",
                "BLOCK",
                60,
            )

        # Position size check
        position_pct = notional / equity if equity > 0 else 1.0
        if position_pct > 0.90:
            return RiskCheck(False, f"Position too large ({position_pct*100:.1f}%)", "BLOCK", 70)

        return RiskCheck(True, "Approved", "ALLOW", 0)

    def check_position_exits(
        self,
        symbol: str,
        position: Any,
        current_price: float,
        equity: float,
    ) -> RiskCheck:
        """Check if position should be exited."""
        entry_price = position.entry_price
        side = position.side
        entry_time = position.entry_time
        highest = getattr(position, "highest_price", entry_price)
        lowest = getattr(position, "lowest_price", entry_price)

        # Calculate current PnL
        if side == "LONG":
            pnl_pct = (current_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - current_price) / entry_price

        # 1. Hard stop loss (3%)
        if pnl_pct <= -self.max_position_loss_pct:
            return RiskCheck(
                True,
                f"Hard stop: {pnl_pct*100:.2f}% loss",
                "CLOSE",
                100,
            )

        # 2. Trailing stop (1.5% from peak)
        if side == "LONG" and highest > entry_price:
            trail_pct = (current_price - highest) / highest
            if trail_pct <= -self.trailing_stop_pct:
                return RiskCheck(
                    True,
                    f"Trailing stop: {trail_pct*100:.2f}% from peak ${highest:.4f}",
                    "CLOSE",
                    90,
                )
        elif side == "SHORT" and lowest < entry_price:
            trail_pct = (current_price - lowest) / lowest
            if trail_pct >= self.trailing_stop_pct:
                return RiskCheck(
                    True,
                    f"Trailing stop: +{trail_pct*100:.2f}% from low ${lowest:.4f}",
                    "CLOSE",
                    90,
                )

        # 3. Max hold time (6 hours)
        hold_seconds = time.time() - entry_time
        if hold_seconds > self.max_hold_seconds:
            if pnl_pct > 0:
                return RiskCheck(
                    True,
                    f"6h time exit (profitable: {pnl_pct*100:.2f}%)",
                    "CLOSE",
                    80,
                )
            else:
                return RiskCheck(
                    True,
                    f"6h forced close (loss: {pnl_pct*100:.2f}%)",
                    "CLOSE",
                    85,
                )

        # 4. Daily loss limit
        if self._daily_start_equity > 0:
            daily_loss_pct = abs(min(0, self._daily_loss)) / self._daily_start_equity
            if daily_loss_pct >= self.max_daily_loss_pct:
                return RiskCheck(
                    True,
                    f"Daily loss limit hit, closing {symbol}",
                    "CLOSE",
                    95,
                )

        # 5. Circuit breaker (30% drawdown)
        if equity < self._daily_start_equity * (1 - self.max_drawdown_pct):
            self._circuit_breaker = True
            return RiskCheck(
                True,
                f"Circuit breaker: {self.max_drawdown_pct*100:.0f}% drawdown",
                "CLOSE_ALL",
                100,
            )

        return RiskCheck(True, "Hold", "HOLD", 0)

    def record_daily_loss(self, loss_amount: float) -> None:
        """Record daily PnL (negative = loss)."""
        if loss_amount < 0:
            self._daily_loss += loss_amount

    def _count_correlated(self, symbol: str, open_positions: Dict[str, Any]) -> int:
        """Count how many positions are in the same correlation group."""
        held = set(open_positions.keys())
        count = 0

        for group_name, group_symbols in self.correlation_groups.items():
            if symbol in group_symbols:
                overlapping = held.intersection(group_symbols)
                count = len(overlapping)
                break

        return count

    def get_risk_status(self, equity: float) -> Dict[str, Any]:
        """Get current risk status."""
        daily_loss_pct = abs(min(0, self._daily_loss)) / self._daily_start_equity if self._daily_start_equity > 0 else 0
        dd_pct = (self._daily_start_equity - equity) / self._daily_start_equity if self._daily_start_equity > 0 else 0

        return {
            "circuit_breaker": self._circuit_breaker,
            "halted": time.time() < self._halt_until,
            "daily_loss_pct": daily_loss_pct * 100,
            "daily_loss_limit_pct": self.max_daily_loss_pct * 100,
            "daily_loss_remaining_pct": (self.max_daily_loss_pct - daily_loss_pct) * 100,
            "drawdown_pct": dd_pct * 100,
            "max_drawdown_pct": self.max_drawdown_pct * 100,
            "drawdown_remaining_pct": (self.max_drawdown_pct - dd_pct) * 100,
        }
