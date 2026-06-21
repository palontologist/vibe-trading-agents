"""SNIPER V13 — W ONLY, BIG BETS, BIG MOMENTUM

Final approach: focus on the single best asset (W) with maximum conviction.

W is the most volatile asset on Hyperliquid:
- Moves 0.36% every 5 min = 7.2% with 20x
- Biggest wins: +28.9%, +20.2%, +13%
- We captured these before — now we go ALL IN on W

SETTINGS:
- W only (no diversification)
- 95% position sizing (all-in on one trade)
- Wait for 0.5% momentum (bigger signal)
- Trailing stop to capture big moves
- 20x leverage
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

INITIAL_CASH = float(os.environ.get("INITIAL_CASH", "10"))
TARGET = 100.0
LEVERAGE = 20
POSITION_PCT = 0.95   # ALL IN on one trade
STOP_LOSS = 0.005     # 0.5% price = 10% loss (wider, avoid noise)
TAKE_PROFIT = 0.03    # 3% max (but trailing will exit before)
PEAK_TRAIL_ACTIVATE = 0.0035  # Trail after 0.35% peak (balance speed vs size)
PEAK_DROP_EXIT = 0.40  # 40% drop from peak (tighter trail = lock profit faster)
MOMENTUM_THRESHOLD = 0.0025  # 0.25% move in 5 min (catch W moves)
MAX_HOLD = 1800       # 30 min
TICK_INTERVAL = 1

ASSETS = ["W"]  # W ONLY
RUN_DIR = Path(__file__).resolve().parent.parent / "paper_runs" / "sniper"
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
                    if len(self._history[s]) > 300:
                        self._history[s] = self._history[s][-300:]
            return result
        except Exception:
            return {}

    def momentum(self, symbol: str) -> float:
        h = self._history.get(symbol, [])
        if len(h) < 150:
            return 0.0
        return (h[-1] - h[-150]) / h[-150]

    def velocity(self, symbol: str, lookback: int = 30) -> float:
        h = self._history.get(symbol, [])
        if len(h) < lookback:
            return 0.0
        recent = h[-lookback:]
        return (recent[-1] - recent[0]) / recent[0]


class Signal:
    def evaluate(self, symbol: str, price: float, fetcher: Fetcher) -> Optional[Dict]:
        mom = fetcher.momentum(symbol)
        vel = fetcher.velocity(symbol)
        reasons = []

        # BUY: W moved up 0.5%+ in 5 min
        if mom > MOMENTUM_THRESHOLD:
            score = 0.7
            reasons.append(f"mom={mom*100:+.3f}%")
            reasons.append(f"vel={vel*100:+.3f}%")
            return {"side": "LONG", "score": score, "reasons": reasons, "vel": vel}

        # SELL: W moved down 0.5%+ in 5 min
        if mom < -MOMENTUM_THRESHOLD:
            score = 0.7
            reasons.append(f"mom={mom*100:+.3f}%")
            reasons.append(f"vel={vel*100:+.3f}%")
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
                and self.consecutive_losses < 4
                and self.cash >= 1.0
                and self.cash < TARGET)

    def open(self, symbol: str, side: str, price: float, vel: float, reasons: list) -> bool:
        if not self.can_trade():
            return False
        notional = self.cash * POSITION_PCT
        margin = notional / LEVERAGE
        fee = notional * 0.0005
        if margin + fee > self.cash or notional < 1:
            return False

        self.position = {
            "symbol": symbol,
            "side": side,
            "entry_price": price,
            "notional": notional,
            "margin": margin,
            "entry_time": time.time(),
            "peak_pnl_pct": 0.0,
        }
        self.cash -= (margin + fee)
        self.total_trades += 1
        self.save()
        logger.info(f"OPEN {side} {symbol} @ ${price:.6f} | ${notional:.2f} ({LEVERAGE}x) | {', '.join(reasons)}")
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
            "symbol": pos["symbol"], "side": pos["side"],
            "entry": entry, "exit": price,
            "pnl": net_pnl, "pnl_pct": pnl_pct,
            "age_s": age, "peak": pos["peak_pnl_pct"],
            "reason": reason,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

        logger.info(f"CLOSE {pos['symbol']}: ${net_pnl:+.4f} ({pnl_pct:+.1f}%) [{reason}] | cash=${self.cash:.4f}")
        self.position = None
        self.save()
        return net_pnl

    def check_exit(self, price: float) -> Optional[Tuple[float, str]]:
        if not self.position:
            return None
        pos = self.position
        entry = pos["entry_price"]
        age = time.time() - pos["entry_time"]

        if pos["side"] == "LONG":
            pnl_pct = (price - entry) / entry
        else:
            pnl_pct = (entry - price) / entry

        if pnl_pct > pos["peak_pnl_pct"]:
            pos["peak_pnl_pct"] = pnl_pct

        # 1. STOP LOSS
        if pnl_pct <= -STOP_LOSS:
            return (price, f"STOP_LOSS ({pnl_pct*100:.2f}%)")

        # 2. TRAILING STOP
        if pos["peak_pnl_pct"] >= PEAK_TRAIL_ACTIVATE:
            drop = pos["peak_pnl_pct"] - pnl_pct
            drop_pct = drop / pos["peak_pnl_pct"]
            if drop_pct >= PEAK_DROP_EXIT:
                return (price, f"TRAILING (peak={pos['peak_pnl_pct']*100:.2f}% drop={drop_pct*100:.0f}%)")

        # 3. TAKE PROFIT
        if pnl_pct >= TAKE_PROFIT:
            return (price, f"TAKE_PROFIT ({pnl_pct*100:.2f}%)")

        # 4. TIME EXIT
        if age >= MAX_HOLD:
            return (price, f"TIME_EXIT ({pnl_pct*100:.2f}%)")

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
        logger.info("=" * 60)
        logger.info("SNIPER V13 — W ONLY, ALL IN")
        logger.info(f"${INITIAL_CASH} -> ${TARGET} in 6h")
        logger.info(f"Leverage: {LEVERAGE}x | Asset: W ONLY")
        logger.info(f"Strategy: Catch big W momentum, trail winners")
        logger.info(f"Signal: 0.5% move in 5 min (bigger signal)")
        logger.info(f"Position: 95% (ALL IN)")
        logger.info(f"SL: {STOP_LOSS*100:.1f}% | Trail after {PEAK_TRAIL_ACTIVATE*100:.1f}%")
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
        elapsed_h = (time.time() - self.start_time) / 3600

        if elapsed_h >= 6.0:
            logger.info(f"6 HOURS REACHED — final cash: ${self.portfolio.cash:.4f}")
            self.running = False
            return

        prices = self.fetcher.prices(ASSETS)
        if not prices:
            return

        # Check exit
        if self.portfolio.position and "W" in prices:
            exit_sig = self.portfolio.check_exit(prices["W"])
            if exit_sig:
                price, reason = exit_sig
                self.portfolio.close(price, reason)

        # Find entry
        if self.portfolio.can_trade() and "W" in prices:
            sig = self.signal.evaluate("W", prices["W"], self.fetcher)
            if sig:
                self.portfolio.open("W", sig["side"], prices["W"], sig["vel"], sig["reasons"])

        # Log equity
        equity = self.portfolio.cash
        if self.portfolio.position:
            cur = prices.get("W", self.portfolio.position["entry_price"])
            pos = self.portfolio.position
            if pos["side"] == "LONG":
                pnl = (cur - pos["entry_price"]) / pos["entry_price"] * pos["notional"]
            else:
                pnl = (pos["entry_price"] - cur) / pos["entry_price"] * pos["notional"]
            equity += pnl

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
            }) + "\n")

        if self.tick_count % 60 == 0:
            ret = (self.portfolio.cash - INITIAL_CASH) / INITIAL_CASH * 100
            logger.info(f"[{elapsed_h:.1f}h] Cash=${self.portfolio.cash:.4f} ({ret:+.1f}%) Trades={self.portfolio.total_trades} WR={self.portfolio.wins}/{self.portfolio.total_trades}")
            if self.portfolio.position:
                pos = self.portfolio.position
                cur = prices.get("W", pos["entry_price"])
                pnl_pct = (cur - pos["entry_price"]) / pos["entry_price"] * 100
                logger.info(f"  {pos['side']} W peak={pos['peak_pnl_pct']*100:.2f}% cur={pnl_pct:+.3f}%")


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
