"""SNIPER V5 — Hold prediction, compound peaks.

$10 -> $100 in 6 hours

EXIT RULES (only exit when prediction is WRONG):
1. Stop loss (-2%) — price went against us
2. Peak captured — we had profit, it faded 40%, take it
3. Take profit (+5%) — prediction very correct
4. Time exit (30 min) — only if clearly wrong direction

NO stale exit. If prediction is right, hold through flat.

REENTRY: Immediately scan for next best setup after each exit.
COMPOUND: 95% of equity per trade.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import requests

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────

INITIAL_CASH = float(os.environ.get("INITIAL_CASH", "10"))
TARGET = 100.0
LEVERAGE = 20
POSITION_PCT = 0.95
STOP_LOSS = 0.02
TAKE_PROFIT = 0.05
PEAK_DROP_EXIT = 0.40
MAX_HOLD = 1800
TICK_INTERVAL = 3

ASSETS = ["JUP", "TURBO", "WIF", "SOL", "DOGE", "RENDER"]

RUN_DIR = Path("./paper_runs/sniper")
RUN_DIR.mkdir(parents=True, exist_ok=True)


class Fetcher:
    HL_URL = "https://api.hyperliquid.xyz/info"

    def __init__(self):
        self._history: Dict[str, List[float]] = {}

    def prices(self, symbols: list) -> Dict[str, float]:
        try:
            resp = requests.post(self.HL_URL, json={"type": "allMids"}, timeout=5)
            data = resp.json()
            result = {}
            for s in symbols:
                if s in data:
                    p = float(data[s])
                    result[s] = p
                    if s not in self._history:
                        self._history[s] = []
                    self._history[s].append(p)
                    if len(self._history[s]) > 60:
                        self._history[s] = self._history[s][-60:]
            return result
        except Exception:
            return {}

    def velocity(self, symbol: str, lookback: int = 10) -> float:
        h = self._history.get(symbol, [])
        if len(h) < lookback:
            return 0.0
        recent = h[-lookback:]
        return (recent[-1] - recent[0]) / recent[0]

    def trend(self, symbol: str, lookback: int = 20) -> float:
        h = self._history.get(symbol, [])
        if len(h) < lookback:
            return 0.0
        return (h[-1] - h[-lookback]) / h[-lookback]

    def candles(self, symbol: str) -> Optional[Dict]:
        try:
            now = int(time.time() * 1000)
            start = now - (20 * 300000)
            resp = requests.post(
                self.HL_URL,
                json={"type": "candleSnapshot", "req": {"coin": symbol, "interval": "5m", "startTime": start, "endTime": now}},
                timeout=10,
            )
            data = resp.json()
            if not data:
                return None
            return {
                "close": [float(d["c"]) for d in data],
                "high": [float(d["h"]) for d in data],
                "low": [float(d["l"]) for d in data],
            }
        except Exception:
            return None


class Signal:
    def evaluate(self, symbol: str, price: float, fetcher: Fetcher) -> Optional[Dict]:
        vel = fetcher.velocity(symbol)
        trend = fetcher.trend(symbol)
        candles = fetcher.candles(symbol)
        if not candles or len(candles["close"]) < 15:
            return None

        closes = np.array(candles["close"])

        # RSI
        deltas = np.diff(closes[-15:])
        gains = np.where(deltas > 0, deltas, 0)
        losses_arr = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains)
        avg_loss = max(np.mean(losses_arr), 0.00001)
        rsi = 100 - (100 / (1 + avg_gain / avg_loss))

        ma5 = np.mean(closes[-5:])
        ma10 = np.mean(closes[-10:])

        score = 0.0
        reasons = []

        # LONG
        if vel > 0 and (trend > -0.003 or vel > 0.001):
            score = 0.3
            reasons.append(f"vel={vel*100:.3f}%")
            if rsi < 40:
                score += 0.2
                reasons.append(f"rsi={rsi:.0f}")
            if ma5 > ma10:
                score += 0.15
                reasons.append("ma_up")
            return {"side": "LONG", "score": score, "reasons": reasons, "vel": vel}

        # SHORT
        if vel < 0 and (trend < 0.003 or vel < -0.001):
            score = 0.3
            reasons.append(f"vel={vel*100:.3f}%")
            if rsi > 60:
                score += 0.2
                reasons.append(f"rsi={rsi:.0f}")
            if ma5 < ma10:
                score += 0.15
                reasons.append("ma_dn")
            return {"side": "SHORT", "score": score, "reasons": reasons, "vel": vel}

        return None


class Portfolio:
    def __init__(self):
        self.cash = INITIAL_CASH
        self.position: Optional[Dict] = None
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.consecutive_losses = 0
        self.history: List[Dict] = []
        self._load()

    def _path(self):
        return RUN_DIR / "state.json"

    def _load(self):
        p = self._path()
        if p.exists():
            try:
                d = json.loads(p.read_text())
                self.cash = d.get("cash", INITIAL_CASH)
                self.position = d.get("position")
                self.total_trades = d.get("total_trades", 0)
                self.wins = d.get("wins", 0)
                self.losses = d.get("losses", 0)
                self.total_pnl = d.get("total_pnl", 0.0)
                self.consecutive_losses = d.get("consecutive_losses", 0)
                self.history = d.get("history", [])
            except Exception:
                pass

    def save(self):
        state = {
            "cash": self.cash,
            "position": self.position,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": self.total_pnl,
            "consecutive_losses": self.consecutive_losses,
            "win_rate": self.wins / max(1, self.total_trades),
            "history": self.history[-50:],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._path().write_text(json.dumps(state, indent=2))

    def can_trade(self) -> bool:
        return (self.position is None
                and self.consecutive_losses < 3
                and self.cash < TARGET)

    def open(self, symbol: str, side: str, price: float, vel: float, reasons: list) -> bool:
        if not self.can_trade():
            return False
        notional = self.cash * POSITION_PCT
        margin = notional / LEVERAGE
        fee = notional * 0.0005
        if margin + fee > self.cash or notional < 5:
            return False

        self.position = {
            "symbol": symbol,
            "side": side,
            "entry_price": price,
            "notional": notional,
            "margin": margin,
            "entry_time": time.time(),
            "peak_pnl_pct": 0.0,
            "last_high_time": time.time(),
            "entry_vel": vel,
            "entry_reasons": reasons,
        }
        self.cash -= (margin + fee)
        self.total_trades += 1
        self.save()
        logger.info(f"OPEN {side} {symbol} @ ${price:.6f} | ${notional:.2f} ({LEVERAGE}x) | vel={vel*100:.3f}% | {', '.join(reasons)}")
        return True

    def close(self, price: float, reason: str) -> float:
        if not self.position:
            return 0.0

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

        self.history.append({
            "symbol": pos["symbol"],
            "side": pos["side"],
            "entry": entry,
            "exit": price,
            "pnl": net_pnl,
            "pnl_pct": pnl_pct,
            "age_s": age,
            "peak": pos["peak_pnl_pct"],
            "reason": reason,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

        logger.info(f"CLOSE {pos['symbol']}: ${net_pnl:+.4f} ({pnl_pct:+.1f}%) [{reason}] | cash=${self.cash:.4f} ({self.cash/TARGET*100:.0f}%)")

        self.position = None
        self.save()
        return net_pnl

    def check_exit(self, prices: Dict[str, float], fetcher: Fetcher) -> Optional[Tuple[float, str]]:
        """Exit ONLY when prediction is wrong. Hold through flat."""
        if not self.position:
            return None

        pos = self.position
        cur = prices.get(pos["symbol"], pos["entry_price"])
        entry = pos["entry_price"]
        now = time.time()
        age = now - pos["entry_time"]

        if pos["side"] == "LONG":
            pnl_pct = (cur - entry) / entry
        else:
            pnl_pct = (entry - cur) / entry

        # Update peak
        if pnl_pct > pos["peak_pnl_pct"]:
            pos["peak_pnl_pct"] = pnl_pct
            pos["last_high_time"] = now

        # 1. STOP LOSS — prediction wrong
        if pnl_pct <= -STOP_LOSS:
            return (cur, f"STOP_LOSS ({pnl_pct*100:.2f}%)")

        # 2. PEAK CAPTURED — had profit, it faded
        if pos["peak_pnl_pct"] > 0.003:
            drop = pos["peak_pnl_pct"] - pnl_pct
            drop_pct = drop / pos["peak_pnl_pct"]
            if drop_pct >= PEAK_DROP_EXIT:
                return (cur, f"PEAK_CAPTURED (peak={pos['peak_pnl_pct']*100:.2f}% drop={drop_pct*100:.0f}%)")

        # 3. TAKE PROFIT — prediction very correct
        if pnl_pct >= TAKE_PROFIT:
            return (cur, f"TAKE_PROFIT ({pnl_pct*100:.2f}%)")

        # 4. TIME EXIT — only if clearly wrong direction
        if age >= MAX_HOLD:
            vel = fetcher.velocity(pos["symbol"])
            if pos["side"] == "LONG" and vel < -0.001:
                return (cur, f"TIME_WRONG_DIR (vel={vel*100:.3f}%)")
            if pos["side"] == "SHORT" and vel > 0.001:
                return (cur, f"TIME_WRONG_DIR (vel={vel*100:.3f}%)")
            if pnl_pct > 0:
                return (cur, f"TIME_PROFIT ({pnl_pct*100:.2f}%)")

        return None


class Orchestrator:
    def __init__(self):
        self.fetcher = Fetcher()
        self.signal = Signal()
        self.portfolio = Portfolio()
        self.tick_count = 0
        self.running = True
        self.start_time = time.time()
        self._equity_log = RUN_DIR / "equity.jsonl"

    def run(self):
        elapsed = (time.time() - self.start_time) / 3600
        logger.info("=" * 60)
        logger.info("SNIPER V5 — 6-HOUR COMPOUND")
        logger.info(f"${INITIAL_CASH} -> ${TARGET} in 6h")
        logger.info(f"Leverage: {LEVERAGE}x | SL: {STOP_LOSS*100}% | TP: {TAKE_PROFIT*100}%")
        logger.info(f"Peak drop exit: {PEAK_DROP_EXIT*100:.0f}%")
        logger.info(f"NO stale exit — hold through flat")
        logger.info(f"Assets: {', '.join(ASSETS)}")
        logger.info("=" * 60)

        while self.running:
            try:
                self._tick()
                time.sleep(TICK_INTERVAL)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Tick error: {e}")
                time.sleep(5)

        logger.info("STOPPED")

    def _tick(self):
        self.tick_count += 1
        now = time.time()
        elapsed_h = (now - self.start_time) / 3600

        # Safety: stop after 6 hours
        if elapsed_h >= 6.0:
            logger.info(f"6 HOURS REACHED — final cash: ${self.portfolio.cash:.4f}")
            self.running = False
            return

        prices = self.fetcher.prices(ASSETS)
        if not prices:
            return

        # Check exit first
        exit_sig = self.portfolio.check_exit(prices, self.fetcher)
        if exit_sig:
            price, reason = exit_sig
            self.portfolio.close(price, reason)

        # Find new entry
        if self.portfolio.can_trade():
            best = None
            best_score = -999
            for sym in ASSETS:
                p = prices.get(sym)
                if not p:
                    continue
                vel = self.fetcher.velocity(sym)
                if abs(vel) < 0.0003:
                    continue
                sig = self.signal.evaluate(sym, p, self.fetcher)
                if sig and sig["score"] > best_score:
                    best_score = sig["score"]
                    best = (sym, sig)

            if best:
                sym, sig = best
                self.portfolio.open(sym, sig["side"], prices[sym], sig["vel"], sig["reasons"])

        # Log equity
        equity = self.portfolio.cash
        if self.portfolio.position:
            pos = self.portfolio.position
            cur = prices.get(pos["symbol"], pos["entry_price"])
            if pos["side"] == "LONG":
                pnl = (cur - pos["entry_price"]) / pos["entry_price"] * pos["notional"]
            else:
                pnl = (pos["entry_price"] - cur) / pos["entry_price"] * pos["notional"]
            equity = self.portfolio.cash + pnl

        with open(self._equity_log, "a") as f:
            f.write(json.dumps({
                "tick": self.tick_count,
                "ts": datetime.now(timezone.utc).isoformat(),
                "elapsed_h": elapsed_h,
                "equity": equity,
                "cash": self.portfolio.cash,
                "trades": self.portfolio.total_trades,
                "wins": self.portfolio.wins,
                "losses": self.portfolio.losses,
                "pnl": self.portfolio.total_pnl,
                "progress": self.portfolio.cash / TARGET * 100,
            }) + "\n")

        # Progress every 60 ticks (~3 min)
        if self.tick_count % 60 == 0:
            ret = (self.portfolio.cash - INITIAL_CASH) / INITIAL_CASH * 100
            logger.info(f"[{elapsed_h:.1f}h] Cash=${self.portfolio.cash:.4f} ({ret:+.1f}%) Trades={self.portfolio.total_trades} WR={self.portfolio.wins}/{self.portfolio.total_trades}")
            if self.portfolio.position:
                pos = self.portfolio.position
                cur = prices.get(pos["symbol"], pos["entry_price"])
                pnl_pct = (cur - pos["entry_price"]) / pos["entry_price"] * 100
                logger.info(f"  POS: {pos['side']} {pos['symbol']} peak={pos['peak_pnl_pct']*100:.2f}% cur={pnl_pct:+.3f}%")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(RUN_DIR / "stdout.log")),
        ],
    )
    Orchestrator().run()


if __name__ == "__main__":
    main()
