"""SNIPER V3 — Momentum capture with velocity detection.

Strategy: $10 -> $100 in 6 hours
- Go ALL-IN on 1 high-conviction trade
- 20x leverage on highest volatility asset
- Capture momentum peaks (1-3% moves)
- Compound immediately after each win
- Rotate to next asset after each trade
- Max 3 losses before pause

Key insight: Don't wait for perfect setup.
Capture what the market gives you RIGHT NOW.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────

INITIAL_CASH = float(os.environ.get("INITIAL_CASH", "10"))
TARGET = 100.0
LEVERAGE = 20
POSITION_PCT = 0.95  # 95% per trade (almost all-in)
STOP_LOSS = 0.02  # 2% = 40% with leverage
MAX_HOLD = 600  # 10 min max hold (fast rotation)
TICK_INTERVAL = 2  # seconds (fast detection)

# MOMENTUM EXIT - capture peaks
PEAK_DROP_EXIT = 0.35  # Close when PnL drops 35% from peak
MIN_PROFIT_EXIT = 0.003  # Close if profit > 0.3% after 3 min
STALE_EXIT_TIMEOUT = 60  # Close if no new high in 60 seconds

# ENTRY FILTERS - only trade when conditions are right
MIN_VELOCITY = 0.0001  # Minimum 0.01% price movement (lower to trade in flat markets)
MIN_VOLATILITY = 0.0001  # Minimum 0.01% volatility (lower to trade in flat markets)
MAX_SPREAD = 0.001  # Maximum 0.1% spread

# Assets ranked by volatility (highest first) - JUP is 5x more volatile than WIF
ASSETS = ["JUP", "TURBO", "WIF", "SOL", "DOGE", "RENDER"]

RUN_DIR = Path("./paper_runs/sniper")
RUN_DIR.mkdir(parents=True, exist_ok=True)


class SniperFetcher:
    """Fast data fetching with velocity tracking."""

    HL_URL = "https://api.hyperliquid.xyz/info"

    def __init__(self):
        self._price_history: Dict[str, List[float]] = {}
        self._last_fetch: Dict[str, float] = {}

    def get_prices(self, symbols: list) -> Dict[str, float]:
        try:
            resp = requests.post(self.HL_URL, json={"type": "allMids"}, timeout=5)
            data = resp.json()
            prices = {s: float(data[s]) for s in symbols if s in data}
            
            # Update price history
            now = time.time()
            for sym, price in prices.items():
                if sym not in self._price_history:
                    self._price_history[sym] = []
                self._price_history[sym].append(price)
                # Keep last 30 prices (60 seconds at 2s ticks)
                if len(self._price_history[sym]) > 30:
                    self._price_history[sym] = self._price_history[sym][-30:]
                self._last_fetch[sym] = now
            
            return prices
        except Exception:
            return {}

    def get_velocity(self, symbol: str) -> float:
        """Calculate price velocity (rate of change)."""
        history = self._price_history.get(symbol, [])
        if len(history) < 5:
            return 0.0
        
        # Calculate velocity over last 5 ticks (10 seconds)
        recent = history[-5:]
        if len(recent) < 2:
            return 0.0
        
        # Velocity = (current - oldest) / oldest
        velocity = (recent[-1] - recent[0]) / recent[0]
        return velocity

    def get_volatility(self, symbol: str) -> float:
        """Calculate recent volatility."""
        history = self._price_history.get(symbol, [])
        if len(history) < 10:
            return 0.0
        
        # Calculate volatility over last 10 ticks (20 seconds)
        returns = np.diff(history[-10:]) / history[-10:-1]
        return float(np.std(returns)) if len(returns) > 0 else 0.0

    def get_candles(self, symbol: str, interval: str = "5m", limit: int = 20) -> Optional[Dict]:
        try:
            end_time = int(time.time() * 1000)
            interval_ms = {"1m": 60000, "5m": 300000}
            start_time = end_time - (limit * interval_ms.get(interval, 300000))
            resp = requests.post(
                self.HL_URL,
                json={"type": "candleSnapshot", "req": {"coin": symbol, "interval": interval, "startTime": start_time, "endTime": end_time}},
                timeout=10,
            )
            data = resp.json()
            if not data:
                return None
            return {
                "close": [float(d["c"]) for d in data],
                "high": [float(d["h"]) for d in data],
                "low": [float(d["l"]) for d in data],
                "volume": [float(d["v"]) for d in data],
            }
        except Exception:
            return None

    def get_fear_greed(self) -> int:
        try:
            resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
            return int(resp.json().get("data", [{}])[0].get("value", 50))
        except Exception:
            return 50


class SniperSignal:
    """High-conviction signal generation with velocity."""

    def __init__(self):
        self.fg = 50

    def find_best_setup(self, prices: Dict[str, float], fetcher: SniperFetcher) -> Optional[Dict]:
        """Find the single best trade setup with velocity."""
        best = None
        best_score = 0

        for symbol in ASSETS:
            price = prices.get(symbol)
            if not price:
                continue

            # Check velocity - must be moving
            velocity = fetcher.get_velocity(symbol)
            volatility = fetcher.get_volatility(symbol)
            
            # Skip if no momentum
            if abs(velocity) < MIN_VELOCITY:
                continue
            
            # Skip if low volatility
            if volatility < MIN_VOLATILITY:
                continue

            candles = fetcher.get_candles(symbol, "5m", 20)
            if not candles or len(candles["close"]) < 15:
                continue

            score, reasons, confidence = self._analyze(symbol, price, candles, velocity, volatility)

            if score > best_score and confidence >= 50:
                best_score = score
                best = {
                    "symbol": symbol,
                    "action": "LONG" if velocity > 0 else "SHORT",
                    "score": score,
                    "confidence": confidence,
                    "reasons": reasons,
                    "price": price,
                    "velocity": velocity,
                    "volatility": volatility,
                }

        return best

    def _analyze(self, symbol: str, price: float, candles: Dict, velocity: float, volatility: float) -> tuple:
        closes = np.array(candles["close"])
        highs = np.array(candles["high"])
        lows = np.array(candles["low"])

        score = 0.0
        reasons = []
        confidence = 50

        # RSI
        deltas = np.diff(closes[-15:])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains) if len(gains) > 0 else 0
        avg_loss = np.mean(losses) if np.mean(losses) > 0 else 1
        rsi = 100 - (100 / (1 + avg_gain / avg_loss))

        # MAs
        ma5 = np.mean(closes[-5:])
        ma10 = np.mean(closes[-10:])

        # ── LONG SETUP (velocity > 0) ──
        if velocity > 0:
            score += 0.3
            confidence += 15
            reasons.append(f"Velocity {velocity*100:.3f}%")
            
            if rsi < 40:
                score += 0.2
                confidence += 10
                reasons.append(f"RSI={rsi:.0f} oversold")
            
            if ma5 > ma10:
                score += 0.15
                confidence += 10
                reasons.append("MA5>MA10")
            
            if volatility > 0.001:
                score += 0.15
                confidence += 10
                reasons.append(f"Vol={volatility*100:.3f}%")
        
        # ── SHORT SETUP (velocity < 0) ──
        else:
            score -= 0.3
            confidence += 15
            reasons.append(f"Velocity {velocity*100:.3f}%")
            
            if rsi > 60:
                score -= 0.2
                confidence += 10
                reasons.append(f"RSI={rsi:.0f} overbought")
            
            if ma5 < ma10:
                score -= 0.15
                confidence += 10
                reasons.append("MA5<MA10")

        return score, reasons, min(90, confidence)


class SniperPortfolio:
    """All-in portfolio management with velocity tracking."""

    def __init__(self, initial_cash: float = 10.0):
        self.cash = initial_cash
        self.initial_cash = initial_cash
        self.position: Optional[Dict] = None
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.consecutive_losses = 0
        self.trade_history: List[Dict] = []
        self._load_state()

    def _state_path(self):
        return RUN_DIR / "state.json"

    def _load_state(self):
        p = self._state_path()
        if p.exists():
            try:
                d = json.loads(p.read_text())
                self.cash = d.get("cash", self.initial_cash)
                self.position = d.get("position")
                self.total_trades = d.get("total_trades", 0)
                self.wins = d.get("wins", 0)
                self.losses = d.get("losses", 0)
                self.total_pnl = d.get("total_pnl", 0.0)
                self.consecutive_losses = d.get("consecutive_losses", 0)
                self.trade_history = d.get("trade_history", [])
            except Exception:
                pass

    def _save_state(self):
        state = {
            "cash": self.cash,
            "position": self.position,
            "equity": self._calc_equity({}),
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": self.total_pnl,
            "consecutive_losses": self.consecutive_losses,
            "win_rate": self.wins / max(1, self.total_trades),
            "target_reached": self.cash >= TARGET,
            "trade_history": self.trade_history[-20:],  # Keep last 20 trades
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._state_path().write_text(json.dumps(state, indent=2))

    def _calc_equity(self, prices: Dict[str, float]) -> float:
        if not self.position:
            return self.cash
        cur = prices.get(self.position["symbol"], self.position["entry_price"])
        if self.position["side"] == "LONG":
            pnl = (cur - self.position["entry_price"]) / self.position["entry_price"] * self.position["notional"]
        else:
            pnl = (self.position["entry_price"] - cur) / self.position["entry_price"] * self.position["notional"]
        return self.cash + pnl

    def can_trade(self) -> bool:
        if self.position:
            return False
        if self.consecutive_losses >= 3:
            return False
        if self.cash >= TARGET:
            return False
        return True

    def open_position(self, symbol: str, side: str, price: float, confidence: int, velocity: float) -> bool:
        if not self.can_trade():
            return False

        notional = self.cash * POSITION_PCT
        if notional < 5:
            return False

        margin = notional / LEVERAGE
        fee = notional * 0.0005

        if margin + fee > self.cash:
            return False

        self.position = {
            "symbol": symbol,
            "side": side,
            "entry_price": price,
            "notional": notional,
            "margin": margin,
            "leverage": LEVERAGE,
            "entry_time": time.time(),
            "highest_price": price,
            "lowest_price": price,
            "peak_pnl_pct": 0.0,
            "peak_time": time.time(),
            "last_high_time": time.time(),
            "entry_velocity": velocity,
        }
        self.cash -= (margin + fee)
        self.total_trades += 1
        self._save_state()

        logger.info(f"SNIPER OPEN {side} {symbol}: ${notional:.2f} @ ${price:.6f} ({LEVERAGE}x)")
        logger.info(f"  Velocity: {velocity*100:.3f}% | Confidence: {confidence}%")
        return True

    def close_position(self, price: float, reason: str) -> Optional[float]:
        if not self.position:
            return None

        pos = self.position
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
            self.consecutive_losses = 0
        else:
            self.losses += 1
            self.consecutive_losses += 1

        pnl_pct = net_pnl / pos["margin"] * 100
        age = time.time() - pos["entry_time"]
        
        # Record trade
        trade = {
            "symbol": pos["symbol"],
            "side": pos["side"],
            "entry": entry,
            "exit": price,
            "pnl": net_pnl,
            "pnl_pct": pnl_pct,
            "age_s": age,
            "reason": reason,
            "peak_pnl_pct": pos["peak_pnl_pct"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.trade_history.append(trade)

        logger.info(f"SNIPER CLOSE {pos['symbol']}: PnL=${net_pnl:+.4f} ({pnl_pct:+.1f}% of margin) [{reason}]")
        logger.info(f"  Cash: ${self.cash:.4f} | Target: ${TARGET:.0f} | Progress: {self.cash/TARGET*100:.1f}%")

        self.position = None
        self._save_state()
        return net_pnl

    def check_exit(self, prices: Dict[str, float]) -> Optional[Tuple[float, str]]:
        if not self.position:
            return None

        pos = self.position
        cur = prices.get(pos["symbol"], pos["entry_price"])
        entry = pos["entry_price"]
        now = time.time()
        age = now - pos["entry_time"]

        # Update trailing
        if pos["side"] == "LONG":
            pos["highest_price"] = max(pos["highest_price"], cur)
            pnl_pct = (cur - entry) / entry
        else:
            pos["lowest_price"] = min(pos["lowest_price"], cur)
            pnl_pct = (entry - cur) / entry

        # Update peak PnL tracking
        if pnl_pct > pos["peak_pnl_pct"]:
            pos["peak_pnl_pct"] = pnl_pct
            pos["peak_time"] = now
            pos["last_high_time"] = now

        # ── EXIT LOGIC ──
        
        # 1. HARD STOP LOSS
        if pnl_pct <= -STOP_LOSS:
            return (cur, f"STOP LOSS ({pnl_pct*100:.2f}%)")
        
        # 2. MOMENTUM PEAK EXIT - close when profit drops from peak
        if pos["peak_pnl_pct"] > 0.003:
            drop_from_peak = pos["peak_pnl_pct"] - pnl_pct
            drop_pct = drop_from_peak / pos["peak_pnl_pct"]
            
            if drop_pct >= PEAK_DROP_EXIT:
                return (cur, f"PEAK EXIT (peak={pos['peak_pnl_pct']*100:.2f}% now={pnl_pct*100:.2f}% drop={drop_pct*100:.0f}%)")
        
        # 3. TAKE WHAT WE CAN - if profitable after 3 min, close
        if age >= 180 and pnl_pct > MIN_PROFIT_EXIT:
            return (cur, f"TAKE PROFIT ({pnl_pct*100:.2f}% after {age/60:.1f}min)")
        
        # 4. STALE POSITION - no new high in 90 seconds
        time_since_high = now - pos["last_high_time"]
        if time_since_high >= STALE_EXIT_TIMEOUT and pnl_pct < 0.001:
            return (cur, f"STALE EXIT (no momentum for {time_since_high:.0f}s)")
        
        # 5. MAX HOLD - force close
        if age >= MAX_HOLD:
            return (cur, f"MAX HOLD ({age/60:.0f}min)")

        return None


class SniperOrchestrator:
    """Main sniper loop."""

    def __init__(self):
        self.fetcher = SniperFetcher()
        self.signal = SniperSignal()
        self.portfolio = SniperPortfolio(INITIAL_CASH)
        self._tick_count = 0
        self._last_fg = 0
        self._running = True
        self._equity_log = RUN_DIR / "equity.jsonl"

    def run(self):
        logger.info("=" * 60)
        logger.info("SNIPER V3 — MOMENTUM CAPTURE")
        logger.info(f"Capital: ${INITIAL_CASH} -> Target: ${TARGET}")
        logger.info(f"Leverage: {LEVERAGE}x | SL: {STOP_LOSS*100}%")
        logger.info(f"Peak Drop Exit: {PEAK_DROP_EXIT*100:.0f}% from peak")
        logger.info(f"Take What We Can: Close if >{MIN_PROFIT_EXIT*100:.1f}% after 3min")
        logger.info(f"Stale Exit: {STALE_EXIT_TIMEOUT}s timeout")
        logger.info(f"Max Hold: {MAX_HOLD/60:.0f} min")
        logger.info(f"Assets: {', '.join(ASSETS)}")
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
        if now - self._last_fg > 300:
            self.signal.fg = self.fetcher.get_fear_greed()
            self._last_fg = now

        # Get prices
        prices = self.fetcher.get_prices(ASSETS)
        if not prices:
            return

        # Check exit first
        exit_signal = self.portfolio.check_exit(prices)
        if exit_signal:
            price, reason = exit_signal
            self.portfolio.close_position(price, reason)

        # Find new setup if no position
        if self.portfolio.can_trade():
            setup = self.signal.find_best_setup(prices, self.fetcher)
            if setup and setup["confidence"] >= 50:
                self.portfolio.open_position(
                    setup["symbol"], setup["action"], setup["price"],
                    setup["confidence"], setup["velocity"]
                )

        # Log
        equity = self.portfolio._calc_equity(prices)
        self._log_equity(equity, prices)

        if self._tick_count % 30 == 0:
            self._print_progress(equity, prices)

        # Check target reached
        if self.portfolio.cash >= TARGET:
            logger.info(f"TARGET REACHED! ${self.portfolio.cash:.2f} >= ${TARGET:.0f}")
            self._running = False

    def _log_equity(self, equity: float, prices: Dict[str, float]):
        pos_info = {}
        if self.portfolio.position:
            pos = self.portfolio.position
            cur = prices.get(pos["symbol"], pos["entry_price"])
            pnl = ((cur - pos["entry_price"]) / pos["entry_price"] * pos["notional"]
                   if pos["side"] == "LONG"
                   else (pos["entry_price"] - cur) / pos["entry_price"] * pos["notional"])
            pos_info = {
                "symbol": pos["symbol"],
                "side": pos["side"],
                "entry": pos["entry_price"],
                "current": cur,
                "pnl": pnl,
                "age_s": time.time() - pos["entry_time"],
                "peak_pnl_pct": pos["peak_pnl_pct"],
            }

        entry = {
            "tick": self._tick_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "equity": equity,
            "cash": self.portfolio.cash,
            "position": pos_info,
            "total_pnl": self.portfolio.total_pnl,
            "total_trades": self.portfolio.total_trades,
            "win_rate": self.portfolio.wins / max(1, self.portfolio.total_trades),
            "consecutive_losses": self.portfolio.consecutive_losses,
            "progress_pct": self.portfolio.cash / TARGET * 100,
        }
        with open(self._equity_log, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _print_progress(self, equity: float, prices: Dict[str, float]):
        ret = (self.portfolio.cash - INITIAL_CASH) / INITIAL_CASH * 100
        logger.info(
            f"Tick {self._tick_count}: Cash=${self.portfolio.cash:.4f} ({ret:+.1f}%) "
            f"Trades={self.portfolio.total_trades} WR={self.portfolio.wins}/{self.portfolio.total_trades} "
            f"Target: {self.portfolio.cash/TARGET*100:.1f}%"
        )
        if self.portfolio.position:
            pos = self.portfolio.position
            cur = prices.get(pos["symbol"], pos["entry_price"])
            pnl = (cur - pos["entry_price"]) / pos["entry_price"] * 100
            logger.info(f"  POSITION: {pos['side']} {pos['symbol']} @ ${pos['entry_price']:.6f} → ${cur:.6f} ({pnl:+.3f}%)")
            
            # Show velocity
            velocity = self.fetcher.get_velocity(pos["symbol"])
            logger.info(f"  Velocity: {velocity*100:.3f}% | Peak: {pos['peak_pnl_pct']*100:.2f}%")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(RUN_DIR / "stdout.log")),
        ],
    )
    orchestrator = SniperOrchestrator()
    orchestrator.run()


if __name__ == "__main__":
    main()
