"""Aggressive Forex Paper Trading Launcher.

High-frequency scalping system for small account growth ($10 -> target).
Uses free Frankfurter API for real-time forex data (USD-MXN, USD-BRL, etc.).

Strategy: Aggressive momentum scalping on volatile exotics.
- 10x leverage, 60% position sizing
- RSI + Bollinger + MA crossover signals
- Tight 0.3% SL, 0.5% TP (scalping)
- Multiple trades per tick cycle
- Focus on highest-volatility pairs

Usage:
    cd /home/palontologist/Downloads/dev/Vibe-Trading/agent
    python -m paper_trading.aggressive_forex_launcher
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)


# ── Frankfurter Forex Data Fetcher ─────────────────────────────────────


class ForexDataFetcher:
    """Fetch real-time and historical forex data from Frankfurter API (ECB)."""

    BASE_URL = "https://api.frankfurter.app"

    def __init__(self, pairs: List[str], base: str = "USD"):
        """
        Args:
            pairs: List of quote currencies ['MXN', 'BRL', 'EUR', ...]
            base: Base currency (default USD)
        """
        self.pairs = pairs
        self.base = base
        self._tick_history: Dict[str, List[float]] = {}
        self._max_ticks = 500
        self._last_rate: Dict[str, float] = {}

    def fetch_latest(self) -> Dict[str, float]:
        """Fetch latest rates for all pairs."""
        to_param = ",".join(self.pairs)
        try:
            r = requests.get(f"{self.BASE_URL}/latest?from={self.base}&to={to_param}", timeout=10)
            data = r.json()
            rates = data.get("rates", {})
            result = {}
            for pair in self.pairs:
                if pair in rates:
                    price = float(rates[pair])
                    symbol = f"{self.base}/{pair}"
                    result[symbol] = price
                    self._last_rate[pair] = price
                    if symbol not in self._tick_history:
                        self._tick_history[symbol] = []
                    self._tick_history[symbol].append(price)
                    if len(self._tick_history[symbol]) > self._max_ticks:
                        self._tick_history[symbol] = self._tick_history[symbol][-self._max_ticks:]
            return result
        except Exception as exc:
            logger.warning("Forex fetch failed: %s", exc)
            return {}

    def fetch_historical(self, pair: str, days: int = 60) -> pd.DataFrame:
        """Fetch historical daily rates for a pair."""
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        try:
            r = requests.get(
                f"{self.BASE_URL}/{start}..{end}?from={self.base}&to={pair}",
                timeout=10,
            )
            data = r.json()
            rates = data.get("rates", {})
            if not rates:
                return pd.DataFrame()

            dates = sorted(rates.keys())
            rows = []
            for d in dates:
                rate = float(rates[d][pair])
                rows.append({
                    "trade_date": pd.Timestamp(d),
                    "open": rate,
                    "high": rate * 1.001,  # Synthetic intraday range
                    "low": rate * 0.999,
                    "close": rate,
                    "volume": 0.0,
                })

            df = pd.DataFrame(rows).set_index("trade_date")

            # Enhance with realistic OHLCV from daily changes
            closes = df["close"].values
            for i in range(1, len(df)):
                prev = closes[i - 1]
                curr = closes[i]
                # Create realistic candle from daily change
                body = abs(curr - prev)
                wick = body * 0.5
                df.iloc[i, df.columns.get_loc("open")] = prev
                df.iloc[i, df.columns.get_loc("high")] = max(prev, curr) + wick
                df.iloc[i, df.columns.get_loc("low")] = min(prev, curr) - wick
                df.iloc[i, df.columns.get_loc("close")] = curr
                df.iloc[i, df.columns.get_loc("volume")] = abs(curr - prev) / prev * 10000

            return df
        except Exception as exc:
            logger.warning("Historical fetch failed for USD/%s: %s", pair, exc)
            return pd.DataFrame()

    def get_ticks(self, symbol: str, count: int = 10) -> List[float]:
        return self._tick_history.get(symbol, [])[-count:]

    def get_recent_change(self, symbol: str, ticks: int = 5) -> float:
        history = self._tick_history.get(symbol, [])
        if len(history) < ticks + 1:
            return 0.0
        return (history[-1] - history[-(ticks + 1)]) / history[-(ticks + 1)] * 100


# ── Aggressive Technical Signals ────────────────────────────────────────


class AggressiveForexSignals:
    """High-frequency scalping signals for volatile forex pairs."""

    def __init__(self, fetcher: ForexDataFetcher):
        self.fetcher = fetcher
        self._indicators_cache: Dict[str, Dict[str, Any]] = {}

    def fetch_indicators(self, symbols: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
        targets = symbols or [f"USD/{p}" for p in self.fetcher.pairs]
        indicators = {}

        for sym in targets:
            pair = sym.split("/")[1] if "/" in sym else sym
            try:
                df = self.fetcher.fetch_historical(pair, days=60)
                if len(df) < 20:
                    # Use tick data if available
                    ticks = self.fetcher.get_ticks(sym, 20)
                    if len(ticks) < 10:
                        continue
                    # Build synthetic series from ticks
                    closes = ticks
                    current = closes[-1]
                    ma5 = sum(closes[-5:]) / 5
                    ma10 = sum(closes[-10:]) / 10
                    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else current

                    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
                    recent = deltas[-14:] if len(deltas) >= 14 else deltas
                    gains = sum(d for d in recent if d > 0)
                    losses = sum(-d for d in recent if d < 0)
                    rsi = 100 - (100 / (1 + gains / losses)) if losses > 0 else (100 if gains > 0 else 50)

                    indicators[sym] = {
                        "price": current,
                        "ma5": round(ma5, 6),
                        "ma10": round(ma10, 6),
                        "ma20": round(ma20, 6),
                        "rsi": round(rsi, 1),
                        "boll_lo": round(ma20 * 0.998, 6),
                        "boll_hi": round(ma20 * 1.002, 6),
                        "atr": round(abs(closes[-1] - closes[-5]) / 5, 6) if len(closes) >= 5 else 0,
                        "vol_ratio": 1.0,
                    }
                    continue

                closes = df["close"].values
                highs = df["high"].values
                lows = df["low"].values
                volumes = df["volume"].values
                current = float(closes[-1])

                ma5 = float(sum(closes[-5:]) / 5)
                ma10 = float(sum(closes[-10:]) / 10)
                ma20 = float(sum(closes[-20:]) / 20)

                # RSI
                deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
                recent = deltas[-14:]
                gains = sum(d for d in recent if d > 0)
                losses = sum(-d for d in recent if d < 0)
                rsi = 100 - (100 / (1 + gains / losses)) if losses > 0 else (100 if gains > 0 else 50)

                # Bollinger
                mean20 = float(sum(closes[-20:]) / 20)
                std20 = (sum((c - mean20) ** 2 for c in closes[-20:]) / 20) ** 0.5
                boll_lo = mean20 - 2 * std20
                boll_hi = mean20 + 2 * std20

                # ATR
                atr = float(sum(highs[-14:]) / 14 - sum(lows[-14:]) / 14)

                # Volume ratio
                vol_mean = float(sum(volumes[-20:]) / 20) if len(volumes) >= 20 else 1.0
                vol_ratio = float(volumes[-1] / vol_mean) if vol_mean > 0 else 1.0

                indicators[sym] = {
                    "price": current,
                    "ma5": round(ma5, 6),
                    "ma10": round(ma10, 6),
                    "ma20": round(ma20, 6),
                    "rsi": round(rsi, 1),
                    "boll_lo": round(boll_lo, 6),
                    "boll_hi": round(boll_hi, 6),
                    "atr": round(atr, 6),
                    "vol_ratio": round(vol_ratio, 2),
                }
            except Exception as exc:
                logger.warning("Indicator failed for %s: %s", sym, exc)

        self._indicators_cache = indicators
        return indicators

    def generate_signals(self, indicators: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Aggressive scalping signals - more entries, tighter targets."""
        signals = {}

        for sym, ind in indicators.items():
            price = ind["price"]
            ma5 = ind["ma5"]
            ma10 = ind["ma10"]
            ma20 = ind["ma20"]
            rsi = ind["rsi"]
            boll_lo = ind["boll_lo"]
            boll_hi = ind["boll_hi"]

            score = 0
            reasons = []

            # AGGRESSIVE RSI: wider thresholds for more entries
            if rsi < 25:
                score += 35
                reasons.append(f"RSI extremely oversold ({rsi:.0f})")
            elif rsi < 35:
                score += 25
                reasons.append(f"RSI oversold ({rsi:.0f})")
            elif rsi > 75:
                score -= 35
                reasons.append(f"RSI extremely overbought ({rsi:.0f})")
            elif rsi > 65:
                score -= 25
                reasons.append(f"RSI overbought ({rsi:.0f})")
            elif rsi < 45:
                score += 10
                reasons.append(f"RSI bearish ({rsi:.0f})")
            elif rsi > 55:
                score -= 10
                reasons.append(f"RSI bullish ({rsi:.0f})")

            # MA crossover (aggressive: 5/10 cross)
            if ma5 > ma10 and price > ma5:
                score += 20
                reasons.append("MA5 > MA10 bullish cross")
            elif ma5 < ma10 and price < ma5:
                score -= 20
                reasons.append("MA5 < MA10 bearish cross")

            # Bollinger bands
            if price < boll_lo:
                score += 15
                reasons.append("below Bollinger")
            elif price > boll_hi:
                score -= 15
                reasons.append("above Bollinger")

            # MA20 trend
            if price < ma20:
                score -= 8
                reasons.append("below MA20")
            elif price > ma20:
                score += 8
                reasons.append("above MA20")

            if score == 0:
                signals[sym] = {"action": "FLAT", "confidence": 50, "weight": 0.0, "score": score, "reasons": reasons}
                continue

            action = "LONG" if score > 0 else "SHORT"
            confidence = min(95, 50 + abs(score))
            weight = min(0.5, abs(score) / 80) * (1 if score > 0 else -1)

            signals[sym] = {
                "action": action,
                "confidence": confidence,
                "weight": round(weight, 3),
                "score": score,
                "reasons": reasons,
            }

        return signals


