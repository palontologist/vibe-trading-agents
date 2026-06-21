"""Aggressive Perps Paper Trader — Focused on BTC/ETH/SOL.

Strategy: Quick in/out trades on major crypto with 20x leverage.
- Wait for clear setups (RSI + MA alignment)
- Enter with 90% of available cash
- Take profit at 1-2% move (20-40% with leverage)
- Cut losses fast at 0.5% (10% with leverage)
- Max hold 2 hours
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import requests

logger = logging.getLogger(__name__)

INITIAL_CASH = float(os.environ.get("INITIAL_CASH", "10"))
LEVERAGE = 20
MAX_POSITION_PCT = 0.90
MAX_POSITIONS = 3  # Focus: BTC, ETH, SOL only
HARD_STOP_PCT = 0.005  # 0.5% (10% with leverage)
TAKE_PROFIT_PCT = 0.015  # 1.5% (30% with leverage)
TRAILING_STOP_PCT = 0.008  # 0.8% (16% with leverage)
MAX_HOLD_SECONDS = 2 * 3600  # 2 hours
TICK_INTERVAL = 10  # seconds

ASSETS = ["BTC", "ETH", "SOL"]

RUN_DIR = Path("./paper_runs/perps_v2")
RUN_DIR.mkdir(parents=True, exist_ok=True)


class PerpFetcher:
    """Fetch live perp data from Hyperliquid."""

    HL_URL = "https://api.hyperliquid.xyz/info"

    def get_prices(self, symbols: List[str]) -> Dict[str, float]:
        try:
            resp = requests.post(self.HL_URL, json={"type": "allMids"}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return {s: float(data[s]) for s in symbols if s in data}
        except Exception as e:
            logger.warning(f"Price fetch failed: {e}")
            return {}

    def get_candles(self, symbol: str, interval: str = "5m", limit: int = 50) -> Optional[Dict]:
        try:
            end_time = int(time.time() * 1000)
            interval_ms = {"1m": 60000, "5m": 300000, "15m": 900000, "1h": 3600000}
            start_time = end_time - (limit * interval_ms.get(interval, 300000))

            resp = requests.post(
                self.HL_URL,
                json={"type": "candleSnapshot", "req": {"coin": symbol, "interval": interval, "startTime": start_time, "endTime": end_time}},
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
        except Exception:
            return None

    def get_fear_greed(self) -> int:
        try:
            resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
            return int(resp.json().get("data", [{}])[0].get("value", 50))
        except Exception:
            return 50


class SmartSignals:
    """Generate high-conviction entry signals."""

    def __init__(self):
        self.fg = 50
        self.fg_history = []

    def update_fg(self, fg: int):
        self.fg = fg
        self.fg_history.append(fg)
        if len(self.fg_history) > 50:
            self.fg_history = self.fg_history[-50:]

    def analyze(self, symbol: str, price: float, candles: Optional[Dict]) -> Dict[str, Any]:
        """High-conviction signal generation."""
        if not candles or len(candles["close"]) < 30:
            return {"action": "FLAT", "confidence": 0, "reasons": ["Insufficient data"]}

        closes = np.array(candles["close"])
        highs = np.array(candles["high"])
        lows = np.array(candles["lows"] if "lows" in candles else candles["low"])
        volumes = np.array(candles["volume"])

        score = 0.0
        reasons = []
        confidence = 50

        # RSI
        deltas = np.diff(closes[-15:])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses) if np.mean(losses) > 0 else 1
        rsi = 100 - (100 / (1 + avg_gain / avg_loss))

        # Moving Averages
        ma5 = np.mean(closes[-5:])
        ma10 = np.mean(closes[-10:])
        ma20 = np.mean(closes[-20:])

        # Price position
        pct_from_high = (max(highs[-20:]) - price) / max(highs[-20:]) * 100
        pct_from_low = (price - min(lows[-20:])) / min(lows[-20:]) * 100

        # Volume
        avg_vol = np.mean(volumes[-20:])
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1

        # ── BULLISH SIGNALS ──
        # 1. RSI oversold bounce
        if rsi < 35:
            score += 0.3
            confidence += 15
            reasons.append(f"RSI={rsi:.0f} oversold bounce")

        # 2. MA alignment (bullish)
        if price > ma5 > ma10 > ma20:
            score += 0.3
            confidence += 20
            reasons.append("Strong bullish MA alignment")
        elif ma5 > ma10:
            score += 0.15
            confidence += 10
            reasons.append("MA5>MA10 uptrend")

        # 3. Near support (bounce play)
        if pct_from_low < 3 and pct_from_low > 0:
            score += 0.2
            confidence += 10
            reasons.append(f"Near 20-bar low ({pct_from_low:.1f}% from low)")

        # 4. Volume surge
        if vol_ratio > 1.5:
            confidence += 10
            reasons.append(f"Volume {vol_ratio:.1f}x avg")

        # 5. F&G contrarian
        if self.fg < 30:
            score += 0.15
            confidence += 8
            reasons.append(f"F&G={self.fg} fear → contrarian")

        # ── BEARISH SIGNALS ──
        if rsi > 65:
            score -= 0.3
            confidence += 15
            reasons.append(f"RSI={rsi:.0f} overbought")

        if price < ma5 < ma10 < ma20:
            score -= 0.3
            confidence += 20
            reasons.append("Strong bearish MA alignment")
        elif ma5 < ma10:
            score -= 0.15
            confidence += 10
            reasons.append("MA5<MA10 downtrend")

        if pct_from_high < 3 and pct_from_high > 0:
            score -= 0.2
            confidence += 10
            reasons.append(f"Near 20-bar high ({pct_from_high:.1f}% from high)")

        if self.fg > 70:
            score -= 0.15
            confidence += 8
            reasons.append(f"F&G={self.fg} greed → contrarian")

        # Determine action
        action = "FLAT"
        if abs(score) >= 0.3 and confidence >= 60:
            action = "LONG" if score > 0 else "SHORT"

        return {
            "action": action,
            "score": float(np.clip(score, -1, 1)),
            "confidence": min(95, confidence),
            "reasons": reasons,
            "rsi": rsi,
            "ma5": ma5,
            "ma10": ma10,
        }


class PerpsPortfolio:
    """Paper portfolio for perps trading."""

    def __init__(self, initial_cash: float = 10.0):
        self.cash = initial_cash
        self.initial_cash = initial_cash
        self.positions: Dict[str, Dict] = {}
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
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
            except Exception:
                pass

    def _save_state(self):
        state = {
            "cash": self.cash,
            "positions": self.positions,
            "equity": self._calc_equity({}),
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": self.total_pnl,
            "win_rate": self.wins / max(1, self.total_trades),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._state_path().write_text(json.dumps(state, indent=2))

    def _calc_equity(self, prices: Dict[str, float]) -> float:
        equity = self.cash
        for sym, pos in self.positions.items():
            cur = prices.get(sym, pos["entry_price"])
            if pos["side"] == "LONG":
                pnl = (cur - pos["entry_price"]) / pos["entry_price"] * pos["notional"]
            else:
                pnl = (pos["entry_price"] - cur) / pos["entry_price"] * pos["notional"]
            equity += pnl
        return equity

    def open_position(self, symbol: str, side: str, price: float, confidence: int) -> bool:
        if len(self.positions) >= MAX_POSITIONS:
            return False
        if symbol in self.positions:
            return False

        notional = self.cash * MAX_POSITION_PCT
        if notional < 5:
            return False

        margin = notional / LEVERAGE
        fee = notional * 0.0005

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
        }
        self.cash -= (margin + fee)
        self.total_trades += 1
        self._save_state()
        logger.info(f"OPEN {side} {symbol}: ${notional:.2f} @ ${price:,.2f} ({LEVERAGE}x, conf={confidence}%)")
        return True

    def close_position(self, symbol: str, price: float, reason: str) -> Optional[float]:
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

    def check_exits(self, prices: Dict[str, float]) -> List[tuple]:
        to_close = []
        now = time.time()

        for symbol, pos in self.positions.items():
            cur = prices.get(symbol, pos["entry_price"])
            entry = pos["entry_price"]
            age = now - pos["entry_time"]

            # Update trailing
            if pos["side"] == "LONG":
                pos["highest_price"] = max(pos["highest_price"], cur)
                pnl_pct = (cur - entry) / entry
                trail_drop = (pos["highest_price"] - cur) / pos["highest_price"]
            else:
                pos["lowest_price"] = min(pos["lowest_price"], cur)
                pnl_pct = (entry - cur) / entry
                trail_drop = (cur - pos["lowest_price"]) / pos["lowest_price"]

            # Take profit
            if pnl_pct >= TAKE_PROFIT_PCT:
                to_close.append((symbol, cur, f"Take profit ({pnl_pct*100:.2f}%)"))
            # Hard stop
            elif pnl_pct <= -HARD_STOP_PCT:
                to_close.append((symbol, cur, f"Hard stop ({pnl_pct*100:.2f}%)"))
            # Trailing stop (only if in profit)
            elif trail_drop >= TRAILING_STOP_PCT and pnl_pct > 0:
                to_close.append((symbol, cur, f"Trailing stop ({trail_drop*100:.2f}% from peak)"))
            # Max hold
            elif age >= MAX_HOLD_SECONDS:
                to_close.append((symbol, cur, f"Max hold ({age/3600:.1f}h)"))

        return to_close


class PerpsOrchestrator:
    """Main loop for focused perps trading."""

    def __init__(self):
        self.fetcher = PerpFetcher()
        self.signals = SmartSignals()
        self.portfolio = PerpsPortfolio(INITIAL_CASH)
        self._tick_count = 0
        self._last_fg_update = 0
        self._running = True
        self._equity_log = RUN_DIR / "equity.jsonl"

    def run(self):
        logger.info("=" * 60)
        logger.info("PERPS TRADER V2 — FOCUSED ON BTC/ETH/SOL")
        logger.info(f"Capital: ${INITIAL_CASH} | Leverage: {LEVERAGE}x")
        logger.info(f"TP: {TAKE_PROFIT_PCT*100}% | SL: {HARD_STOP_PCT*100}% | Trail: {TRAILING_STOP_PCT*100}%")
        logger.info(f"Max hold: {MAX_HOLD_SECONDS/3600}h | Max positions: {MAX_POSITIONS}")
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

        # Update F&G every 5 min
        if now - self._last_fg_update > 300:
            fg = self.fetcher.get_fear_greed()
            self.signals.update_fg(fg)
            self._last_fg_update = now
            logger.info(f"F&G: {fg}")

        # Fetch prices
        prices = self.fetcher.get_prices(ASSETS)
        if not prices:
            return

        # Check exits first
        exits = self.portfolio.check_exits(prices)
        for symbol, price, reason in exits:
            self.portfolio.close_position(symbol, price, reason)

        # Generate signals for each asset
        for symbol in ASSETS:
            if symbol in self.portfolio.positions:
                continue

            candles = self.fetcher.get_candles(symbol, "5m", 50)
            signal = self.signals.analyze(symbol, prices[symbol], candles)

            if signal["action"] in ("LONG", "SHORT") and signal["confidence"] >= 60:
                self.portfolio.open_position(symbol, signal["action"], prices[symbol], signal["confidence"])

        # Log and print
        equity = self.portfolio._calc_equity(prices)
        self._log_equity(equity, prices)

        if self._tick_count % 15 == 0:
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
        }
        with open(self._equity_log, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _print_progress(self, equity: float, prices: Dict[str, float]):
        ret = (equity - INITIAL_CASH) / INITIAL_CASH * 100
        logger.info(
            f"Tick {self._tick_count}: Equity=${equity:.4f} ({ret:+.2f}%) "
            f"Cash=${self.portfolio.cash:.2f} Trades={self.portfolio.total_trades} "
            f"WR={self.portfolio.wins}/{self.portfolio.total_trades} "
            f"PnL=${self.portfolio.total_pnl:+.4f}"
        )
        for s, p in self.portfolio.positions.items():
            cur = prices.get(s, p["entry_price"])
            pnl = (cur - p["entry_price"]) / p["entry_price"] * 100
            logger.info(f"  {s}: {p['side']} @ ${p['entry_price']:,.2f} → ${cur:,.2f} ({pnl:+.3f}%)")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(RUN_DIR / "stdout.log")),
        ],
    )
    orchestrator = PerpsOrchestrator()
    orchestrator.run()


if __name__ == "__main__":
    main()
