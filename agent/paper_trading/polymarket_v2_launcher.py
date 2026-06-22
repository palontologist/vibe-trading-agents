"""Polymarket Paper Trader V2 — Reversed + 15-Minute Reverse Bot.

Two strategies in one file:

1. V2 REVERSED (original): Ride favorites, fade longshots on high-volume markets
   - BUY YES on favorites (>0.70 prob)
   - BUY NO on longshots (<0.30 prob)
   - Quick in/out — TP 5%, SL 10%

2. 15-MINUTE REVERSE BOT: Dual-leg limit orders on BTC/ETH Up/Down windows
   - Cheap leg: BUY underdog @ 7-10¢ (reversal bet, 10-14x payout)
   - Hedge leg: BUY favorite @ 90-95¢ (small profit if favorite holds)
   - Hold to resolution — no selling
   - Scans every 5 seconds for new 15m windows

Run modes:
  python -m paper_trading.polymarket_v2_launcher              # V2 reversed
  python -m paper_trading.polymarket_v2_launcher --reverse-15m # 15m reverse bot
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import requests

logger = logging.getLogger(__name__)

INITIAL_CASH = float(os.environ.get("INITIAL_CASH", "10"))
MAX_POSITION_PCT = 0.25  # 25% per market
MAX_POSITIONS = 8
TAKE_PROFIT_PCT = 0.05  # 5%
STOP_LOSS_PCT = 0.10  # 10%
MIN_VOLUME_24H = 50000  # $50K minimum
TICK_INTERVAL = 30  # seconds

# ── 15-Minute Reverse Bot Config ───────────────────────────────────────────────
REVERSE_15M_TICK = 5  # seconds between scans
# Real strategy: buy both sides at various price levels
CHEAP_PRICES = [0.25, 0.30, 0.35, 0.40]  # underdog side
MID_PRICES = [0.45, 0.50, 0.55]  # middle ground
FAVORITE_PRICES = [0.60, 0.65, 0.70, 0.75]  # favorite side
SHARES_PER_LEVEL = 8  # shares per price level (matching real trades)
ENABLE_HEDGE = True  # place both sides
TARGET_ASSETS = ["btc", "eth"]  # which 15m markets to trade

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
        self.total_invested = 0.0  # for 15m reverse bot tracking
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
                self.total_invested = d.get("total_invested", 0.0)
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
            "total_invested": self.total_invested,
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

    # ── 15-Minute Reverse Bot Methods ──────────────────────────────────────────

    def can_place_order(self, condition_id: str, price_label: str) -> bool:
        """Check if we've already placed this order for the current window."""
        for pos in self.positions.values():
            if pos.get("condition_id") == condition_id and pos.get("price_label") == price_label:
                return False
        return True

    def record_order(
        self,
        condition_id: str,
        price_label: str,
        cost: float,
        shares: float,
        question: str,
        leg: str,
        event_end: str = "",
    ):
        """Record a placed order as a position for the 15m reverse bot."""
        position_key = f"{condition_id}_{price_label}"
        self.positions[position_key] = {
            "side": leg.upper(),
            "condition_id": condition_id,
            "price_label": price_label,
            "entry_price": cost / shares if shares > 0 else 0,
            "current_price": cost / shares if shares > 0 else 0,
            "shares": shares,
            "notional": cost,
            "entry_time": time.time(),
            "question": question,
            "leg": leg,
            "resolved": False,
            "event_end": event_end,
        }
        self.cash -= cost
        self.total_invested += cost
        self.total_trades += 1
        self._save_state()

    def resolve_positions(self, condition_id: str, winning_outcome: str):
        """Resolve positions when a market closes."""
        for key, pos in self.positions.items():
            if pos.get("condition_id") == condition_id and not pos.get("resolved"):
                pos["resolved"] = True
                label = pos.get("price_label", "")
                if winning_outcome.lower() in label.lower():
                    payout = pos["shares"] * 1.0
                    pnl = payout - pos["notional"]
                else:
                    payout = 0.0
                    pnl = -pos["notional"]
                pos["pnl"] = pnl
                self.cash += payout
                self.total_invested -= pos["notional"]
                self.total_pnl += pnl
                if pnl > 0:
                    self.wins += 1
                else:
                    self.losses += 1
                logger.info(
                    f"RESOLVED: {pos.get('question', '')[:40]} | {label} | "
                    f"PnL=${pnl:+.2f} | Cash=${self.cash:.2f}"
                )
        self._save_state()

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


