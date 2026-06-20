"""Order management with stop-loss, take-profit, trailing stops, and Kelly sizing.

Tracks open positions and automatically triggers exits when price conditions are met.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class OrderParams:
    """Parameters for a managed order."""

    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    trailing_stop: Optional[float] = None
    trailing_stop_pct: Optional[float] = None
    entry_price: float = 0.0
    direction: int = 1  # 1 = long, -1 = short


@dataclass
class ManagedPosition:
    """A position with active order management."""

    symbol: str
    direction: int
    size: float
    entry_price: float
    order_params: OrderParams
    highest_price: float = 0.0
    lowest_price: float = float("inf")
    opened_at: str = ""
    trailing_stop_price: Optional[float] = None
    closed: bool = False
    close_reason: str = ""
    close_price: float = 0.0

    def __post_init__(self):
        if not self.opened_at:
            self.opened_at = datetime.now(timezone.utc).isoformat()
        self.highest_price = self.entry_price
        self.lowest_price = self.entry_price


class OrderManager:
    """Manage stop-loss, take-profit, and trailing stops for open positions."""

    def __init__(
        self,
        default_stop_loss_pct: float = 5.0,
        default_take_profit_pct: float = 10.0,
        default_trailing_stop_pct: float = 3.0,
        kelly_fraction: float = 0.25,
        max_position_pct: float = 0.25,
    ):
        self.default_stop_loss_pct = default_stop_loss_pct
        self.default_take_profit_pct = default_take_profit_pct
        self.default_trailing_stop_pct = default_trailing_stop_pct
        self.kelly_fraction = kelly_fraction
        self.max_position_pct = max_position_pct
        self._positions: Dict[str, ManagedPosition] = {}
        self._closed_positions: List[ManagedPosition] = []

    def calculate_position_size(
        self,
        equity: float,
        confidence: float,
        win_rate: float = 0.5,
        avg_win_loss_ratio: float = 1.5,
    ) -> float:
        """Calculate position size using fractional Kelly criterion.

        Args:
            equity: Total portfolio equity.
            confidence: Signal confidence (0-100).
            win_rate: Estimated win rate (0-1).
            avg_win_loss_ratio: Average win / average loss.

        Returns:
            Position size in USD.
        """
        # Kelly fraction: f* = (p * b - q) / b
        # where p = win_rate, q = 1-p, b = win/loss ratio
        p = max(0.01, min(0.99, win_rate))
        q = 1 - p
        b = max(0.1, avg_win_loss_ratio)

        kelly = (p * b - q) / b if b > 0 else 0
        kelly = max(0, kelly)

        # Scale by confidence and fractional Kelly
        scaled_kelly = kelly * (confidence / 100) * self.kelly_fraction

        # Cap at max position percentage
        scaled_kelly = min(scaled_kelly, self.max_position_pct)

        return equity * scaled_kelly

    def open_position(
        self,
        symbol: str,
        direction: int,
        size: float,
        entry_price: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        trailing_stop_pct: Optional[float] = None,
    ) -> ManagedPosition:
        """Open a managed position with order parameters."""
        if symbol in self._positions:
            existing = self._positions[symbol]
            if not existing.closed:
                logger.warning("Position already open for %s, closing first", symbol)
                self.close_position(symbol, entry_price, reason="replaced")

        trailing = trailing_stop_pct or self.default_trailing_stop_pct

        params = OrderParams(
            stop_loss=stop_loss,
            take_profit=take_profit,
            trailing_stop=trailing,
            trailing_stop_pct=trailing,
            entry_price=entry_price,
            direction=direction,
        )

        pos = ManagedPosition(
            symbol=symbol,
            direction=direction,
            size=size,
            entry_price=entry_price,
            order_params=params,
            trailing_stop_price=self._calc_initial_trailing_stop(entry_price, trailing, direction),
        )

        self._positions[symbol] = pos
        logger.info(
            "Position opened: %s %s %.4f @ %.2f | SL: %s | TP: %s | Trail: %.1f%%",
            symbol,
            "LONG" if direction > 0 else "SHORT",
            size,
            entry_price,
            f"${stop_loss:.2f}" if stop_loss else "N/A",
            f"${take_profit:.2f}" if take_profit else "N/A",
            trailing,
        )
        return pos

    def check_orders(self, symbol: str, current_price: float) -> Optional[Tuple[str, float]]:
        """Check if any order conditions are met for a position.

        Returns:
            (reason, close_price) if position should be closed, None otherwise.
        """
        pos = self._positions.get(symbol)
        if not pos or pos.closed:
            return None

        # Update price tracking
        pos.highest_price = max(pos.highest_price, current_price)
        pos.lowest_price = min(pos.lowest_price, current_price)

        direction = pos.direction

        # Check stop loss
        if pos.order_params.stop_loss is not None:
            if direction > 0 and current_price <= pos.order_params.stop_loss:
                return ("stop_loss", current_price)
            if direction < 0 and current_price >= pos.order_params.stop_loss:
                return ("stop_loss", current_price)

        # Check take profit
        if pos.order_params.take_profit is not None:
            if direction > 0 and current_price >= pos.order_params.take_profit:
                return ("take_profit", current_price)
            if direction < 0 and current_price <= pos.order_params.take_profit:
                return ("take_profit", current_price)

        # Check trailing stop
        if pos.order_params.trailing_stop is not None:
            trail_price = self._update_trailing_stop(pos, current_price)
            if trail_price is not None:
                if direction > 0 and current_price <= trail_price:
                    return ("trailing_stop", current_price)
                if direction < 0 and current_price >= trail_price:
                    return ("trailing_stop", current_price)

        return None

    def close_position(
        self,
        symbol: str,
        close_price: float,
        reason: str = "manual",
    ) -> Optional[ManagedPosition]:
        """Close a managed position."""
        pos = self._positions.get(symbol)
        if not pos or pos.closed:
            return None

        pos.closed = True
        pos.close_reason = reason
        pos.close_price = close_price

        pnl = self._calc_pnl(pos, close_price)
        self._closed_positions.append(pos)

        logger.info(
            "Position closed: %s @ %.2f | Reason: %s | PnL: %.2f",
            symbol,
            close_price,
            reason,
            pnl,
        )
        return pos

    def get_open_positions(self) -> List[ManagedPosition]:
        return [p for p in self._positions.values() if not p.closed]

    def get_position(self, symbol: str) -> Optional[ManagedPosition]:
        pos = self._positions.get(symbol)
        if pos and pos.closed:
            return None
        return pos

    def get_pnl_summary(self) -> Dict[str, Any]:
        total_pnl = 0.0
        wins = 0
        losses = 0
        for pos in self._closed_positions:
            pnl = self._calc_pnl(pos, pos.close_price)
            total_pnl += pnl
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
        total = wins + losses
        return {
            "total_closed": len(self._closed_positions),
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / total * 100) if total > 0 else 0,
            "total_pnl": total_pnl,
            "open_positions": len(self.get_open_positions()),
        }

    # --- Internal helpers ---

    def _calc_initial_trailing_stop(self, entry_price: float, pct: float, direction: int) -> Optional[float]:
        if pct is None or pct <= 0:
            return None
        offset = entry_price * (pct / 100)
        if direction > 0:
            return entry_price - offset
        else:
            return entry_price + offset

    def _update_trailing_stop(self, pos: ManagedPosition, current_price: float) -> Optional[float]:
        pct = pos.order_params.trailing_stop_pct
        if pct is None or pct <= 0:
            return None

        direction = pos.direction
        offset = current_price * (pct / 100)

        if direction > 0:
            # Long: trail below highest price
            new_trail = pos.highest_price - (pos.highest_price * (pct / 100))
            if pos.trailing_stop_price is None or new_trail > pos.trailing_stop_price:
                pos.trailing_stop_price = new_trail
        else:
            # Short: trail above lowest price
            new_trail = pos.lowest_price + (pos.lowest_price * (pct / 100))
            if pos.trailing_stop_price is None or new_trail < pos.trailing_stop_price:
                pos.trailing_stop_price = new_trail

        return pos.trailing_stop_price

    @staticmethod
    def _calc_pnl(pos: ManagedPosition, close_price: float) -> float:
        if pos.direction > 0:
            return (close_price - pos.entry_price) * pos.size
        else:
            return (pos.entry_price - close_price) * pos.size
