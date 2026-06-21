"""Polymarket Paper Trader V2 — Reversed Strategy.

Instead of buying cheap longshots, we:
1. BUY YES on favorites (>0.70 prob) - ride momentum
2. BUY NO on longshots (<0.30 prob) - fade the hype
3. Quick in/out - take profit at 5%, stop at 10%
4. Focus on high-volume markets only
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

INITIAL_CASH = float(os.environ.get("INITIAL_CASH", "10"))
MAX_POSITION_PCT = 0.25  # 25% per market
MAX_POSITIONS = 8
TAKE_PROFIT_PCT = 0.05  # 5%
STOP_LOSS_PCT = 0.10  # 10%
MIN_VOLUME_24H = 50000  # $50K minimum
TICK_INTERVAL = 30  # seconds

RUN_DIR = Path("./paper_runs/polymarket_v2")
RUN_DIR.mkdir(parents=True, exist_ok=True)


class PolymarketFetcher:
    """Fetch market data from Polymarket Gamma API."""

    GAMMA_URL = "https://gamma-api.polymarket.com"

    def get_active_markets(self, limit: int = 50) -> List[Dict]:
        try:
            resp = requests.get(
                f"{self.GAMMA_URL}/markets",
                params={"limit": limit, "active": "true", "order": "volume24hr", "ascending": "false"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("data", [])
        except Exception as e:
            logger.warning(f"Failed to fetch markets: {e}")
            return []

    def parse_prices(self, market: Dict) -> tuple:
        """Parse outcome prices from market."""
        prices_raw = market.get("outcomePrices", [])
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except Exception:
                return None, None
        else:
            prices = prices_raw

        if len(prices) < 2:
            return None, None

        try:
            return float(prices[0]), float(prices[1])
        except (ValueError, IndexError):
            return None, None


class ReversedSignals:
    """Reversed strategy: ride favorites, fade longshots."""

    def analyze_market(self, market: Dict) -> Dict[str, Any]:
        question = market.get("question", "")
        volume_24h = float(market.get("volume24hr", 0))
        condition_id = market.get("conditionId", "")

        yes_price, no_price = self._fetcher_parse(market)
        if yes_price is None:
            return {"action": "SKIP", "reason": "Invalid prices"}

        signals = []
        confidence = 50

        # ── REVERSED LOGIC ──
        # Buy YES on favorites (price > 0.70)
        if yes_price > 0.70:
            edge = yes_price - 0.50  # How far above 50/50
            if edge > 0.20:  # Strong favorite
                signals.append(f"Strong favorite YES at {yes_price:.3f} (edge={edge:.3f})")
                confidence += 20
                return {
                    "action": "BUY", "side": "YES", "price": yes_price,
                    "confidence": min(90, confidence), "signals": signals,
                    "condition_id": condition_id, "question": question[:80],
                    "volume_24h": volume_24h,
                    "size_pct": min(MAX_POSITION_PCT, 0.15 + edge * 0.3),
                }

        # Buy NO on longshots (price < 0.30 means YES is cheap, NO is expensive)
        # Actually: if YES < 0.30, then NO > 0.70, so we're fading the YES
        if yes_price < 0.30:
            edge = 0.50 - yes_price  # How far below 50/50
            if edge > 0.20:  # Strong longshot
                signals.append(f"Fading longshot: YES at {yes_price:.3f} → buy NO")
                confidence += 20
                return {
                    "action": "BUY", "side": "NO", "price": no_price,
                    "confidence": min(90, confidence), "signals": signals,
                    "condition_id": condition_id, "question": question[:80],
                    "volume_24h": volume_24h,
                    "size_pct": min(MAX_POSITION_PCT, 0.15 + edge * 0.3),
                }

        # Moderate favorites (0.55-0.70)
        if 0.55 < yes_price < 0.70:
            signals.append(f"Moderate favorite YES at {yes_price:.3f}")
            confidence += 10
            return {
                "action": "BUY", "side": "YES", "price": yes_price,
                "confidence": min(80, confidence), "signals": signals,
                "condition_id": condition_id, "question": question[:80],
                "volume_24h": volume_24h,
                "size_pct": 0.15,
            }

        return {"action": "SKIP", "reason": f"No edge (YES={yes_price:.3f})"}

    def _fetcher_parse(self, market: Dict):
        prices_raw = market.get("outcomePrices", [])
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except Exception:
                return None, None
        else:
            prices = prices_raw
        if len(prices) < 2:
            return None, None
        try:
            return float(prices[0]), float(prices[1])
        except (ValueError, IndexError):
            return None, None


class PolymarketPortfolio:
    """Paper portfolio for Polymarket trading."""

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
            "equity": self._calc_equity(),
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": self.total_pnl,
            "win_rate": self.wins / max(1, self.total_trades),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._state_path().write_text(json.dumps(state, indent=2))

    def _calc_equity(self) -> float:
        equity = self.cash
        for pos in self.positions.values():
            equity += pos.get("shares", 0) * pos.get("current_price", pos["entry_price"])
        return equity

    def buy(self, condition_id: str, side: str, price: float, size_pct: float, question: str) -> bool:
        if len(self.positions) >= MAX_POSITIONS:
            return False
        if condition_id in self.positions:
            return False

        notional = self.cash * size_pct
        if notional < 1:
            return False

        shares = notional / price
        fee = notional * 0.02

        if notional + fee > self.cash:
            return False

        self.positions[condition_id] = {
            "side": side,
            "entry_price": price,
            "current_price": price,
            "shares": shares,
            "notional": notional,
            "entry_time": time.time(),
            "question": question,
        }
        self.cash -= (notional + fee)
        self.total_trades += 1
        self._save_state()
        logger.info(f"BUY {side} {question[:50]} @ ${price:.3f} (${notional:.2f}, {shares:.1f} shares)")
        return True

    def sell(self, condition_id: str, price: float, reason: str) -> Optional[float]:
        if condition_id not in self.positions:
            return None

        pos = self.positions[condition_id]
        shares = pos["shares"]
        notional = shares * price
        fee = notional * 0.02
        net = notional - fee

        pnl = net - pos["notional"]

        self.cash += net
        self.total_pnl += pnl
        if pnl > 0:
            self.wins += 1
        else:
            self.losses += 1

        del self.positions[condition_id]
        self._save_state()
        logger.info(f"SELL {pos['side']} {pos['question'][:50]} @ ${price:.3f} PnL=${pnl:+.4f} ({reason})")
        return pnl

    def check_exits(self, market_prices: Dict[str, tuple]) -> List[tuple]:
        """Check positions for exit conditions."""
        to_close = []
        now = time.time()

        for cond_id, pos in self.positions.items():
            prices = market_prices.get(cond_id)
            if not prices:
                continue

            yes_price, no_price = prices
            if pos["side"] == "YES":
                cur_price = yes_price
            else:
                cur_price = no_price

            pos["current_price"] = cur_price
            age_hours = (now - pos["entry_time"]) / 3600

            pnl_pct = (cur_price - pos["entry_price"]) / pos["entry_price"]

            # Take profit
            if pnl_pct >= TAKE_PROFIT_PCT:
                to_close.append((cond_id, cur_price, f"Take profit ({pnl_pct*100:.1f}%)"))
            # Stop loss
            elif pnl_pct <= -STOP_LOSS_PCT:
                to_close.append((cond_id, cur_price, f"Stop loss ({pnl_pct*100:.1f}%)"))
            # Max hold 1 hour (quick trades)
            elif age_hours > 1:
                to_close.append((cond_id, cur_price, f"Max hold ({age_hours:.1f}h)"))

        return to_close


class PolymarketOrchestrator:
    """Main loop for Polymarket paper trading."""

    def __init__(self):
        self.fetcher = PolymarketFetcher()
        self.signals = ReversedSignals()
        self.portfolio = PolymarketPortfolio(INITIAL_CASH)
        self._tick_count = 0
        self._running = True
        self._equity_log = RUN_DIR / "equity.jsonl"

    def run(self):
        logger.info("=" * 60)
        logger.info("POLYMARKET V2 — REVERSED STRATEGY")
        logger.info(f"Capital: ${INITIAL_CASH} | Max positions: {MAX_POSITIONS}")
        logger.info(f"TP: {TAKE_PROFIT_PCT*100}% | SL: {STOP_LOSS_PCT*100}% | Max hold: 1h")
        logger.info("Strategy: Buy favorites YES, fade longshots NO")
        logger.info("=" * 60)

        while self._running:
            try:
                self._tick()
                time.sleep(TICK_INTERVAL)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Tick error: {e}", exc_info=True)
                time.sleep(10)

        logger.info("Stopped.")

    def _tick(self):
        self._tick_count += 1

        # Fetch markets
        markets = self.fetcher.get_active_markets(50)
        if not markets:
            return

        # Build price lookup for exits
        market_prices = {}
        for m in markets:
            cid = m.get("conditionId", "")
            yes_p, no_p = self.signals._fetcher_parse(m)
            if yes_p is not None:
                market_prices[cid] = (yes_p, no_p)

        # Check exits
        exits = self.portfolio.check_exits(market_prices)
        for cond_id, price, reason in exits:
            self.portfolio.sell(cond_id, price, reason)

        # Generate signals
        for market in markets[:30]:
            cond_id = market.get("conditionId", "")
            if cond_id in self.portfolio.positions:
                continue

            vol24 = float(market.get("volume24hr", 0))
            if vol24 < MIN_VOLUME_24H:
                continue

            signal = self.signals.analyze_market(market)
            if signal["action"] == "BUY" and signal["confidence"] >= 60:
                self.portfolio.buy(
                    condition_id=cond_id,
                    side=signal["side"],
                    price=signal["price"],
                    size_pct=signal["size_pct"],
                    question=signal["question"],
                )

        # Log and print
        equity = self.portfolio._calc_equity()
        self._log_equity(equity)

        if self._tick_count % 10 == 0:
            self._print_progress(equity)

    def _log_equity(self, equity: float):
        entry = {
            "tick": self._tick_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "equity": equity,
            "cash": self.portfolio.cash,
            "positions": len(self.portfolio.positions),
            "total_pnl": self.portfolio.total_pnl,
            "total_trades": self.portfolio.total_trades,
            "win_rate": self.portfolio.wins / max(1, self.portfolio.total_trades),
        }
        with open(self._equity_log, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _print_progress(self, equity: float):
        ret = (equity - INITIAL_CASH) / INITIAL_CASH * 100
        logger.info(
            f"Tick {self._tick_count}: Equity=${equity:.4f} ({ret:+.2f}%) "
            f"Cash=${self.portfolio.cash:.2f} Trades={self.portfolio.total_trades} "
            f"WR={self.portfolio.wins}/{self.portfolio.total_trades} "
            f"PnL=${self.portfolio.total_pnl:+.4f}"
        )
        for cid, pos in self.portfolio.positions.items():
            pnl = (pos["current_price"] - pos["entry_price"]) / pos["entry_price"] * 100
            logger.info(f"  {pos['side']} {pos['question'][:45]} @ {pos['entry_price']:.3f} → {pos['current_price']:.3f} ({pnl:+.1f}%)")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(RUN_DIR / "stdout.log")),
        ],
    )
    orchestrator = PolymarketOrchestrator()
    orchestrator.run()


if __name__ == "__main__":
    main()
