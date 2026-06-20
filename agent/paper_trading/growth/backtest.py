"""Backtest validation for the growth framework.

Tests the multi-signal strategy on historical data to validate
the $10 -> $10K thesis before paper trading.

Usage:
    cd agent
    python -m paper_trading.growth.backtest
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class GrowthBacktest:
    """Simplified backtest for growth strategy validation."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.initial_cash = self.config.get("initial_cash", 10.0)
        self.leverage = self.config.get("leverage", 10.0)
        self.max_positions = self.config.get("max_positions", 5)
        self.position_pct = self.config.get("position_pct", 0.60)
        self.slippage = self.config.get("slippage", 0.0005)
        self.taker_fee = self.config.get("taker_fee", 0.0005)

    def run(
        self,
        price_data: Dict[str, pd.DataFrame],
        fear_greed_data: Optional[pd.Series] = None,
    ) -> Dict[str, Any]:
        """Run backtest on historical price data.

        Args:
            price_data: {symbol: DataFrame with open/high/low/close/volume}
            fear_greed_data: Optional Series of F&G values indexed by date

        Returns:
            Backtest results dict
        """
        cash = self.initial_cash
        equity = self.initial_cash
        peak_equity = self.initial_cash
        positions: Dict[str, Dict] = {}
        trades: List[Dict] = []
        equity_curve = [{"date": "start", "equity": equity, "cash": cash}]

        # Get common date range
        all_dates = set()
        for df in price_data.values():
            all_dates.update(df.index)
        dates = sorted(all_dates)

        if not dates:
            return {"error": "No data"}

        # Simulate each time step
        for i, date in enumerate(dates):
            prices = {}
            for sym, df in price_data.items():
                if date in df.index:
                    prices[sym] = df.loc[date, "close"]

            if not prices:
                continue

            # Update positions
            for sym in list(positions.keys()):
                if sym in prices:
                    pos = positions[sym]
                    current = prices[sym]
                    if pos["side"] == "LONG":
                        pos["pnl"] = (current - pos["entry"]) * pos["size"]
                    else:
                        pos["pnl"] = (pos["entry"] - current) * pos["size"]

                    # Check exit conditions
                    pnl_pct = pos["pnl"] / (pos["margin"])
                    age_hours = (date - pos["entry_date"]).total_seconds() / 3600

                    # Hard stop (3%)
                    if pnl_pct <= -0.03:
                        cash += pos["margin"] + pos["pnl"]
                        trades.append({
                            "date": str(date), "symbol": sym, "side": pos["side"],
                            "pnl": pos["pnl"], "reason": "stop_loss", "age_h": age_hours,
                        })
                        del positions[sym]
                        continue

                    # Trailing stop (1.5% from peak)
                    if pos["side"] == "LONG" and current > pos.get("peak", pos["entry"]):
                        pos["peak"] = current
                    if "peak" in pos and pos["side"] == "LONG":
                        if (current - pos["peak"]) / pos["peak"] < -0.015:
                            cash += pos["margin"] + pos["pnl"]
                            trades.append({
                                "date": str(date), "symbol": sym, "side": pos["side"],
                                "pnl": pos["pnl"], "reason": "trailing_stop", "age_h": age_hours,
                            })
                            del positions[sym]
                            continue

                    # 6-hour time exit
                    if age_hours > 6:
                        cash += pos["margin"] + pos["pnl"]
                        trades.append({
                            "date": str(date), "symbol": sym, "side": pos["side"],
                            "pnl": pos["pnl"], "reason": "time_exit", "age_h": age_hours,
                        })
                        del positions[sym]
                        continue

            # Generate entry signals
            if len(positions) < self.max_positions:
                fg = 50
                if fear_greed_data is not None and date in fear_greed_data.index:
                    fg = fear_greed_data.loc[date]

                for sym, current_price in prices.items():
                    if sym in positions:
                        continue
                    if len(positions) >= self.max_positions:
                        break

                    # Simple momentum + sentiment signal
                    if sym in price_data:
                        df = price_data[sym]
                        if date in df.index and len(df.loc[:date]) >= 20:
                            recent = df.loc[:date]["close"].tail(20)
                            ma20 = recent.mean()
                            rsi = self._compute_rsi(recent.values, 14)

                            # Signal logic
                            signal = 0
                            if fg < 25:
                                signal += 1  # Contrarian buy on extreme fear
                            if current_price > ma20:
                                signal += 1  # Above MA
                            if not np.isnan(rsi) and rsi < 40:
                                signal += 1  # Oversold

                            if signal >= 2:
                                side = "LONG"
                                notional = equity * self.position_pct
                                margin = notional / self.leverage
                                if margin < cash * 0.9:
                                    fill = current_price * (1 + self.slippage)
                                    size = notional / fill
                                    commission = notional * self.taker_fee
                                    cash -= (margin + commission)
                                    positions[sym] = {
                                        "side": side,
                                        "entry": fill,
                                        "size": size,
                                        "margin": margin,
                                        "pnl": 0,
                                        "entry_date": date,
                                    }
                                    trades.append({
                                        "date": str(date), "symbol": sym, "side": side,
                                        "entry": fill, "notional": notional,
                                        "reason": f"signal: fg={fg}, price>ma20",
                                    })

            # Calculate equity
            total_margin = sum(p["margin"] for p in positions.values())
            total_unrealized = sum(p["pnl"] for p in positions.values())
            equity = cash + total_margin + total_unrealized

            if equity > peak_equity:
                peak_equity = equity

            equity_curve.append({
                "date": str(date),
                "equity": equity,
                "cash": cash,
                "positions": len(positions),
                "unrealized": total_unrealized,
            })

        # Close remaining positions
        for sym, pos in list(positions.items()):
            last_price = prices.get(sym, pos["entry"])
            if pos["side"] == "LONG":
                pnl = (last_price - pos["entry"]) * pos["size"]
            else:
                pnl = (pos["entry"] - last_price) * pos["size"]
            cash += pos["margin"] + pnl
            trades.append({
                "date": str(dates[-1]), "symbol": sym, "side": pos["side"],
                "pnl": pnl, "reason": "backtest_end",
            })

        # Compute metrics
        equity_values = [e["equity"] for e in equity_curve]
        returns = np.diff(equity_values) / equity_values[:-1]

        final_equity = equity_values[-1] if equity_values else self.initial_cash
        total_return = (final_equity - self.initial_cash) / self.initial_cash
        max_dd = self._max_drawdown(equity_values)

        winning_trades = [t for t in trades if t.get("pnl", 0) > 0]
        losing_trades = [t for t in trades if t.get("pnl", 0) < 0]

        results = {
            "initial_cash": self.initial_cash,
            "final_equity": final_equity,
            "total_return_pct": total_return * 100,
            "multiplier": final_equity / self.initial_cash,
            "max_drawdown_pct": max_dd * 100,
            "total_trades": len(trades),
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "win_rate": len(winning_trades) / max(1, len(trades)),
            "avg_win": np.mean([t["pnl"] for t in winning_trades]) if winning_trades else 0,
            "avg_loss": np.mean([t["pnl"] for t in losing_trades]) if losing_trades else 0,
            "sharpe": np.mean(returns) / np.std(returns) * np.sqrt(252) if len(returns) > 1 and np.std(returns) > 0 else 0,
            "trades": trades,
            "equity_curve": equity_curve,
            "achieved_10k": final_equity >= 10000,
        }

        return results

    @staticmethod
    def _compute_rsi(prices: np.ndarray, period: int = 14) -> float:
        """Compute RSI for last value."""
        if len(prices) < period + 1:
            return 50.0
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _max_drawdown(equity_curve: List[float]) -> float:
        """Calculate maximum drawdown."""
        peak = equity_curve[0]
        max_dd = 0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
        return max_dd


