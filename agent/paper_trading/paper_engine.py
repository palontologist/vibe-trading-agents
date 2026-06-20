"""Paper trading engine for perp simulations.

Extends CryptoEngine with real-time execution simulation and wallet tracking.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from backtest.engines.crypto import CryptoEngine
from backtest.models import EquitySnapshot, Position, TradeRecord

logger = logging.getLogger(__name__)


class PaperTradingEngine(CryptoEngine):
    """Paper trading engine simulates perp trades with real-time data.

    Mirrors CryptoEngine but adds:
      - Real-time bar simulation
      - Wallet balance tracking
      - Paper profit/loss calculation
      - Trade logging to JSONL
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.wallet_address: str = config.get("wallet_address", "")
        self.run_dir: Path = Path(config.get("run_dir", "./paper_runs"))
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.trade_log_path = self.run_dir / "trades.jsonl"
        self.equity_log_path = self.run_dir / "equity.jsonl"
        self._trade_count = 0
        self._initial_wallet_value = config.get("initial_wallet_value", 0.0)

    def can_execute(self, symbol: str, direction: int, bar: pd.Series) -> bool:
        """Paper trading: always allow execution for simulation."""
        return True

    def round_size(self, raw_size: float, price: float) -> float:
        """Round to 6 decimals like crypto."""
        return round(max(raw_size, 0.0), 6)

    def calc_commission(self, size: float, price: float, direction: int, is_open: bool) -> float:
        """Use taker fees for realistic simulation."""
        rate = self.taker_rate if is_open else self.maker_rate
        return size * price * rate

    def apply_slippage(self, price: float, direction: int) -> float:
        """Apply slippage for realistic fills."""
        return price * (1 + direction * self.slippage_rate)

    def _make_timestamp(self, timestamp: Optional[datetime] = None) -> pd.Timestamp:
        if timestamp is None:
            return pd.Timestamp(datetime.utcnow())
        return pd.Timestamp(timestamp)

    def execute_paper_trade(
        self,
        symbol: str,
        direction: int,
        target_weight: float,
        price: float,
        equity: float,
        timestamp: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """Execute a single paper trade and log it.

        Args:
            symbol: Trading pair (e.g., "BTC-USDT").
            direction: 1 for long, -1 for short, 0 for close.
            target_weight: Target portfolio weight (-1.0 to 1.0).
            price: Current market price.
            equity: Current portfolio equity.
            timestamp: Trade timestamp (defaults to now).

        Returns:
            Trade record dict or None if no trade executed.
        """
        ts = self._make_timestamp(timestamp)
        self._active_symbol = symbol

        target_dir = 1 if target_weight > 1e-9 else (-1 if target_weight < -1e-9 else 0)
        current_pos = self.positions.get(symbol)

        # Nothing to do
        if current_pos is None and target_dir == 0:
            return None

        # Close if direction changed or target is flat
        if current_pos is not None:
            need_close = target_dir == 0 or target_dir != current_pos.direction
            if need_close:
                slipped = self.apply_slippage(price, -current_pos.direction)
                trade = self._close_and_record(symbol, slipped, ts, "signal")
                if target_dir == 0:
                    return trade
                current_pos = None

        # Open new position
        if target_dir != 0 and symbol not in self.positions:
            slipped = self.apply_slippage(price, target_dir)
            leverage = self.default_leverage
            target_notional = abs(target_weight) * equity * leverage
            raw_size = self._calc_raw_size(symbol, target_notional, slipped)
            size = self.round_size(raw_size, slipped)

            if size <= 0:
                return None

            margin = self._calc_margin(symbol, size, slipped, leverage)
            comm = self.calc_commission(size, slipped, target_dir, is_open=True)

            # Check capital
            if margin + comm > self.capital:
                available = self.capital - comm
                if available <= 0:
                    return None
                size = self.round_size(self._calc_raw_size(symbol, available * leverage, slipped), slipped)
                if size <= 0:
                    return None
                margin = self._calc_margin(symbol, size, slipped, leverage)
                comm = self.calc_commission(size, slipped, target_dir, is_open=True)

            self.capital -= margin + comm
            self.positions[symbol] = Position(
                symbol=symbol,
                direction=target_dir,
                entry_price=slipped,
                entry_time=ts,
                size=size,
                leverage=leverage,
                entry_bar_idx=self._bar_idx,
                entry_commission=comm,
            )

            trade = {
                "timestamp": ts.isoformat(),
                "symbol": symbol,
                "side": "buy" if target_dir == 1 else "sell",
                "price": round(slipped, 4),
                "size": round(size, 6),
                "notional": round(size * slipped, 2),
                "direction": target_dir,
                "leverage": leverage,
                "margin": round(margin, 2),
                "commission": round(comm, 4),
                "action": "open",
                "wallet": self.wallet_address,
            }
            self._log_trade(trade)
            return trade

        return None

    def _close_and_record(self, symbol: str, exit_price: float, exit_time: pd.Timestamp, reason: str) -> Dict[str, Any]:
        """Close position and record the trade."""
        self._active_symbol = symbol
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return {}

        pnl = self._calc_pnl(symbol, pos.direction, pos.size, pos.entry_price, exit_price)
        margin = self._calc_margin(symbol, pos.size, pos.entry_price, pos.leverage)
        pnl_pct = pnl / margin * 100 if margin > 1e-9 else 0.0
        exit_comm = self.calc_commission(pos.size, exit_price, pos.direction, is_open=False)

        self.capital += margin + pnl - exit_comm
        holding_bars = max(self._bar_idx - pos.entry_bar_idx, 0)

        trade = TradeRecord(
            symbol=symbol,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_time=pos.entry_time,
            exit_time=exit_time,
            size=pos.size,
            leverage=pos.leverage,
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_reason=reason,
            holding_bars=holding_bars,
            commission=pos.entry_commission + exit_comm,
        )
        self.trades.append(trade)

        trade_dict = {
            "timestamp": exit_time.isoformat(),
            "symbol": symbol,
            "side": "sell" if pos.direction == 1 else "buy",
            "price": round(exit_price, 4),
            "size": round(pos.size, 6),
            "notional": round(pos.size * exit_price, 2),
            "direction": pos.direction,
            "leverage": pos.leverage,
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 2),
            "commission": round(pos.entry_commission + exit_comm, 4),
            "action": "close",
            "reason": reason,
            "wallet": self.wallet_address,
        }
        self._log_trade(trade_dict)
        return trade_dict

    def _log_trade(self, trade: Dict[str, Any]) -> None:
        """Append trade to JSONL log."""
        with open(self.trade_log_path, "a") as f:
            f.write(json.dumps(trade) + "\n")
        self._trade_count += 1

    def get_unrealized_pnl(self, current_prices: Dict[str, float]) -> Dict[str, float]:
        """Calculate unrealized PnL for all open positions.

        Args:
            current_prices: symbol -> current market price.

        Returns:
            symbol -> unrealized PnL.
        """
        pnl_map: Dict[str, float] = {}
        for sym, pos in self.positions.items():
            if sym in current_prices:
                pnl = self._calc_pnl(
                    sym,
                    pos.direction,
                    pos.size,
                    pos.entry_price,
                    current_prices[sym],
                )
                pnl_map[sym] = pnl
        return pnl_map

    def log_equity_snapshot(
        self,
        timestamp: Optional[datetime] = None,
        current_prices: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """Log current equity state with proper unrealized PnL."""
        ts = self._make_timestamp(timestamp)
        prices = current_prices or {}
        unrealized_map = self.get_unrealized_pnl(prices)
        total_unrealized = sum(unrealized_map.values())

        total_margin = sum(
            self._calc_margin(p.symbol, p.size, p.entry_price, p.leverage) for p in self.positions.values()
        )
        equity = self.capital + total_margin + total_unrealized

        snapshot = {
            "timestamp": ts.isoformat(),
            "capital": round(self.capital, 2),
            "total_margin": round(total_margin, 2),
            "unrealized_pnl": round(total_unrealized, 2),
            "unrealized_by_symbol": {k: round(v, 2) for k, v in unrealized_map.items()},
            "equity": round(equity, 2),
            "positions": len(self.positions),
            "wallet_value": self._initial_wallet_value,
            "wallet": self.wallet_address,
        }

        with open(self.equity_log_path, "a") as f:
            f.write(json.dumps(snapshot) + "\n")

        return snapshot

    def get_summary(self, current_prices: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """Get paper trading summary with unrealized PnL."""
        total_realized_pnl = sum(t.pnl for t in self.trades)
        total_comm = sum(t.commission for t in self.trades)
        wins = len([t for t in self.trades if t.pnl > 0])

        unrealized_map = self.get_unrealized_pnl(current_prices or {})
        total_unrealized_pnl = sum(unrealized_map.values())
        total_pnl = total_realized_pnl + total_unrealized_pnl

        total_margin = sum(
            self._calc_margin(p.symbol, p.size, p.entry_price, p.leverage) for p in self.positions.values()
        )
        equity = self.capital + total_margin + total_unrealized_pnl

        return {
            "wallet": self.wallet_address,
            "total_trades": self._trade_count,
            "closed_trades": len(self.trades),
            "open_positions": len(self.positions),
            "capital": round(self.capital, 2),
            "total_margin": round(total_margin, 2),
            "equity": round(equity, 2),
            "realized_pnl": round(total_realized_pnl, 2),
            "unrealized_pnl": round(total_unrealized_pnl, 2),
            "total_pnl": round(total_pnl, 2),
            "total_commission": round(total_comm, 4),
            "win_rate": round(wins / len(self.trades) * 100, 2) if self.trades else 0,
            "positions": {
                sym: {
                    "size": p.size,
                    "direction": p.direction,
                    "entry": p.entry_price,
                    "unrealized_pnl": round(unrealized_map.get(sym, 0.0), 2),
                }
                for sym, p in self.positions.items()
            },
        }