# ── Aggressive Paper Engine ─────────────────────────────────────────────


class AggressivePaperEngine:
    """Paper engine for aggressive forex scalping."""

    def __init__(self, initial_capital: float, leverage: float = 10.0, commission_rate: float = 0.0002):
        self.capital = initial_capital
        self.leverage = leverage
        self.commission_rate = commission_rate
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.trade_log: List[Dict[str, Any]] = []

    def open_position(
        self, symbol: str, direction: int, size_usd: float, price: float, sl: float, tp: float,
    ) -> Optional[Dict[str, Any]]:
        notional = size_usd * self.leverage
        margin = size_usd
        commission = notional * self.commission_rate
        if margin + commission > self.capital * 0.98:
            return None

        self.capital -= margin + commission
        size = notional / price

        self.positions[symbol] = {
            "direction": direction,
            "size": size,
            "entry": price,
            "margin": margin,
            "notional": notional,
            "sl": sl,
            "tp": tp,
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }

        trade = {
            "time": datetime.now(timezone.utc).isoformat(),
            "sym": symbol,
            "action": "OPEN",
            "dir": "SHORT" if direction == -1 else "LONG",
            "size": size,
            "price": price,
            "notional": notional,
            "margin": margin,
            "leverage": self.leverage,
            "sl": sl,
            "tp": tp,
        }
        self.trade_log.append(trade)
        return trade

    def close_position(self, symbol: str, price: float, reason: str = "signal") -> Optional[float]:
        if symbol not in self.positions:
            return None
        pos = self.positions.pop(symbol)
        pnl = (price - pos["entry"]) * pos["size"] * pos["direction"]
        commission = pos["size"] * price * self.commission_rate
        self.capital += pos["margin"] + pnl - commission

        trade = {
            "time": datetime.now(timezone.utc).isoformat(),
            "sym": symbol,
            "action": "CLOSE",
            "dir": "SHORT" if pos["direction"] == -1 else "LONG",
            "size": pos["size"],
            "entry": pos["entry"],
            "exit": price,
            "pnl": round(pnl, 4),
            "reason": reason,
        }
        self.trade_log.append(trade)
        return pnl

    def get_equity(self, prices: Dict[str, float]) -> float:
        equity = self.capital
        for sym, pos in self.positions.items():
            equity += pos["margin"]
            if sym in prices:
                equity += (prices[sym] - pos["entry"]) * pos["size"] * pos["direction"]
        return equity

    def get_summary(self, prices: Dict[str, float]) -> Dict[str, Any]:
        realized = sum(t.get("pnl", 0) for t in self.trade_log if t["action"] == "CLOSE")
        total_trades = len([t for t in self.trade_log if t["action"] == "CLOSE"])
        wins = len([t for t in self.trade_log if t["action"] == "CLOSE" and t.get("pnl", 0) > 0])
        equity = self.get_equity(prices)

        return {
            "equity": round(equity, 2),
            "capital": round(self.capital, 2),
            "realized_pnl": round(realized, 4),
            "total_pnl": round(realized, 4),
            "total_trades": total_trades,
            "wins": wins,
            "win_rate": round(wins / total_trades * 100, 1) if total_trades else 0,
            "open_positions": len(self.positions),
            "positions": {
                sym: {
                    "dir": "S" if p["direction"] == -1 else "L",
                    "size": p["size"],
                    "entry": p["entry"],
                    "notional": p["notional"],
                }
                for sym, p in self.positions.items()
            },
        }