def run_validation():
    """Run backtest validation with synthetic data."""
    logging.basicConfig(level=logging.INFO)

    print("=" * 60)
    print("GROWTH FRAMEWORK BACKTEST VALIDATION")
    print("=" * 60)

    # Generate synthetic price data for testing
    np.random.seed(42)
    dates = pd.date_range("2025-01-01", periods=180, freq="D")

    price_data = {}
    for sym in ["BTC", "ETH", "SOL"]:
        base_price = {"BTC": 43000, "ETH": 2500, "SOL": 100}[sym]
        returns = np.random.normal(0.002, 0.03, len(dates))  # Slight uptrend
        prices = base_price * np.cumprod(1 + returns)
        price_data[sym] = pd.DataFrame({
            "open": prices * (1 + np.random.uniform(-0.01, 0.01, len(dates))),
            "high": prices * (1 + np.random.uniform(0, 0.03, len(dates))),
            "low": prices * (1 - np.random.uniform(0, 0.03, len(dates))),
            "close": prices,
            "volume": np.random.uniform(1e6, 1e8, len(dates)),
        }, index=dates)

    # Generate Fear & Greed data
    fg_values = (30 + np.random.normal(0, 15, len(dates))).clip(0, 100).astype(int)
    fear_greed = pd.Series(fg_values, index=dates)

    # Run backtest
    bt = GrowthBacktest({
        "initial_cash": 10.0,
        "leverage": 10.0,
        "max_positions": 5,
        "position_pct": 0.60,
    })

    results = bt.run(price_data, fear_greed)

    # Print results
    print(f"\nInitial Cash:  ${results['initial_cash']:.2f}")
    print(f"Final Equity:  ${results['final_equity']:.2f}")
    print(f"Total Return:  {results['total_return_pct']:.1f}%")
    print(f"Multiplier:    {results['multiplier']:.1f}x")
    print(f"Max Drawdown:  {results['max_drawdown_pct']:.1f}%")
    print(f"Total Trades:  {results['total_trades']}")
    print(f"Win Rate:      {results['win_rate']*100:.0f}%")
    print(f"Sharpe Ratio:  {results['sharpe']:.2f}")
    print(f"Achieved $10K: {results['achieved_10k']}")

    # Save results
    out_dir = Path("./paper_runs/growth/backtest")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Remove non-serializable items
    save_results = {k: v for k, v in results.items() if k not in ("trades", "equity_curve")}
    # Convert numpy bools to Python bools
    save_results = {k: bool(v) if isinstance(v, (np.bool_,)) else v for k, v in save_results.items()}
    with open(out_dir / "metrics.json", "w") as f:
        json.dump(save_results, f, indent=2, default=str)

    # Save equity curve
    with open(out_dir / "equity.json", "w") as f:
        json.dump(results["equity_curve"], f, indent=2)

    print(f"\nResults saved to {out_dir}")

    return results


if __name__ == "__main__":
    run_validation()
