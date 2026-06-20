"""Portfolio manager with Kelly criterion sizing and multi-asset allocation.

Targets maximum growth with controlled risk:
  - Quarter-Kelly for position sizing
  - Multi-asset diversification across crypto/forex/commodities
  - Dynamic allocation based on signal strength and correlation
  - Daily compounding of all gains
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """Active position."""
    symbol: str
    side: str  # "LONG" or "SHORT"
    entry_price: float
    size_units: float
    size_usd: float
    margin: float
    leverage: float
    entry_time: float  # timestamp
    stop_loss: float = 0.0
    take_profit: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    pnl: float = 0.0


@dataclass
class PortfolioState:
    """Complete portfolio state."""
    cash: float
    equity: float
    positions: Dict[str, Position] = field(default_factory=dict)
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    daily_pnl: float = 0.0
    peak_equity: float = 0.0
    max_drawdown: float = 0.0


class GrowthPortfolio:
    """Portfolio manager for aggressive growth.

    Uses quarter-Kelly criterion for position sizing with multi-asset
    diversification and dynamic allocation.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.initial_cash = self.config.get("initial_cash", 10.0)
        self.leverage = self.config.get("leverage", 10.0)
        self.max_positions = self.config.get("max_positions", 7)
        self.max_position_pct = self.config.get("max_position_pct", 0.80)
        self.min_order_usd = self.config.get("min_order_usd", 1.0)
        self.kelly_fraction = self.config.get("kelly_fraction", 0.40)  # Half-Kelly for aggression

        # Fee model (Hyperliquid-style)
        self.taker_fee = self.config.get("taker_fee", 0.0005)  # 0.05%
        self.maker_fee = self.config.get("maker_fee", 0.0002)  # 0.02%
        self.slippage = self.config.get("slippage", 0.0005)  # 0.05%

        # Asset-specific leverage limits
        self.leverage_map = {
            "BTC": 10.0, "ETH": 10.0, "SOL": 10.0,
            "WIF": 5.0, "JUP": 5.0, "RENDER": 5.0,
            "DOGE": 5.0, "SUI": 5.0, "LINK": 5.0,
            "NEAR": 5.0, "AAVE": 5.0, "FET": 5.0,
            "XAU": 10.0, "XAG": 10.0, "NATGAS": 5.0,
            "MXN": 10.0, "BRL": 10.0, "ZAR": 10.0,
            "NZD": 10.0, "HUF": 10.0, "MYR": 10.0, "PLN": 10.0,
        }

        self.state = PortfolioState(
            cash=self.initial_cash,
            equity=self.initial_cash,
            peak_equity=self.initial_cash,
        )

        self._daily_start_equity = self.initial_cash
        self._daily_reset_hour = 0

    def reset_daily(self) -> None:
        """Reset daily tracking (call at start of each trading day)."""
        self._daily_start_equity = self.state.equity
        self.state.daily_pnl = 0.0

    def kelly_size(self, win_rate: float, win_loss_ratio: float, confidence: int) -> float:
        """Calculate position size using Kelly criterion.

        Args:
            win_rate: Historical win rate (0.0 to 1.0)
            win_loss_ratio: Average win / average loss
            confidence: Signal confidence (0-100)

        Returns:
            Position size as fraction of equity (0.0 to max_position_pct)
        """
        if win_loss_ratio <= 0 or win_rate <= 0:
            return 0.0

        # Kelly formula: f* = (p * b - q) / b
        p = win_rate
        q = 1.0 - p
        b = win_loss_ratio

        kelly = (p * b - q) / b

        if kelly <= 0:
            return 0.0

        # Scale by confidence and kelly fraction
        scaled = kelly * self.kelly_fraction * (confidence / 100)

        return min(scaled, self.max_position_pct)

    def estimate_kelly_params(self) -> Tuple[float, float]:
        """Estimate win rate and win/loss ratio from history."""
        if self.state.total_trades < 5:
            return 0.5, 1.5  # Default optimistic assumptions

        win_rate = self.state.wins / self.state.total_trades

        # Estimate from total PnL
        avg_pnl = self.state.total_pnl / self.state.total_trades
        if avg_pnl > 0:
            win_loss_ratio = 1.5 + (avg_pnl / self.state.equity)
        else:
            win_loss_ratio = 1.0

        return win_rate, win_loss_ratio

    def calculate_position(
        self,
        symbol: str,
        side: str,
        price: float,
        signal_confidence: int,
        signal_size_pct: float,
    ) -> Optional[Dict[str, Any]]:
        """Calculate position details.

        Args:
            symbol: Asset symbol
            side: "LONG" or "SHORT"
            price: Current price
            signal_confidence: Signal confidence (0-100)
            signal_size_pct: Signal-recommended size (0.0-1.0)

        Returns:
            Position dict or None if can't open
        """
        # Check if we can open more positions
        if len(self.state.positions) >= self.max_positions:
            logger.debug(f"Max positions ({self.max_positions}) reached")
            return None

        if symbol in self.state.positions:
            logger.debug(f"Already have position in {symbol}")
            return None

        # Get asset-specific leverage
        asset_leverage = self.leverage_map.get(symbol, self.leverage)

        # Kelly sizing
        win_rate, win_loss_ratio = self.estimate_kelly_params()
        kelly_pct = self.kelly_size(win_rate, win_loss_ratio, signal_confidence)

        # Use max of Kelly and signal-recommended
        size_pct = max(kelly_pct, signal_size_pct)
        size_pct = min(size_pct, self.max_position_pct)

        # Calculate notional
        equity = self.state.equity
        notional = equity * size_pct

        # Check minimum order
        if notional < self.min_order_usd:
            logger.debug(f"Order too small: ${notional:.2f} < ${self.min_order_usd}")
            return None

        # Margin required
        margin = notional / asset_leverage

        # Check available capital
        margin_buffer = margin * 1.1  # 10% buffer for fees
        if margin_buffer > self.state.cash * 0.95:
            # Scale down to available capital
            available = self.state.cash * 0.90
            margin = available
            notional = margin * asset_leverage
            size_pct = notional / equity

        # Apply slippage
        if side == "LONG":
            fill_price = price * (1 + self.slippage)
        else:
            fill_price = price * (1 - self.slippage)

        # Size in units
        size_units = notional / fill_price

        # Commission
        commission = notional * self.taker_fee

        return {
            "symbol": symbol,
            "side": side,
            "entry_price": fill_price,
            "size_units": size_units,
            "size_usd": notional,
            "margin": margin,
            "leverage": asset_leverage,
            "commission": commission,
            "size_pct": size_pct,
            "equity_at_entry": equity,
        }

    def open_position(self, pos_info: Dict[str, Any]) -> Position:
        """Execute position open."""
        import time

        symbol = pos_info["symbol"]
        side = pos_info["side"]
        entry_price = pos_info["entry_price"]
        size_units = pos_info["size_units"]
        size_usd = pos_info["size_usd"]
        margin = pos_info["margin"]
        leverage = pos_info["leverage"]
        commission = pos_info["commission"]

        # Deduct margin + commission from cash
        self.state.cash -= (margin + commission)

        position = Position(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            size_units=size_units,
            size_usd=size_usd,
            margin=margin,
            leverage=leverage,
            entry_time=time.time(),
            highest_price=entry_price,
            lowest_price=entry_price,
        )

        self.state.positions[symbol] = position
        self.state.total_trades += 1

        logger.info(
            f"OPEN {side} {symbol}: {size_units:.4f} @ ${entry_price:.4f} "
            f"(notional=${size_usd:.2f}, margin=${margin:.2f}, fee=${commission:.4f})"
        )

        return position

    def close_position(self, symbol: str, current_price: float, reason: str = "") -> Optional[float]:
        """Close position and return realized PnL."""
        if symbol not in self.state.positions:
            return None

        pos = self.state.positions[symbol]

        # Apply slippage on exit
        if pos.side == "LONG":
            exit_price = current_price * (1 - self.slippage)
            pnl = (exit_price - pos.entry_price) * pos.size_units
        else:
            exit_price = current_price * (1 + self.slippage)
            pnl = (pos.entry_price - exit_price) * pos.size_units

        # Commission on exit
        commission = pos.size_usd * self.maker_fee
        net_pnl = pnl - commission

        # Return margin + PnL to cash
        self.state.cash += pos.margin + net_pnl

        # Update stats
        self.state.total_pnl += net_pnl
        self.state.daily_pnl += net_pnl
        if net_pnl > 0:
            self.state.wins += 1
        else:
            self.state.losses += 1

        # Update equity
        self.state.equity = self.state.cash + sum(
            self._position_value(p, current_price) for s, p in self.state.positions.items() if s != symbol
        )

        # Track drawdown
        if self.state.equity > self.state.peak_equity:
            self.state.peak_equity = self.state.equity
        dd = (self.state.peak_equity - self.state.equity) / self.state.peak_equity
        if dd > self.state.max_drawdown:
            self.state.max_drawdown = dd

        del self.state.positions[symbol]

        logger.info(
            f"CLOSE {pos.side} {symbol}: PnL=${net_pnl:.4f} ({reason}) "
            f"[equity=${self.state.equity:.2f}, cash=${self.state.cash:.2f}]"
        )

        return net_pnl

    def update_position_price(self, symbol: str, current_price: float) -> None:
        """Update position tracking (highest/lowest, unrealized PnL)."""
        if symbol not in self.state.positions:
            return

        pos = self.state.positions[symbol]
        pos.highest_price = max(pos.highest_price, current_price)
        pos.lowest_price = min(pos.lowest_price, current_price)

        if pos.side == "LONG":
            pos.pnl = (current_price - pos.entry_price) * pos.size_units
        else:
            pos.pnl = (pos.entry_price - current_price) * pos.size_units

    def _position_value(self, pos: Position, current_price: float) -> float:
        """Calculate position margin value + unrealized PnL."""
        if pos.side == "LONG":
            unrealized = (current_price - pos.entry_price) * pos.size_units
        else:
            unrealized = (pos.entry_price - current_price) * pos.size_units
        return pos.margin + unrealized

    def update_equity(self, prices: Dict[str, float]) -> float:
        """Recalculate total equity from current prices."""
        total_margin = 0.0
        total_unrealized = 0.0

        for symbol, pos in self.state.positions.items():
            price = prices.get(symbol, pos.entry_price)
            self.update_position_price(symbol, price)
            total_margin += pos.margin
            total_unrealized += pos.pnl

        self.state.equity = self.state.cash + total_margin + total_unrealized

        # Update drawdown
        if self.state.equity > self.state.peak_equity:
            self.state.peak_equity = self.state.equity
        dd = (self.state.peak_equity - self.state.equity) / self.state.peak_equity
        if dd > self.state.max_drawdown:
            self.state.max_drawdown = dd

        return self.state.equity

    def get_target_curve(self, days: int = 10) -> List[float]:
        """Calculate ideal compound growth curve ($10 -> $10K in 10 days)."""
        start = self.initial_cash
        target = 10000.0
        daily_mult = (target / start) ** (1.0 / days)
        return [start * (daily_mult ** d) for d in range(days + 1)]

    def get_progress(self) -> Dict[str, Any]:
        """Get current progress toward $10K target."""
        target = 10000.0
        equity = self.state.equity
        progress_pct = (equity - self.initial_cash) / (target - self.initial_cash) * 100
        multiplier = equity / self.initial_cash

        return {
            "equity": equity,
            "target": target,
            "progress_pct": min(100, progress_pct),
            "multiplier": multiplier,
            "daily_pnl": self.state.daily_pnl,
            "total_pnl": self.state.total_pnl,
            "total_trades": self.state.total_trades,
            "win_rate": self.state.wins / max(1, self.state.total_trades),
            "max_drawdown": self.state.max_drawdown,
            "open_positions": len(self.state.positions),
            "cash": self.state.cash,
        }