# ── Main Aggressive Orchestrator ────────────────────────────────────────


class AggressiveForexOrchestrator:
    """Aggressive forex swing trader for small account growth.

    Uses daily ECB rates. Simulates intraday ticks using realistic
    volatility-based price movement. Holds positions for hours/days.
    """

    def __init__(self, config: Dict[str, Any]):
        self.pairs = config["pairs"]
        self.tick_interval = config.get("tick_interval_seconds", 30)
        self.initial_cash = config.get("initial_cash", 10.0)
        self.leverage = config.get("leverage", 10.0)
        self.daily_profit_target = config.get("daily_profit_target", 5.0)
        self.run_dir = config.get("run_dir", "./paper_runs/aggressive_forex")
        self.max_positions = config.get("max_positions", 5)
        self.position_pct = config.get("position_pct", 0.60)

        self.fetcher = ForexDataFetcher(self.pairs, base="USD")
        self.signals = AggressiveForexSignals(self.fetcher)
        self.engine = AggressivePaperEngine(
            initial_capital=self.initial_cash,
            leverage=self.leverage,
        )

        self._running = False
        self._tick_count = 0
        self._daily_start_equity = self.initial_cash
        self._trade_count_today = 0
        self._simulated_prices: Dict[str, float] = {}
        self._last_base_prices: Dict[str, float] = {}

        Path(self.run_dir).mkdir(parents=True, exist_ok=True)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        logger.info(
            "AggressiveForex: USD/%s | %ds ticks | $%.2f | %dx leverage | target: $%.2f/day",
            ", ".join(self.pairs),
            self.tick_interval,
            self.initial_cash,
            self.leverage,
            self.daily_profit_target,
        )

    def _signal_handler(self, signum, frame):
        logger.info("Shutdown signal received")
        self._running = False

    def _simulate_intraday_move(self, symbol: str, base_price: float) -> float:
        """Simulate realistic intraday price movement using geometric Brownian motion.

        Uses ATR-based volatility calibrated to each pair's typical daily range.
        """
        import random

        # Typical daily range as % of price for each pair
        vol_map = {
            "MXN": 0.004, "BRL": 0.006, "ZAR": 0.005, "NZD": 0.003,
            "HUF": 0.004, "MYR": 0.005, "PLN": 0.003, "EUR": 0.002,
            "GBP": 0.002, "JPY": 0.003, "CHF": 0.002, "CAD": 0.002,
            "AUD": 0.003, "INR": 0.002, "THB": 0.002, "PHP": 0.003,
        }
        pair = symbol.split("/")[1] if "/" in symbol else symbol
        daily_vol = vol_map.get(pair, 0.003)

        # Scale to tick interval (assuming 30s ticks, ~2880 ticks/day)
        tick_vol = daily_vol / (2880 ** 0.5)

        # Geometric Brownian motion step
        drift = 0  # No drift
        shock = random.gauss(0, 1)
        ret = drift + tick_vol * shock
        return base_price * (1 + ret)

    def run(self):
        self._running = True

        # Fetch base prices
        base_prices = self.fetcher.fetch_latest()
        self._simulated_prices = dict(base_prices)
        self._last_base_prices = dict(base_prices)
        self._daily_start_equity = self.engine.get_equity(base_prices) if base_prices else self.initial_cash

        logger.info("=" * 60)
        logger.info("AGGRESSIVE FOREX SWING TRADER")
        logger.info("Pairs: USD/%s", ", USD/".join(self.pairs))
        logger.info("Capital: $%.2f | Leverage: %dx", self.initial_cash, self.leverage)
        logger.info("Daily target: $%.2f | Max positions: %d", self.daily_profit_target, self.max_positions)
        logger.info("Note: Using daily ECB rates + simulated intraday ticks")
        logger.info("=" * 60)

        while self._running:
            try:
                self._tick()
            except Exception as exc:
                logger.error("Tick failed: %s", exc, exc_info=True)

            if self._running:
                time.sleep(self.tick_interval)

        self._print_final_summary()

    def _tick(self):
        self._tick_count += 1

        # Every 50 ticks (~25 min), refresh base price from API
        if self._tick_count % 50 == 0:
            new_prices = self.fetcher.fetch_latest()
            if new_prices:
                self._last_base_prices.update(new_prices)
                self._simulated_prices.update(new_prices)

        # Simulate intraday movement from base prices
        for sym, base in self._last_base_prices.items():
            self._simulated_prices[sym] = self._simulate_intraday_move(sym, base)

        prices = dict(self._simulated_prices)
        if not prices:
            return

        # Compute indicators using historical + simulated data
        indicators = self.signals.fetch_indicators(list(prices.keys()))
        # Update current price in indicators
        for sym in indicators:
            if sym in prices:
                indicators[sym]["price"] = prices[sym]
        entry_signals = self.signals.generate_signals(indicators)

        # Check exits
        for sym in list(self.engine.positions.keys()):
            if sym not in prices:
                continue
            pos = self.engine.positions[sym]
            price = prices[sym]
            direction = pos["direction"]
            entry = pos["entry"]

            if direction > 0:
                pnl = (price - entry) * pos["size"]
            else:
                pnl = (entry - price) * pos["size"]

            # TP: 0.4% move (daily swing)
            tp_target = pos["notional"] * 0.004
            if pnl >= tp_target and pnl > 0:
                self.engine.close_position(sym, price, "take_profit")
                self._trade_count_today += 1
                logger.info("TP: %s @ %.6f pnl=$%.4f", sym, price, pnl)
                continue

            # SL: 0.25% move against
            sl_limit = -pos["notional"] * 0.0025
            if pnl <= sl_limit:
                self.engine.close_position(sym, price, "stop_loss")
                self._trade_count_today += 1
                logger.info("SL: %s @ %.6f pnl=$%.4f", sym, price, pnl)
                continue

            # Signal reversal
            sig = entry_signals.get(sym, {})
            if sig.get("action") == "FLAT" and pnl > -0.01:
                self.engine.close_position(sym, price, "signal_flat")
                self._trade_count_today += 1
                continue

        # Open new positions
        equity = self.engine.get_equity(prices)
        daily_pnl = equity - self._daily_start_equity

        open_count = len(self.engine.positions)
        if open_count < self.max_positions:
            for sym, sig in entry_signals.items():
                if open_count >= self.max_positions:
                    break
                if sym in self.engine.positions:
                    continue
                if sig["action"] == "FLAT":
                    continue
                if sig["confidence"] < 45:
                    continue

                price = prices.get(sym)
                if not price:
                    continue

                size_usd = equity * self.position_pct
                if size_usd > equity * 0.95:
                    size_usd = equity * 0.95
                if size_usd < 0.50:
                    continue

                direction = 1 if sig["action"] == "LONG" else -1

                sl_pct = 0.0025  # 0.25% SL
                tp_pct = 0.004   # 0.4% TP

                sl = price * (1 - sl_pct) if direction == 1 else price * (1 + sl_pct)
                tp = price * (1 + tp_pct) if direction == 1 else price * (1 - tp_pct)

                trade = self.engine.open_position(sym, direction, size_usd, price, sl, tp)
                if trade:
                    open_count += 1
                    self._trade_count_today += 1
                    logger.info(
                        "ENTRY: %s %s $%.2f (notional $%.2f) @ %.6f | conf=%d | %s",
                        sym, sig["action"], size_usd, trade["notional"],
                        price, sig["confidence"], ", ".join(sig["reasons"]),
                    )

        # Log
        summary = self.engine.get_summary(prices)
        self._log_tick(summary, indicators, entry_signals)

        if self._tick_count % 10 == 0:
            self._print_tick_summary(summary, daily_pnl)

    def _log_tick(self, summary, indicators, signals):
        record = {
            "tick": self._tick_count,
            "time": datetime.now(timezone.utc).isoformat(),
            **summary,
            "indicators": {s: {k: v for k, v in ind.items()} for s, ind in indicators.items()},
            "signals": {s: {"action": sig["action"], "confidence": sig["confidence"], "reasons": sig["reasons"]} for s, sig in signals.items()},
        }
        with open(Path(self.run_dir) / "equity.jsonl", "a") as f:
            f.write(json.dumps(record, default=str) + "\n")

        state = {
            "tick": self._tick_count,
            "equity": summary["equity"],
            "total_pnl": summary["total_pnl"],
            "positions": summary["positions"],
            "trades_today": self._trade_count_today,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(Path(self.run_dir) / "state.json", "w") as f:
            json.dump(state, f, indent=2)

    def _print_tick_summary(self, summary, daily_pnl):
        logger.info(
            "Tick %d | Equity: $%.4f | PnL: $%.4f | Trades: %d | WR: %.0f%% | Open: %d | Today: %d",
            self._tick_count, summary["equity"], summary["total_pnl"],
            summary["total_trades"], summary["win_rate"], summary["open_positions"],
            self._trade_count_today,
        )
        for sym, pos in summary.get("positions", {}).items():
            logger.info("  %s [%s] $%.2f @ %.6f notional=$%.2f", sym, pos["dir"], pos["notional"] / self.leverage, pos["entry"], pos["notional"])

    def _print_final_summary(self):
        prices = self.fetcher.fetch_latest()
        summary = self.engine.get_summary(prices)
        logger.info("=" * 60)
        logger.info("FINAL SUMMARY — %d ticks", self._tick_count)
        logger.info("Equity: $%.4f (started: $%.2f)", summary["equity"], self.initial_cash)
        logger.info("Total PnL: $%.4f", summary["total_pnl"])
        logger.info("Total trades: %d | Win rate: %.1f%%", summary["total_trades"], summary["win_rate"])
        logger.info("=" * 60)

        with open(Path(self.run_dir) / "trades.jsonl", "w") as f:
            for t in self.engine.trade_log:
                f.write(json.dumps(t, default=str) + "\n")


# ── CLI Entry Point ────────────────────────────────────────────────────


def main():
    Path("./paper_runs/aggressive_forex").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("./paper_runs/aggressive_forex/trader.log", mode="a"),
        ],
    )

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    # Most volatile forex pairs
    default_pairs = ["MXN", "BRL", "ZAR", "NZD", "HUF", "MYR", "PLN"]

    config = {
        "pairs": default_pairs,
        "tick_interval_seconds": int(os.environ.get("TICK_INTERVAL", "30")),
        "initial_cash": float(os.environ.get("INITIAL_CASH", "10")),
        "leverage": float(os.environ.get("LEVERAGE", "10")),
        "daily_profit_target": float(os.environ.get("DAILY_TARGET", "5")),
        "max_positions": 5,
        "position_pct": 0.60,
        "run_dir": os.environ.get("RUN_DIR", "./paper_runs/aggressive_forex"),
    }

    pairs_env = os.environ.get("FOREX_PAIRS")
    if pairs_env:
        config["pairs"] = [p.strip().upper() for p in pairs_env.split(",")]

    orchestrator = AggressiveForexOrchestrator(config)
    orchestrator.run()


if __name__ == "__main__":
    main()
