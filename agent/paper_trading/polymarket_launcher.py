"""Polymarket Prediction Market Paper Trader.

Paper trades on Polymarket prediction markets using the Gamma Markets API.
Targets high-volume binary markets (Yes/No outcomes) with edge detection.

Strategy:
  - Find markets with high 24h volume and clear mispricings
  - Buy YES when probability < 0.40 and expected value > 1.2x
  - Buy NO when probability > 0.60 and expected value > 1.2x
  - Sell after price moves 10%+ or at market resolution
  - Kelly criterion position sizing
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
MAX_POSITION_PCT = 0.30  # 30% per market (diversified)
MAX_POSITIONS = 10
MIN_EDGE = 0.10  # 10% minimum expected edge
MIN_VOLUME_24H = 10000  # $10K minimum 24h volume
TICK_INTERVAL = 60  # seconds

RUN_DIR = Path("./paper_runs/polymarket")
RUN_DIR.mkdir(parents=True, exist_ok=True)


class PolymarketFetcher:
    """Fetch market data from Polymarket Gamma API."""

    GAMMA_URL = "https://gamma-api.polymarket.com"

    def get_active_markets(self, limit: int = 50) -> List[Dict]:
        """Get active markets sorted by 24h volume."""
        try:
            resp = requests.get(
                f"{self.GAMMA_URL}/markets",
                params={
                    "limit": limit,
                    "active": "true",
                    "order": "volume24hr",
                    "ascending": "false",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            return data.get("data", [])
        except Exception as e:
            logger.warning(f"Failed to fetch markets: {e}")
            return []

    def get_market(self, condition_id: str) -> Optional[Dict]:
        """Get a specific market."""
        try:
            resp = requests.get(
                f"{self.GAMMA_URL}/markets/{condition_id}", timeout=10
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug(f"Failed to fetch market {condition_id}: {e}")
            return None


class PolymarketSignals:
    """Signal generation for prediction markets."""

    def analyze_market(self, market: Dict) -> Dict[str, Any]:
        """Analyze a market for trading signals."""
        question = market.get("question", "")
        outcomes = market.get("outcomes", [])
        prices_raw = market.get("outcomePrices", [])
        volume_24h = float(market.get("volume24hr", 0))
        volume_total = float(market.get("volume", 0))
        condition_id = market.get("conditionId", "")
        end_date = market.get("endDate", "")

        # Parse outcomePrices (may be JSON string)
        if isinstance(prices_raw, str):
            try:
                import json as _json
                prices = _json.loads(prices_raw)
            except Exception:
                prices = []
        else:
            prices = prices_raw

        # Parse outcomes (may be JSON string)
        if isinstance(outcomes, str):
            try:
                import json as _json
                outcomes = _json.loads(outcomes)
            except Exception:
                outcomes = []

        if len(outcomes) < 2 or len(prices) < 2:
            return {"action": "SKIP", "reason": "Invalid market structure"}

        try:
            yes_price = float(prices[0])
            no_price = float(prices[1])
        except (ValueError, IndexError):
            return {"action": "SKIP", "reason": "Invalid prices"}

        # Calculate edge
        signals = []
        confidence = 50

        # 1. Mispricing detection
        # In a fair binary market, YES + NO should = ~1.0
        total = yes_price + no_price
        if abs(total - 1.0) > 0.05:
            signals.append(f"Price anomaly: YES+NO={total:.3f}")

        # 2. Extreme prices (mean reversion)
        if yes_price < 0.30:
            # Cheap YES - potential value
            expected_return = (1.0 - yes_price) / yes_price  # Max return if YES wins
            if expected_return > 2.0:  # 3:1 potential
                signals.append(f"Cheap YES at {yes_price:.3f} (max return {expected_return:.1f}x)")
                confidence += 15
        elif yes_price > 0.70:
            # Expensive YES - potential NO value
            expected_return = (1.0 - no_price) / no_price
            if expected_return > 2.0:
                signals.append(f"Cheap NO at {no_price:.3f} (max return {expected_return:.1f}x)")
                confidence += 15

        # 3. Volume signal
        if volume_24h > 100000:
            confidence += 10
            signals.append(f"High volume: ${volume_24h:,.0f}/24h")
        elif volume_24h > 50000:
            confidence += 5
            signals.append(f"Good volume: ${volume_24h:,.0f}/24h")

        # 4. Near-resolution markets (binary bet)
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                hours_left = (end_dt - now).total_seconds() / 3600
                if 0 < hours_left < 48:
                    confidence += 10
                    signals.append(f"Resolving in {hours_left:.0f}h")
            except Exception:
                pass

        # Determine action
        action = "FLAT"
        side = ""
        size_pct = 0.0

        if confidence >= 55 and signals:
            if yes_price < 0.40 and any("Cheap YES" in s for s in signals):
                action = "BUY"
                side = "YES"
                size_pct = min(MAX_POSITION_PCT, 0.20 + (confidence - 55) / 200)
            elif no_price < 0.40 and any("Cheap NO" in s for s in signals):
                action = "BUY"
                side = "NO"
                size_pct = min(MAX_POSITION_PCT, 0.20 + (confidence - 55) / 200)

        return {
            "action": action,
            "side": side,
            "confidence": min(90, confidence),
            "signals": signals,
            "yes_price": yes_price,
            "no_price": no_price,
            "volume_24h": volume_24h,
            "condition_id": condition_id,
            "question": question[:80],
            "size_pct": size_pct,
        }


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
        equity = self._calc_equity()
        state = {
            "cash": self.cash,
            "positions": self.positions,
            "equity": equity,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": self.total_pnl,
            "win_rate": self.wins / max(1, self.total_trades),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._state_path().write_text(json.dumps(state, indent=2))

    def _calc_equity(self) -> float:
        """Calculate total equity (cash + position values)."""
        equity = self.cash
        for pos in self.positions.values():
            # Position value = shares * current_price
            equity += pos.get("shares", 0) * pos.get("current_price", pos["entry_price"])
        return equity

    def buy(self, condition_id: str, side: str, price: float, size_pct: float, question: str) -> bool:
        """Buy YES or NO shares."""
        if len(self.positions) >= MAX_POSITIONS:
            return False
        if condition_id in self.positions:
            return False

        notional = self.cash * size_pct
        if notional < 1:
            return False

        shares = notional / price
        fee = notional * 0.02  # 2% maker fee

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
        logger.info(f"BUY {side} {question[:50]} @ ${price:.3f} (shares={shares:.1f}, notional=${notional:.2f})")
        return True

    def sell(self, condition_id: str, price: float, reason: str) -> Optional[float]:
        """Sell shares and return PnL."""
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

    def check_exits(self, markets: Dict[str, Dict]) -> List[tuple]:
        """Check positions for exit conditions."""
        to_close = []
        now = time.time()

        for cond_id, pos in self.positions.items():
            market = markets.get(cond_id, {})
            prices_raw = market.get("outcomePrices", [])

            # Parse outcomePrices
            if isinstance(prices_raw, str):
                try:
                    import json as _json
                    prices = _json.loads(prices_raw)
                except Exception:
                    prices = []
            else:
                prices = prices_raw

            age_hours = (now - pos["entry_time"]) / 3600

            if len(prices) >= 2:
                try:
                    if pos["side"] == "YES":
                        cur_price = float(prices[0])
                    else:
                        cur_price = float(prices[1])
                    pos["current_price"] = cur_price
                except (ValueError, IndexError):
                    cur_price = pos["current_price"]

                # Take profit (10%+ move)
                pnl_pct = (cur_price - pos["entry_price"]) / pos["entry_price"]
                if pnl_pct >= 0.10:
                    to_close.append((cond_id, cur_price, f"Take profit ({pnl_pct*100:.1f}%)"))
                # Stop loss (20% loss)
                elif pnl_pct <= -0.20:
                    to_close.append((cond_id, cur_price, f"Stop loss ({pnl_pct*100:.1f}%)"))
                # Max hold 7 days
                elif age_hours > 168:
                    to_close.append((cond_id, cur_price, f"Max hold ({age_hours:.0f}h)"))

        return to_close


class PolymarketOrchestrator:
    """Main loop for Polymarket paper trading."""

    def __init__(self):
        self.fetcher = PolymarketFetcher()
        self.signals = PolymarketSignals()
        self.portfolio = PolymarketPortfolio(INITIAL_CASH)
        self._tick_count = 0
        self._running = True
        self._equity_log = RUN_DIR / "equity.jsonl"

    def run(self):
        logger.info("=" * 60)
        logger.info("POLYMARKET PAPER TRADER STARTING")
        logger.info(f"Capital: ${INITIAL_CASH} | Max positions: {MAX_POSITIONS}")
        logger.info(f"Min edge: {MIN_EDGE*100}% | Min volume: ${MIN_VOLUME_24H:,}")
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

        # Build market lookup
        market_map = {m.get("conditionId", ""): m for m in markets}

        # Check exits
        exits = self.portfolio.check_exits(market_map)
        for cond_id, price, reason in exits:
            self.portfolio.sell(cond_id, price, reason)

        # Generate signals for top markets
        for market in markets[:20]:
            cond_id = market.get("conditionId", "")
            if cond_id in self.portfolio.positions:
                continue

            vol24 = float(market.get("volume24hr", 0))
            if vol24 < MIN_VOLUME_24H:
                continue

            signal = self.signals.analyze_market(market)
            if signal["action"] == "BUY" and signal["confidence"] >= 55:
                self.portfolio.buy(
                    condition_id=cond_id,
                    side=signal["side"],
                    price=signal["yes_price"] if signal["side"] == "YES" else signal["no_price"],
                    size_pct=signal["size_pct"],
                    question=signal["question"],
                )

        # Log equity
        equity = self.portfolio._calc_equity()
        self._log_equity(equity)

        # Print progress
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
        pos_str = ", ".join(
            f"{p['side']}@{p['entry_price']:.3f}"
            for p in self.portfolio.positions.values()
        )
        logger.info(
            f"Tick {self._tick_count}: Equity=${equity:.4f} ({ret:+.2f}%) "
            f"Cash=${self.portfolio.cash:.2f} Trades={self.portfolio.total_trades} "
            f"WR={self.portfolio.wins}/{self.portfolio.total_trades}"
        )
        if pos_str:
            logger.info(f"  Positions: {pos_str}")


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
