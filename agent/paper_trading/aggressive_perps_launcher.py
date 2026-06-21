"""Aggressive Perps Paper Trader — $10 → $100 in 24 Hours.

Targets high-volatility crypto perps with 20x leverage, 90% position sizing,
and 2-3 hour max hold time. Designed for maximum short-term profit.

Strategy:
  - Pick top 5 high-beta assets (meme coins, small caps)
  - 20x leverage, 90% equity per position
  - Trailing stop 2%, hard stop 5%
  - Max hold 3 hours (force rotation)
  - F&G contrarian + momentum + breakout signals
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import requests

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────

INITIAL_CASH = float(os.environ.get("INITIAL_CASH", "10"))
LEVERAGE = 20
MAX_POSITION_PCT = 0.90  # 90% per trade
MAX_POSITIONS = 5
HARD_STOP_PCT = 0.05  # 5%
TRAILING_STOP_PCT = 0.02  # 2%
MAX_HOLD_SECONDS = 3 * 3600  # 3 hours
TICK_INTERVAL = 10  # seconds

# High-volatility perps (meme coins, small caps)
HIGH_VOL_ASSETS = [
    "DOGE", "WIF", "PEPE", "SHIB", "FLOKI", "BONK",
    "TURBO", "MEME", "MOG", "POPCAT", "MEW", "BRETT",
    "ANDY", "TOSHI", "MYRO", "BOME", "WIF", "JUP",
]

RUN_DIR = Path("./paper_runs/perps_aggressive")
RUN_DIR.mkdir(parents=True, exist_ok=True)


# ── Data ──────────────────────────────────────────────────────────────────

class PerpFetcher:
    """Fetch live perp data from Hyperliquid + Binance."""

    HL_URL = "https://api.hyperliquid.xyz/info"

    def get_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Get mid prices from Hyperliquid."""
        try:
            resp = requests.post(self.HL_URL, json={"type": "allMids"}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            prices = {}
            for sym in symbols:
                if sym in data:
                    prices[sym] = float(data[sym])
                elif f"{sym}-USDC" in data:
                    prices[sym] = float(data[f"{sym}-USDC"])
            return prices
        except Exception as e:
            logger.warning(f"Hyperliquid price fetch failed: {e}")
            return {}

    def get_candles(self, symbol: str, interval: str = "5m", limit: int = 100) -> Optional[Dict]:
        """Get OHLCV candles from Hyperliquid."""
        try:
            end_time = int(time.time() * 1000)
            interval_ms = {"1m": 60000, "5m": 300000, "15m": 900000, "1h": 3600000}
            start_time = end_time - (limit * interval_ms.get(interval, 300000))

            resp = requests.post(
                self.HL_URL,
                json={
                    "type": "candleSnapshot",
                    "req": {
                        "coin": symbol,
                        "interval": interval,
                        "startTime": start_time,
                        "endTime": end_time,
                    }
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                return None
            return {
                "open": [float(d["o"]) for d in data],
                "high": [float(d["h"]) for d in data],
                "low": [float(d["l"]) for d in data],
                "close": [float(d["c"]) for d in data],
                "volume": [float(d["v"]) for d in data],
            }
        except Exception as e:
            logger.debug(f"Candle fetch failed for {symbol}: {e}")
            return None

    def get_fear_greed(self) -> int:
        """Get Fear & Greed Index."""
        try:
            resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
            resp.raise_for_status()
            return int(resp.json().get("data", [{}])[0].get("value", 50))
        except Exception:
            return 50


# ── Signals ───────────────────────────────────────────────────────────────

class AggressiveSignals:
    """Fast signal generation for high-vol perps."""

    def __init__(self):
        self.fg_history: List[int] = []

    def update_fg(self, fg: int):
        self.fg_history.append(fg)
        if len(self.fg_history) > 50:
            self.fg_history = self.fg_history[-50:]

    def generate_signal(
        self, symbol: str, price: float, candles: Optional[Dict], fg: int
    ) -> Dict[str, Any]:
        """Generate trading signal for a single asset."""
        score = 0.0
        reasons = []
        confidence = 50

        # 1. F&G Contrarian (weight: 30%)
        if fg < 25:
            score += 0.3
            confidence += 15
            reasons.append(f"F&G={fg} extreme fear → contrarian LONG")
        elif fg < 40:
            score += 0.15
            confidence += 8
            reasons.append(f"F&G={fg} fear zone")
        elif fg > 75:
            score -= 0.3
            confidence += 15
            reasons.append(f"F&G={fg} extreme greed → contrarian SHORT")
        elif fg > 60:
            score -= 0.15
            confidence += 8
            reasons.append(f"F&G={fg} greed zone")

        # 2. Momentum (weight: 35%)
        if candles and len(candles["close"]) >= 20:
            closes = candles["close"]
            ma5 = np.mean(closes[-5:])
            ma10 = np.mean(closes[-10:])
            ma20 = np.mean(closes[-20:])

            # RSI
            deltas = np.diff(closes[-15:])
            gains = np.where(deltas > 0, deltas, 0)
            losses = np.where(deltas < 0, -deltas, 0)
            avg_gain = np.mean(gains) if len(gains) > 0 else 0
            avg_loss = np.mean(losses) if len(losses) > 0 else 1
            rsi = 100 - (100 / (1 + avg_gain / max(avg_loss, 0.001)))

            if price > ma5 > ma10 > ma20:
                score += 0.35
                confidence += 20
                reasons.append(f"Strong uptrend MA5>{ma5:.4f}>MA10>{ma10:.4f}>MA20")
            elif ma5 > ma10:
                score += 0.15
                confidence += 10
                reasons.append("MA5>MA10 uptrend")
            elif price < ma5 < ma10 < ma20:
                score -= 0.35
                confidence += 20
                reasons.append("Strong downtrend")
            elif ma5 < ma10:
                score -= 0.15
                confidence += 10
                reasons.append("MA5<MA10 downtrend")

            if rsi < 30:
                score += 0.2
                confidence += 12
                reasons.append(f"RSI={rsi:.0f} oversold")
            elif rsi > 70:
                score -= 0.2
                confidence += 12
                reasons.append(f"RSI={rsi:.0f} overbought")

        # 3. Breakout (weight: 20%)
        if candles and len(candles["close"]) >= 20:
            closes = candles["close"]
            highs = candles["high"]
            lows = candles["low"]

            # Recent high/low
            recent_high = max(highs[-20:])
            recent_low = min(lows[-20:])
            rng = recent_high - recent_low

            if price > recent_high - rng * 0.1:
                score += 0.2
                confidence += 10
                reasons.append(f"Near 20-bar high ${recent_high:.4f}")
            elif price < recent_low + rng * 0.1:
                score -= 0.2
                confidence += 10
                reasons.append(f"Near 20-bar low ${recent_low:.4f}")

        # 4. Volume surge (weight: 15%)
        if candles and len(candles["volume"]) >= 20:
            vols = candles["volume"]
            avg_vol = np.mean(vols[-20:])
            cur_vol = vols[-1]
            vol_ratio = cur_vol / max(avg_vol, 0.001)

            if vol_ratio > 2.0:
                confidence += 15
                reasons.append(f"Volume surge {vol_ratio:.1f}x")
            elif vol_ratio > 1.5:
                confidence += 8
                reasons.append(f"Volume {vol_ratio:.1f}x")

        # Determine action
        action = "FLAT"
        if abs(score) >= 0.25 and confidence >= 55:
            action = "LONG" if score > 0 else "SHORT"

        return {
            "symbol": symbol,
            "action": action,
            "score": float(np.clip(score, -1, 1)),
            "confidence": min(95, confidence),
            "reasons": reasons,
        }


# ── Portfolio ─────────────────────────────────────────────────────────────

class AggressivePortfolio:
    """Paper portfolio for aggressive perps trading."""

    def __init__(self, initial_cash: float = 10.0):
        self.cash = initial_cash
        self.initial_cash = initial_cash
        self.positions: Dict[str, Dict] = {}
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.peak_equity = initial_cash
        self.max_drawdown = 0.0
        self._load_state()

    def _state_path(self):
        return RUN_DIR / "state.json"

    def _load_state(self):
        p = self._state_path()
        if p.exists():
            try:
                d = json.loads(p.read_text())
                self.cash = d.get("cash", self.initial_cash)
                self.positions = d.get("positions", {})
                self.total_trades = d.get("total_trades", 0)
                self.wins = d.get("wins", 0)
                self.losses = d.get("losses", 0)
                self.total_pnl = d.get("total_pnl", 0.0)
                self.peak_equity = d.get("peak_equity", self.initial_cash)
                self.max_drawdown = d.get("max_drawdown", 0.0)
            except Exception:
                pass

    def _save_state(self):
        equity = self._calc_equity({})
        self.peak_equity = max(self.peak_equity, equity)
        dd = (self.peak_equity - equity) / self.peak_equity if self.peak_equity > 0 else 0
        self.max_drawdown = max(self.max_drawdown, dd)

        state = {
            "cash": self.cash,
            "positions": self.positions,
            "equity": equity,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": self.total_pnl,
            "peak_equity": self.peak_equity,
            "max_drawdown": self.max_drawdown,
            "win_rate": self.wins / max(1, self.total_trades),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._state_path().write_text(json.dumps(state, indent=2))

    def _calc_equity(self, prices: Dict[str, float]) -> float:
        equity = self.cash
        for sym, pos in self.positions.items():
            cur_price = prices.get(sym, pos["entry_price"])
            if pos["side"] == "LONG":
                pnl = (cur_price - pos["entry_price"]) / pos["entry_price"] * pos["notional"]
            else:
                pnl = (pos["entry_price"] - cur_price) / pos["entry_price"] * pos["notional"]
            equity += pnl
        return equity

    def open_position(self, symbol: str, side: str, price: float, confidence: int) -> bool:
        """Open a new position."""
        if len(self.positions) >= MAX_POSITIONS:
            return False
        if symbol in self.positions:
            return False

        notional = self.cash * MAX_POSITION_PCT
        if notional < 5:
            return False

        margin = notional / LEVERAGE
        fee = notional * 0.0005  # 0.05% taker

        if margin + fee > self.cash:
            return False

        self.positions[symbol] = {
            "side": side,
            "entry_price": price,
            "notional": notional,
            "margin": margin,
            "leverage": LEVERAGE,
            "entry_time": time.time(),
            "highest_price": price,
            "lowest_price": price,
            "stop_loss": HARD_STOP_PCT,
            "trailing_stop": TRAILING_STOP_PCT,
        }
        self.cash -= (margin + fee)
        self.total_trades += 1
        self._save_state()
        logger.info(f"OPEN {side} {symbol}: notional=${notional:.2f} @ ${price:.4f} ({LEVERAGE}x)")
        return True

    def close_position(self, symbol: str, price: float, reason: str) -> Optional[float]:
        """Close a position and return PnL."""
        if symbol not in self.positions:
            return None

        pos = self.positions[symbol]
        entry = pos["entry_price"]
        notional = pos["notional"]

        if pos["side"] == "LONG":
            pnl = (price - entry) / entry * notional
        else:
            pnl = (entry - price) / entry * notional

        fee = notional * 0.0005
        net_pnl = pnl - fee

        self.cash += pos["margin"] + net_pnl
        self.total_pnl += net_pnl
        if net_pnl > 0:
            self.wins += 1
        else:
            self.losses += 1

        del self.positions[symbol]
        self._save_state()
        logger.info(f"CLOSE {symbol}: PnL=${net_pnl:+.4f} ({reason})")
        return net_pnl

    def check_exits(self, prices: Dict[str, float]) -> List[str]:
        """Check all positions for exit conditions."""
        to_close = []
        now = time.time()

        for symbol, pos in self.positions.items():
            cur_price = prices.get(symbol, pos["entry_price"])
            entry = pos["entry_price"]
            age = now - pos["entry_time"]

            # Update trailing
            if pos["side"] == "LONG":
                pos["highest_price"] = max(pos["highest_price"], cur_price)
                pnl_pct = (cur_price - entry) / entry
                trail_drop = (pos["highest_price"] - cur_price) / pos["highest_price"]
            else:
                pos["lowest_price"] = min(pos["lowest_price"], cur_price)
                pnl_pct = (entry - cur_price) / entry
                trail_drop = (cur_price - pos["lowest_price"]) / pos["lowest_price"]

            # Hard stop
            if pnl_pct <= -HARD_STOP_PCT:
                to_close.append((symbol, cur_price, f"Hard stop ({pnl_pct*100:.2f}%)"))
            # Trailing stop
            elif trail_drop >= TRAILING_STOP_PCT and pnl_pct > 0:
                to_close.append((symbol, cur_price, f"Trailing stop (dropped {trail_drop*100:.2f}% from peak)"))
            # Max hold
            elif age >= MAX_HOLD_SECONDS:
                to_close.append((symbol, cur_price, f"Max hold ({age/3600:.1f}h)"))

        return to_close


# ── Orchestrator ──────────────────────────────────────────────────────────

class AggressivePerpsOrchestrator:
    """Main loop for aggressive perps paper trading."""

    def __init__(self):
        self.fetcher = PerpFetcher()
        self.signals = AggressiveSignals()
        self.portfolio = AggressivePortfolio(INITIAL_CASH)
        self._tick_count = 0
        self._last_fg_update = 0
        self._running = True

        # Equity log
        self._equity_log = RUN_DIR / "equity.jsonl"

    def run(self):
        logger.info("=" * 60)
        logger.info("AGGRESSIVE PERPS TRADER STARTING")
        logger.info(f"Capital: ${INITIAL_CASH} | Leverage: {LEVERAGE}x | Max positions: {MAX_POSITIONS}")
        logger.info(f"Hard stop: {HARD_STOP_PCT*100}% | Trailing: {TRAILING_STOP_PCT*100}% | Max hold: {MAX_HOLD_SECONDS/3600}h")
        logger.info("=" * 60)

        while self._running:
            try:
                self._tick()
                time.sleep(TICK_INTERVAL)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Tick error: {e}", exc_info=True)
                time.sleep(5)

        logger.info("Stopped.")

    def _tick(self):
        self._tick_count += 1
        now = time.time()

        # Update F&G
        if now - self._last_fg_update > 300:
            fg = self.fetcher.get_fear_greed()
            self.signals.update_fg(fg)
            self._last_fg_update = now
            logger.info(f"F&G: {fg}")

        # Fetch prices
        prices = self.fetcher.get_prices(HIGH_VOL_ASSETS)
        if not prices:
            return

        # Update equity
        equity = self.portfolio._calc_equity(prices)

        # Check exits
        exits = self.portfolio.check_exits(prices)
        for symbol, price, reason in exits:
            self.portfolio.close_position(symbol, price, reason)

        # Generate signals
        fg = self.signals.fg_history[-1] if self.signals.fg_history else 50
        for symbol in prices:
            if symbol in self.portfolio.positions:
                continue

            candles = self.fetcher.get_candles(symbol, "5m", 100)
            signal = self.signals.generate_signal(symbol, prices[symbol], candles, fg)

            if signal["action"] in ("LONG", "SHORT") and signal["confidence"] >= 55:
                self.portfolio.open_position(symbol, signal["action"], prices[symbol], signal["confidence"])

        # Log equity
        equity = self.portfolio._calc_equity(prices)
        self._log_equity(equity, prices)

        # Print progress
        if self._tick_count % 20 == 0:
            self._print_progress(equity, prices)

    def _log_equity(self, equity: float, prices: Dict[str, float]):
        entry = {
            "tick": self._tick_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "equity": equity,
            "cash": self.portfolio.cash,
            "positions": {
                s: {
                    "side": p["side"],
                    "entry": p["entry_price"],
                    "current": prices.get(s, p["entry_price"]),
                    "pnl": ((prices.get(s, p["entry_price"]) - p["entry_price"]) / p["entry_price"] * p["notional"])
                    if p["side"] == "LONG"
                    else ((p["entry_price"] - prices.get(s, p["entry_price"])) / p["entry_price"] * p["notional"]),
                    "age_s": time.time() - p["entry_time"],
                }
                for s, p in self.portfolio.positions.items()
            },
            "total_pnl": self.portfolio.total_pnl,
            "total_trades": self.portfolio.total_trades,
            "win_rate": self.portfolio.wins / max(1, self.portfolio.total_trades),
            "max_drawdown": self.portfolio.max_drawdown,
        }
        with open(self._equity_log, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _print_progress(self, equity: float, prices: Dict[str, float]):
        ret = (equity - INITIAL_CASH) / INITIAL_CASH * 100
        pos_str = " | ".join(
            f"{s}:{p['side'][:1]}@{p['entry_price']:.4f}"
            for s, p in self.portfolio.positions.items()
        )
        logger.info(
            f"Tick {self._tick_count}: Equity=${equity:.4f} ({ret:+.2f}%) "
            f"Cash=${self.portfolio.cash:.2f} Trades={self.portfolio.total_trades} "
            f"WR={self.portfolio.wins}/{self.portfolio.total_trades} "
            f"DD={self.portfolio.max_drawdown*100:.2f}%"
        )
        if pos_str:
            logger.info(f"  Positions: {pos_str}")


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(RUN_DIR / "stdout.log")),
        ],
    )
    orchestrator = AggressivePerpsOrchestrator()
    orchestrator.run()


if __name__ == "__main__":
    main()