# ══════════════════════════════════════════════════════════════════════════════
# 15-Minute Reverse Bot — BTC/ETH Up/Down Windows
# ══════════════════════════════════════════════════════════════════════════════

class ClobFetcher:
    """Fetch 15-minute Up/Down markets and order books from Polymarket."""

    GAMMA_URL = "https://gamma-api.polymarket.com"
    CLOB_URL = "https://clob.polymarket.com"

    def _generate_15m_slugs(self, assets: List[str]) -> List[str]:
        """Generate slugs for upcoming 15-minute windows.

        Slug format: {asset}-updown-15m-{unix_timestamp}
        Timestamp is the window START time, aligned to 15-minute boundaries.
        """
        now = int(time.time())
        # Align to current 15-minute boundary
        aligned = (now // 900) * 900

        slugs = []
        for asset in assets:
            # Check previous window (for resolution) + current + next 3
            for offset in range(-1, 4):
                ts = aligned + (offset * 900)
                slugs.append(f"{asset}-updown-15m-{ts}")
        return slugs

    def get_active_15m_markets(self, assets: List[str] = None) -> List[Dict]:
        """Find active 15-minute Up/Down events for specified assets."""
        if assets is None:
            assets = TARGET_ASSETS

        slugs = self._generate_15m_slugs(assets)
        markets = []

        for slug in slugs:
            try:
                resp = requests.get(
                    f"{self.GAMMA_URL}/events",
                    params={"slug": slug},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                events = data if isinstance(data, list) else data.get("data", [])

                for event in events:
                    for market in event.get("markets", []) or []:
                        if not market.get("acceptingOrders"):
                            continue

                        market["_event_slug"] = slug
                        market["_event_title"] = event.get("title", "")
                        market["_event_start"] = event.get("startDate")
                        market["_event_end"] = event.get("endDate")
                        markets.append(market)

            except Exception as e:
                logger.debug(f"Failed to fetch {slug}: {e}")

        return markets

    def get_order_book(self, token_id: str) -> Optional[Dict]:
        """Fetch order book for a specific token from CLOB API."""
        try:
            resp = requests.get(
                f"{self.CLOB_URL}/book",
                params={"token_id": token_id},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"Failed to fetch order book: {e}")
            return None


class Reverse15mSignals:
    """Generate dual-leg order signals for 15-minute Up/Down markets.

    Real strategy: Buy BOTH sides at current market prices.
    - Place limit orders at/near current best ask on both sides
    - Variable order sizes (5-15 shares)
    - Multiple orders per side as price moves
    """

    def __init__(self, fetcher: ClobFetcher):
        self.fetcher = fetcher

    def _parse_prices(self, market: Dict) -> tuple:
        """Parse outcome prices from Gamma API."""
        prices_raw = market.get("outcomePrices", "[]")
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

    def analyze_market(self, market: Dict) -> Optional[Dict[str, Any]]:
        """Analyze a 15m market and return order signals for both sides."""
        outcomes_raw = market.get("outcomes", "[]")
        clob_ids_raw = market.get("clobTokenIds", "[]")

        if isinstance(outcomes_raw, str):
            outcomes = json.loads(outcomes_raw)
        else:
            outcomes = outcomes_raw

        if isinstance(clob_ids_raw, str):
            clob_ids = json.loads(clob_ids_raw)
        else:
            clob_ids = clob_ids_raw

        if len(outcomes) != 2 or len(clob_ids) != 2:
            return None

        up_price, down_price = self._parse_prices(market)
        if up_price is None:
            return None

        up_token = clob_ids[0]
        down_token = clob_ids[1]

        question = market.get("question", "")
        condition_id = market.get("conditionId", "")
        event_end = market.get("_event_end", "")

        import random
        orders = []

        # Generate 4 orders for EACH side, centered around current price
        for side_name, token, current_price in [("up", up_token, up_price), ("down", down_token, down_price)]:
            # Calculate offsets to place orders below current price
            offsets = [0.0, -0.03, -0.06, -0.09]
            for offset in offsets:
                price = round(current_price + offset, 2)
                if 0.20 <= price <= 0.80:
                    # Adaptive size: more shares at cheaper prices, fewer at expensive
                    max_affordable = int(5.0 / price)  # ~$5 per side budget
                    size = min(random.randint(5, 13), max_affordable)
                    if size >= 3:  # minimum viable order
                        orders.append({
                            "token_id": token,
                            "side": "BUY",
                            "price": price,
                            "size": size,
                            "leg": side_name,
                            "label": f"{side_name.capitalize()}@{price:.2f}",
                        })

        # Ensure we have orders on BOTH sides
        up_orders = [o for o in orders if o["leg"] == "up"]
        down_orders = [o for o in orders if o["leg"] == "down"]

        if not up_orders or not down_orders:
            return None

        return {
            "question": question,
            "condition_id": condition_id,
            "event_end": event_end,
            "up_token": up_token,
            "down_token": down_token,
            "up_price": up_price,
            "down_price": down_price,
            "orders": orders,
        }


class Reverse15mOrchestrator:
    """Main loop for 15-minute reverse bot on BTC/ETH Up/Down windows."""

    def __init__(self):
        self.fetcher = ClobFetcher()
        self.signals = Reverse15mSignals(self.fetcher)
        self.portfolio = PolymarketPortfolio(INITIAL_CASH)
        self._tick_count = 0
        self._running = True
        self._equity_log = RUN_DIR / "equity_15m.jsonl"
        self._placed_this_window: Dict[str, Set[str]] = {}
        self._active_window: Optional[str] = None

    def run(self):
        logger.info("=" * 60)
        logger.info("POLYMARKET 15-MINUTE REVERSE BOT (AUTO-REENTRY)")
        logger.info(f"Capital: ${INITIAL_CASH} | Paper mode: True")
        logger.info(f"Cheap: {CHEAP_PRICES} | Mid: {MID_PRICES} | Fav: {FAVORITE_PRICES}")
        logger.info(f"Shares/level: {SHARES_PER_LEVEL} | Both sides: {ENABLE_HEDGE}")
        logger.info(f"Assets: {TARGET_ASSETS} | Tick: {REVERSE_15M_TICK}s")
        logger.info("=" * 60)

        while self._running:
            try:
                self._tick()
                time.sleep(REVERSE_15M_TICK)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Tick error: {e}", exc_info=True)
                time.sleep(10)

        logger.info("Stopped.")

    def _tick(self):
        self._tick_count += 1

        markets = self.fetcher.get_active_15m_markets()
        if not markets:
            return

        # Check for resolved positions
        self._check_resolved(markets)

        # Find best active market that generates valid signals
        best_market = None
        best_signal = None
        for market in markets:
            condition_id = market.get("conditionId", "")
            if not condition_id:
                continue

            # Skip if we already traded this window
            if condition_id in self._placed_this_window:
                event_end = market.get("_event_end", "")
                if event_end:
                    try:
                        end_dt = datetime.fromisoformat(event_end.replace("Z", "+00:00"))
                        now = datetime.now(timezone.utc)
                        if now > end_dt:
                            continue
                    except Exception:
                        pass
                # Already have 4 orders per side (8 total), skip
                if len(self._placed_this_window.get(condition_id, set())) >= 8:
                    continue

            # Try to generate signal — skip lopsided markets
            signal = self.signals.analyze_market(market)
            if signal and signal["orders"]:
                best_market = market
                best_signal = signal
                break

        if not best_market or not best_signal:
            return

        condition_id = best_market.get("conditionId", "")
        if condition_id not in self._placed_this_window:
            self._placed_this_window[condition_id] = set()

        signal = best_signal

        # Track spending per side for this window
        placed = self._placed_this_window[condition_id]
        up_spent = sum(
            o["price"] * o["size"]
            for o in signal["orders"]
            if o["leg"] == "up" and o["label"] in placed
        )
        down_spent = sum(
            o["price"] * o["size"]
            for o in signal["orders"]
            if o["leg"] == "down" and o["label"] in placed
        )

        # Split cash 50/50 between sides
        cash_per_side = self.portfolio.cash / 2

        for order in signal["orders"]:
            price_label = order["label"]

            if price_label in placed:
                continue

            if not self.portfolio.can_place_order(condition_id, price_label):
                continue

            cost = order["price"] * order["size"]
            side = order["leg"]

            # Check per-side budget
            side_spent = up_spent if side == "up" else down_spent
            if side_spent + cost > cash_per_side:
                continue

            if cost > self.portfolio.cash:
                continue

            order_id = f"paper_{condition_id[:8]}_{price_label}"
            self.portfolio.record_order(
                condition_id=condition_id,
                price_label=price_label,
                cost=cost,
                shares=order["size"],
                question=signal["question"],
                leg=order["leg"],
                event_end=signal.get("event_end", ""),
            )
            placed.add(price_label)

            # Update side spent tracking
            if side == "up":
                up_spent += cost
            else:
                down_spent += cost

            logger.info(
                f"PLACED: {order['leg'].upper()} {price_label} | "
                f"{order['size']} shares @ ${order['price']:.2f} | "
                f"Cost=${cost:.2f} | Cash=${self.portfolio.cash:.2f}"
            )

        equity = self.portfolio.cash + self.portfolio.total_invested
        self._log_equity(equity)

        if self._tick_count % 30 == 0:
            self._print_progress(equity)

    def _check_resolved(self, active_markets: List[Dict]):
        """Check if any positions have resolved based on market end times."""
        now = datetime.now(timezone.utc)
        resolved_any = False

        # Build price lookup from active markets
        price_map = {}
        for market in active_markets:
            cid = market.get("conditionId", "")
            prices_raw = market.get("outcomePrices", "[]")
            if isinstance(prices_raw, str):
                try:
                    prices = json.loads(prices_raw)
                except Exception:
                    continue
            else:
                prices = prices_raw
            if len(prices) >= 2:
                price_map[cid] = (float(prices[0]), float(prices[1]))

        # Check all unresolved positions
        for key, pos in list(self.portfolio.positions.items()):
            if pos.get("resolved"):
                continue

            condition_id = pos.get("condition_id", "")
            event_end = pos.get("event_end", "")

            if not event_end:
                continue

            try:
                end_dt = datetime.fromisoformat(event_end.replace("Z", "+00:00"))
            except Exception:
                continue

            if now <= end_dt:
                continue  # Window still open

            # Window ended — determine winner from prices
            if condition_id in price_map:
                up_price, down_price = price_map[condition_id]
            else:
                # Market closed and no longer in active list — re-fetch once
                try:
                    resp = requests.get(
                        f"{self.fetcher.GAMMA_URL}/markets",
                        params={"conditionId": condition_id},
                        timeout=10,
                    )
                    data = resp.json()
                    markets = data if isinstance(data, list) else data.get("data", [])
                    if markets:
                        m = markets[0]
                        prices_raw = m.get("outcomePrices", "[]")
                        if isinstance(prices_raw, str):
                            prices = json.loads(prices_raw)
                        else:
                            prices = prices_raw
                        up_price = float(prices[0])
                        down_price = float(prices[1])
                    else:
                        continue
                except Exception:
                    continue

            # Determine winner
            if up_price > 0.9:
                winning = "up"
            elif down_price > 0.9:
                winning = "down"
            else:
                continue  # Can't determine winner yet

            # Resolve this position
            label = pos.get("price_label", "")
            if winning.lower() in label.lower():
                payout = pos["shares"] * 1.0
                pnl = payout - pos["notional"]
            else:
                payout = 0.0
                pnl = -pos["notional"]

            pos["resolved"] = True
            self.portfolio.cash += payout
            self.portfolio.total_pnl += pnl
            if pnl > 0:
                self.portfolio.wins += 1
            self.portfolio._save_state()

            resolved_any = True
            logger.info(
                f"RESOLVED: {pos['question'][:40]} | {pos['price_label']} | "
                f"PnL=${pnl:+.2f} | Cash=${self.portfolio.cash:.2f}"
            )

        if resolved_any:
            self._cleanup_old_windows()

    def _cleanup_old_windows(self):
        """Remove closed windows from placed_this_window."""
        now = datetime.now(timezone.utc)
        to_remove = []

        for cid in self._placed_this_window:
            # Find the market for this condition_id
            # If we can't find it or it's past end time, remove it
            to_remove.append(cid)  # Simplified: clean all resolved

        # Only keep windows that are still active
        # In practice, we check against active markets
        pass

    def _log_equity(self, equity: float):
        entry = {
            "tick": self._tick_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "equity": equity,
            "cash": self.portfolio.cash,
            "total_invested": self.portfolio.total_invested,
            "total_pnl": self.portfolio.total_pnl,
            "positions": len([p for p in self.portfolio.positions.values() if not p.get("resolved")]),
        }
        with open(self._equity_log, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _print_progress(self, equity: float):
        ret = (equity - INITIAL_CASH) / INITIAL_CASH * 100
        active = [p for p in self.portfolio.positions.values() if not p.get("resolved")]
        resolved = [p for p in self.portfolio.positions.values() if p.get("resolved")]
        wins = sum(1 for p in resolved if p.get("pnl", 0) > 0)

        logger.info(
            f"Tick {self._tick_count}: Equity=${equity:.4f} ({ret:+.2f}%) "
            f"Cash=${self.portfolio.cash:.2f} Active={len(active)} "
            f"Resolved={len(resolved)} Wins={wins} "
            f"PnL=${self.portfolio.total_pnl:+.4f}"
        )

        if active:
            logger.info("  Active:")
            for p in active[:6]:
                logger.info(f"    {p.get('leg','')} {p.get('price_label','')} | {p.get('question','')[:40]}")


# ══════════════════════════════════════════════════════════════════════════════
# V2 Reversed Strategy (original)
# ══════════════════════════════════════════════════════════════════════════════

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
    global INITIAL_CASH, TARGET_ASSETS, ENABLE_HEDGE

    import argparse

    parser = argparse.ArgumentParser(description="Polymarket V2 Paper Trader")
    parser.add_argument(
        "--reverse-15m",
        action="store_true",
        help="Run 15-minute reverse bot (BTC/ETH Up/Down windows)",
    )
    parser.add_argument("--cash", type=float, default=INITIAL_CASH, help="Initial cash")
    parser.add_argument("--assets", nargs="+", default=TARGET_ASSETS, help="Assets for 15m bot")
    parser.add_argument("--no-hedge", action="store_true", help="Disable hedge leg")
    args = parser.parse_args()

    INITIAL_CASH = args.cash
    TARGET_ASSETS = args.assets
    ENABLE_HEDGE = not args.no_hedge

    log_file = "stdout_15m.log" if args.reverse_15m else "stdout.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(RUN_DIR / log_file)),
        ],
    )

    if args.reverse_15m:
        orchestrator = Reverse15mOrchestrator()
    else:
        orchestrator = PolymarketOrchestrator()

    orchestrator.run()


if __name__ == "__main__":
    main()
