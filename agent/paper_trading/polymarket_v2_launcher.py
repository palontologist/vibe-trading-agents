"""Polymarket Paper Trader V2 — Reversed + 15-Minute Reverse Bot.

Two strategies in one file:

1. V2 REVERSED (original): Ride favorites, fade longshots on high-volume markets
   - BUY YES on favorites (>0.70 prob)
   - BUY NO on longshots (<0.30 prob)
   - Quick in/out — TP 5%, SL 10%

2. 15-MINUTE REVERSE BOT: DCA both sides on BTC/ETH Up/Down windows
   - Buy BOTH sides (Up AND Down) at current market price
   - Scale in over time — multiple orders per side
   - Typical entry: 20¢-70¢ range
   - Hold to resolution — one side wins $1, one side worth $0
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

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

INITIAL_CASH = float(os.environ.get("INITIAL_CASH", "9.57"))
MAX_POSITION_PCT = 0.25  # 25% per market
MAX_POSITIONS = 8
TAKE_PROFIT_PCT = 0.05  # 5%
STOP_LOSS_PCT = 0.10  # 10%
MIN_VOLUME_24H = 50000  # $50K minimum
TICK_INTERVAL = 30  # seconds

# ── Ultra-Cheap Dislocation Strategy ─────────────────────────────────────────

class UltraCheapLiveSignals:
    """Generate deep-discount limit orders for 15m windows."""
    def __init__(self):
        self.cheap_prices = [0.05, 0.07, 0.10]

    def analyze_market(self, market: Dict) -> Optional[Dict[str, Any]]:
        outcomes_raw = market.get("outcomes", "[]")
        clob_ids_raw = market.get("clobTokenIds", "[]")
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        clob_ids = json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw

        if len(outcomes) != 2 or len(clob_ids) != 2: return None

        orders = []
        for i in range(2): # Both sides
            token_id = clob_ids[i]
            outcome = outcomes[i]
            for price in self.cheap_prices:
                orders.append({
                    "token_id": token_id,
                    "price": price,
                    "size": 10, # Small fixed sizing
                    "leg": outcome,
                    "label": f"{outcome}@{price:.2f}",
                })
        return {"question": market.get("question", ""), "condition_id": market.get("conditionId", ""), "orders": orders}

class UltraCheapLiveOrchestrator:
    """Live loop for Ultra-Cheap Dislocation strategy."""
    def __init__(self, live: bool = True):
        self.fetcher = ClobFetcher()
        self.signals = UltraCheapLiveSignals()
        self.portfolio = PolymarketPortfolio(INITIAL_CASH, state_path=LIVE_STATE)
        self.live = live
        self.trader = LiveTrader() if live else None
        if live: self.portfolio.sync_cash_from_clob(self.trader.balance_usd)
        self._tick_count = 0
        self._placed_windows: Dict[str, Set[str]] = {}
        self._live_orders: Dict[str, Dict] = {} # order_id -> details

    def run(self):
        logger.info("=" * 60)
        logger.info("ULTRA-CHEAP LIVE BOT (Sprinting with $1)")
        logger.info(f"Budget: ${self.portfolio.cash:.2f} | Entries: [0.05, 0.07, 0.10]")
        logger.info("=" * 60)
        while True:
            try:
                self._tick()
                time.sleep(5)
            except KeyboardInterrupt: break
            except Exception as e:
                logger.error(f"Tick error: {e}", exc_info=True)
                time.sleep(10)

    def _tick(self):
        self._tick_count += 1
        markets = self.fetcher.get_active_15m_markets()
        
        # 1. Check for Resolutions (New)
        all_markets = self.fetcher.get_all_15m_markets()
        self._check_resolved(all_markets)

        # 2. Check Fills
        if self.live and self.trader:
            open_orders = {o.get("id") for o in self.trader.get_open_orders()}
            for oid in list(self._live_orders.keys()):
                if oid not in open_orders:
                    details = self._live_orders.pop(oid)
                    logger.info(f"FILL: {details['leg']} {details['label']} matched!")
                    self.portfolio.record_order(details['cid'], details['label'], 
                                              details['price']*details['size'], details['size'], 
                                              details['q'], details['leg'])

        # 3. Place New Limits
        for m in markets:
            cid = m.get("conditionId", "")
            if not cid or not m.get("acceptingOrders", False): continue
            if cid in self._placed_windows and len(self._placed_windows[cid]) >= 12: continue
            
            sig = self.signals.analyze_market(m)
            if not sig: continue
            
            if cid not in self._placed_windows: self._placed_windows[cid] = set()
            
            for order in sig["orders"]:
                label = order["label"]
                if label in self._placed_windows[cid]: continue
                
                cost = order["price"] * order["size"]
                if cost > self.portfolio.cash: continue
                
                live_res = self.trader.place_limit_buy(order["token_id"], order["price"], order["size"])
                if live_res:
                    oid = live_res["order_id"]
                    self._live_orders[oid] = {
                        "cid": cid, "label": label, "price": order["price"], 
                        "size": order["size"], "leg": order["leg"], "q": sig["question"]
                    }
                    self._placed_windows[cid].add(label)
                    self.portfolio.record_order(cid, label, cost, order["size"], sig["question"], order["leg"])
                    logger.info(f"[LIVE-LIMIT] {label} placed for {cid[:8]}")

        if self._tick_count % 20 == 0:
            logger.info(f"Tick {self._tick_count}: Cash=${self.portfolio.cash:.2f} | Pending={len(self._live_orders)}")

    def _check_resolved(self, active_markets: List[Dict]):
        """Check if any positions have resolved based on market end times."""
        now = datetime.now(timezone.utc)
        resolved_any = False

        price_map = {}
        for market in active_markets:
            cid = market.get("conditionId", "")
            prices_raw = market.get("outcomePrices", "[]")
            if isinstance(prices_raw, str):
                try: prices = json.loads(prices_raw)
                except Exception: continue
            else: prices = prices_raw
            if len(prices) >= 2: price_map[cid] = (float(prices[0]), float(prices[1]))

        for key, pos in list(self.portfolio.positions.items()):
            if pos.get("resolved"): continue
            condition_id = pos.get("condition_id", "")
            event_end = pos.get("event_end", "")
            if not event_end: continue
            try:
                end_dt = datetime.fromisoformat(event_end.replace("Z", "+00:00"))
            except Exception: continue
            if now <= end_dt: continue

            if condition_id in price_map:
                up_price, down_price = price_map[condition_id]
            else:
                continue

            if up_price > 0.9: winning = "up"
            elif down_price > 0.9: winning = "down"
            else: continue

            label = pos.get("price_label", "")
            is_winner = winning.lower() in label.lower()
            payout = pos["shares"] * 1.0 if is_winner else 0.0
            pnl = payout - pos["notional"]

            pos["resolved"] = True
            self.portfolio.cash += payout
            self.portfolio.total_pnl += pnl
            if is_winner: self.portfolio.wins += 1
            self.portfolio._save_state()
            resolved_any = True
            logger.info(f"RESOLVED: {pos['question'][:40]} | {label} | {'WIN' if is_winner else 'LOSS'} | PnL=${pnl:+.2f} | Cash=${self.portfolio.cash:.2f}")

        if resolved_any:
            self._placed_windows.clear()
REVERSE_15M_TICK = 5  # seconds between scans
# Real strategy (observed from owner): ALL-IN underdog side, NO hedge
# Buy UP on both BTC and ETH at 20¢-35¢ limit, DCA climbing the ladder
# 90 shares max per order, multiple price levels
CHEAP_BUY_MIN = 0.20   # cheapest limit for underdog
CHEAP_BUY_MAX = 0.35   # climb up to 35¢
CHEAP_ORDER_USDC = 1.80  # $1.80 per order (90 shares × 20¢)
MAX_SHARES_PER_ORDER = 90  # max shares per order (matches owner)
ENABLE_EXPENSIVE_HEDGE = False  # NO hedge — owner goes all-in same direction
EXPENSIVE_BUY_MIN = 0.84  # unused when hedge disabled
EXPENSIVE_BUY_MAX = 0.85  # unused when hedge disabled
EXPENSIVE_ORDER_USDC = 1.00  # unused when hedge disabled
TARGET_ASSETS = ["btc", "eth"]  # trade BOTH BTC and ETH on same window

RUN_DIR = Path(__file__).resolve().parent.parent / "paper_runs" / "polymarket_v2"
RUN_DIR.mkdir(parents=True, exist_ok=True)

# Separate state files for paper vs live
PAPER_STATE = RUN_DIR / "paper_state.json"
LIVE_STATE = RUN_DIR / "live_state.json"


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

    def __init__(self, initial_cash: float = 10.0, state_path: Path = None):
        self.cash = initial_cash
        self.initial_cash = initial_cash
        self.positions: Dict[str, Dict] = {}
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.total_invested = 0.0
        self._state_file = state_path or PAPER_STATE
        self._load_state()

    def _state_path(self):
        return self._state_file

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

    def sync_cash_from_clob(self, clob_balance: float):
        """Sync portfolio cash with actual CLOB balance. Clear stale paper state."""
        old_cash = self.cash
        self.cash = clob_balance
        resolved = [p for p in self.positions.values() if p.get("resolved")]
        active = {k: v for k, v in self.positions.items() if not v.get("resolved")}
        self.positions = active
        self.total_invested = sum(p.get("notional", 0) for p in active.values())
        self._save_state()
        logger.info(f"Portfolio synced: CLOB=${clob_balance:.2f} (was ${old_cash:.2f}) | Active={len(active)} Resolved={len(resolved)}")

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
        order_id: Optional[str] = None,
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
            "order_id": order_id,
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
        """Find OPEN 15-minute Up/Down events for specified assets."""
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
                        market["_event_slug"] = slug
                        market["_event_title"] = event.get("title", "")
                        market["_event_start"] = event.get("startDate")
                        market["_event_end"] = event.get("endDate")
                        markets.append(market)

            except Exception as e:
                logger.debug(f"Failed to fetch {slug}: {e}")

        return markets

    def get_all_15m_markets(self, assets: List[str] = None) -> List[Dict]:
        """Find ALL 15-minute markets including closed (for resolution)."""
        if assets is None:
            assets = TARGET_ASSETS

        now = int(time.time())
        aligned = (now // 900) * 900
        markets = []

        for asset in assets:
            for offset in range(-2, 4):
                ts = aligned + (offset * 900)
                slug = f"{asset}-updown-15m-{ts}"
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
                            market["_event_slug"] = slug
                            market["_event_end"] = event.get("endDate")
                            markets.append(market)
                except Exception:
                    pass

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


class LiveTrader:
    """Place real orders on Polymarket CLOB using py-clob-client-v2."""

    CLOB_HOST = "https://clob.polymarket.com"
    CHAIN_ID = 137
    DEPOSIT_WALLET = "0x3B4D8B57a729799a49ce259580cADaC29B4d1aB8"
    PRIVATE_KEY = os.environ.get(
        "POLYMARKET_PRIVATE_KEY",
        os.environ.get("POLYMARKET_PRIVATE_KEY", ""),
    )

    def __init__(self):
        self._client = None
        self._init_client()

    def _init_client(self):
        try:
            from py_clob_client_v2 import ClobClient, ApiCreds
            from py_clob_client_v2.clob_types import BalanceAllowanceParams

            boot = ClobClient(
                host=self.CLOB_HOST,
                chain_id=self.CHAIN_ID,
                key=self.PRIVATE_KEY,
                signature_type=3,
                funder=self.DEPOSIT_WALLET,
            )
            # Try derive creds (reuse existing)
            try:
                creds = boot.derive_api_key()
            except Exception:
                creds = boot.create_or_derive_api_key()

            self._client = ClobClient(
                host=self.CLOB_HOST,
                chain_id=self.CHAIN_ID,
                key=self.PRIVATE_KEY,
                creds=creds,
                signature_type=3,
                funder=self.DEPOSIT_WALLET,
            )

            # Sync balance
            params = BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=3)
            self._client.update_balance_allowance(params)
            bal = self._client.get_balance_allowance(params)
            self._balance = int(bal.get("balance", 0))
            logger.info(f"LIVE TRADER: Deposit wallet {self.DEPOSIT_WALLET[:12]}... | Balance: ${self._balance / 10**6:.2f}")

        except Exception as e:
            logger.error(f"Failed to init live trader: {e}")
            self._client = None

    @property
    def balance_usd(self) -> float:
        return self._balance / 10**6 if self._balance else 0.0

    def refresh_balance(self):
        if not self._client:
            return
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams
            params = BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=3)
            self._client.update_balance_allowance(params)
            bal = self._client.get_balance_allowance(params)
            self._balance = int(bal.get("balance", 0))
        except Exception as e:
            logger.warning(f"Balance refresh failed: {e}")

    def place_limit_buy(self, token_id: str, price: float, size: int) -> Optional[Dict]:
        """Place a GTC limit BUY order. Returns order dict or None."""
        if not self._client:
            logger.error("Live trader not initialized")
            return None

        if size < 5:
            logger.warning(f"Size {size} below minimum (5), skipping")
            return None

        # Enforce $1 minimum order value
        if price * size < 1.0:
            size = max(5, int(1.0 / price) + 1)
            logger.info(f"Adjusted size to {size} to meet $1 minimum")

        try:
            from py_clob_client_v2 import OrderType, Side
            from py_clob_client_v2.clob_types import OrderArgs, CreateOrderOptions

            resp = self._client.create_and_post_order(
                OrderArgs(
                    price=price,
                    size=size,
                    side=Side.BUY,
                    token_id=token_id,
                ),
                CreateOrderOptions(tick_size="0.01", neg_risk=False),
                OrderType.GTC,
            )

            if resp.get("success"):
                logger.info(f"LIVE ORDER: BUY {size} @ ${price:.2f} | ID={resp.get('orderID', '?')[:16]}...")
                return {
                    "order_id": resp.get("orderID"),
                    "status": resp.get("status"),
                    "price": price,
                    "size": size,
                }
            else:
                logger.warning(f"Order failed: {resp}")
                return None

        except Exception as e:
            logger.error(f"Order error: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        if not self._client:
            return False
        try:
            self._client.cancel(order_id)
            return True
        except Exception as e:
            logger.warning(f"Cancel failed: {e}")
            return False

    def cancel_all(self) -> bool:
        if not self._client:
            return False
        try:
            self._client.cancel_all()
            return True
        except Exception as e:
            logger.warning(f"Cancel all failed: {e}")
            return False

    def get_open_orders(self) -> List[Dict]:
        """Get all open orders for the deposit wallet."""
        if not self._client:
            return []
        try:
            orders = self._client.get_open_orders()
            return orders if isinstance(orders, list) else []
        except Exception as e:
            logger.warning(f"Get open orders failed: {e}")
            return []

    def check_order_status(self, order_id: str) -> Optional[str]:
        """Check if an order is filled, open, or cancelled."""
        if not self._client:
            return None
        try:
            order = self._client.get_order(order_id)
            if order:
                return order.get("status", "unknown")
        except Exception:
            pass
        return None

    def cancel_stale_orders(self, max_age_seconds: int = 900) -> int:
        """Cancel orders older than max_age_seconds (default 15 min)."""
        if not self._client:
            return 0
        try:
            orders = self.get_open_orders()
            cancelled = 0
            now = time.time()
            for order in orders:
                created = order.get("createdAt", 0)
                if isinstance(created, (int, float)) and (now - created) > max_age_seconds:
                    oid = order.get("id", "")
                    if oid:
                        self.cancel_order(oid)
                        cancelled += 1
            return cancelled
        except Exception as e:
            logger.warning(f"Cancel stale failed: {e}")
            return 0


class Reverse15mSignals:
    """Generate order signals matching the original TypeScript strategy.

    Strategy:
    - CHEAP: Buy the underdog (cheapest side) at 7¢-10¢ limit
    - EXPENSIVE: Hedge by buying the favorite at 90¢-95¢ limit
    - Place limit orders across the price range
    - Hold to resolution — one side wins $1, other goes to $0
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

    def _cheap_limit_prices(self) -> List[float]:
        """Generate cheap limit prices: 0.20, 0.25, 0.30, 0.35 (DCA ladder)"""
        prices = []
        p = CHEAP_BUY_MIN
        while p <= CHEAP_BUY_MAX + 0.0001:
            prices.append(round(p, 2))
            p += 0.05  # 5¢ increments like the real owner
        return prices

    def _expensive_limit_prices(self) -> List[float]:
        """Generate expensive limit prices: 0.90, 0.91, 0.92, 0.93, 0.94, 0.95"""
        prices = []
        p = EXPENSIVE_BUY_MIN
        while p <= EXPENSIVE_BUY_MAX + 0.0001:
            prices.append(round(p, 2))
            p += 0.01
        return prices

    def _compute_size(self, usdc_budget: float, price: float) -> int:
        """Compute share size capped at MAX_SHARES_PER_ORDER."""
        shares = usdc_budget / max(price, 0.01)
        capped = min(shares, MAX_SHARES_PER_ORDER)
        return max(1, int(capped))

    def analyze_market(self, market: Dict) -> Optional[Dict[str, Any]]:
        """Analyze a 15m market and return cheap + expensive order signals.

        Matches TypeScript findOpportunities():
        - pickReverseToken: cheapest side with bestAsk
        - cheap orders: buy underdog at 7¢-10¢
        - expensive hedge: buy favorite at 90¢-95¢
        """
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

        # pickReverseToken: the CHEAPER outcome is the underdog
        if up_price <= down_price:
            reverse_token = up_token
            reverse_price = up_price
            reverse_outcome = "up"
            favorite_token = down_token
            favorite_price = down_price
        else:
            reverse_token = down_token
            reverse_price = down_price
            reverse_outcome = "down"
            favorite_token = up_token
            favorite_price = up_price

        orders = []

        # Real strategy: ALL-IN underdog side at DCA price ladder, NO hedge
        cheap_prices = self._cheap_limit_prices()

        for price in cheap_prices:
            size = self._compute_size(CHEAP_ORDER_USDC, price)
            orders.append({
                "token_id": reverse_token,
                "side": "BUY",
                "price": price,
                "size": size,
                "leg": reverse_outcome,
                "label": f"{reverse_outcome}@{price:.2f}",
                "kind": "cheap",
            })

        if not orders:
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

    def __init__(self, live: bool = False):
        self.fetcher = ClobFetcher()
        self.signals = Reverse15mSignals(self.fetcher)
        state_path = LIVE_STATE if live else PAPER_STATE
        self.portfolio = PolymarketPortfolio(INITIAL_CASH, state_path=state_path)
        self.live = live
        self.trader: Optional[LiveTrader] = None
        if live:
            self.trader = LiveTrader()
            self.portfolio.sync_cash_from_clob(self.trader.balance_usd)
        self._tick_count = 0
        self._running = True
        self._equity_log = RUN_DIR / ("equity_15m_live.jsonl" if live else "equity_15m_paper.jsonl")
        self._placed_this_window: Dict[str, Set[str]] = {}
        self._active_window: Optional[str] = None
        self._live_order_ids: List[str] = []  # track live order IDs

    def run(self):
        mode = "LIVE" if self.live else "PAPER"
        logger.info("=" * 60)
        logger.info(f"POLYMARKET 15-MINUTE REVERSE BOT — {mode} MODE")
        if self.live:
            logger.info(f"Deposit wallet: {LiveTrader.DEPOSIT_WALLET}")
            logger.info(f"Balance: ${self.trader.balance_usd:.2f}")
        else:
            logger.info(f"Capital: ${self.portfolio.cash:.2f}")
        logger.info(f"Cheap: {CHEAP_BUY_MIN}-{CHEAP_BUY_MAX} (${CHEAP_ORDER_USDC}/order) | Hedge: {ENABLE_EXPENSIVE_HEDGE} | Assets: {TARGET_ASSETS}")
        logger.info(f"Max shares: {MAX_SHARES_PER_ORDER} | Hedge: {ENABLE_EXPENSIVE_HEDGE} | Assets: {TARGET_ASSETS}")
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

        if self._tick_count % 6 == 0:
            active = [p for p in self.portfolio.positions.values() if not p.get("resolved")]
            logger.info(f"Tick {self._tick_count}: Cash=${self.portfolio.cash:.2f} Markets={len(markets)} Active={len(active)}")

        # Check for resolved positions (uses all markets including closed)
        all_markets = self.fetcher.get_all_15m_markets()
        self._check_resolved(all_markets)

        # Live mode: check fills and cancel stale orders
        if self.live and self.trader and self._tick_count % 12 == 0:
            self._check_fills()

        # Place orders on ALL qualifying markets (BTC + ETH on same window)
        for market in markets:
            if self.portfolio.cash < 1.0:
                break

            condition_id = market.get("conditionId", "")
            if not condition_id:
                continue

            if not market.get("acceptingOrders", False):
                continue

            if condition_id in self._placed_this_window:
                if len(self._placed_this_window.get(condition_id, set())) >= 20:
                    continue

            signal = self.signals.analyze_market(market)
            if not signal or not signal["orders"]:
                continue

            if condition_id not in self._placed_this_window:
                self._placed_this_window[condition_id] = set()

            for order in signal["orders"]:
                price_label = order["label"]

                if price_label in self._placed_this_window[condition_id]:
                    continue

                if not self.portfolio.can_place_order(condition_id, price_label):
                    continue

                if self.live and order["token_id"] in getattr(self, '_bad_tokens', set()):
                    continue

                cost = order["price"] * order["size"]

                if cost < 1.0 or order["size"] < 5:
                    order["size"] = max(5, int(1.0 / order["price"]) + 1)
                    cost = order["price"] * order["size"]

                if cost > self.portfolio.cash:
                    continue

                order_id = f"paper_{condition_id[:8]}_{price_label}"

                live_order = None
                if self.live and self.trader:
                    live_order = self.trader.place_limit_buy(
                        token_id=order["token_id"],
                        price=order["price"],
                        size=order["size"],
                    )
                    if live_order:
                        self._live_order_ids.append(live_order["order_id"])
                        order_id = live_order["order_id"]
                    else:
                        if not hasattr(self, '_bad_tokens'):
                            self._bad_tokens = set()
                        self._bad_tokens.add(order["token_id"])
                        continue
                elif not self.live:
                    pass
                else:
                    continue

                self.portfolio.record_order(
                    condition_id=condition_id,
                    price_label=price_label,
                    cost=cost,
                    shares=order["size"],
                    question=signal["question"],
                    leg=order["leg"],
                    event_end=signal.get("event_end", ""),
                    order_id=order_id if self.live else None,
                )
                self._placed_this_window[condition_id].add(price_label)

                tag = "LIVE" if self.live else "PAPER"
                logger.info(
                    f"[{tag}] {order['leg'].upper()} {price_label} | "
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
            is_winner = winning.lower() in label.lower()
            if is_winner:
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
                f"{'WIN' if is_winner else 'LOSS'} | PnL=${pnl:+.2f} | Cash=${self.portfolio.cash:.2f}"
            )

            # Auto-redeem winning positions on-chain (converts shares → pUSD)
            if is_winner and self.live and self.trader:
                self._try_redeem(pos)

            # RE-ENTRY: clear this window from tracker so we can trade the next one
            condition_id = pos.get("condition_id", "")
            if condition_id in self._placed_this_window:
                del self._placed_this_window[condition_id]
                logger.info(f"  [RE-ENTRY] Cleared window {condition_id[:12]}... — ready for next opportunity")

        if resolved_any:
            self._cleanup_old_windows()

    def _try_redeem(self, pos: Dict):
        """Attempt to redeem winning shares on-chain via CTF.redeemPositions."""
        try:
            from web3 import Web3
            from web3.middleware import ExtraDataToPOAMiddleware

            condition_id = pos.get("condition_id", "")
            leg = pos.get("leg", "")
            if not condition_id or not leg:
                return

            w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com", request_kwargs={"timeout": 15}))
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

            eoa = "0xbe2C534B73CEb53F2c16cb5954f5baE03F1f07Cf"
            matic = w3.eth.get_balance(Web3.to_checksum_address(eoa))
            if matic < w3.to_wei(0.05, "ether"):
                logger.info(f"  [REDEEM] Skipped — need MATIC for gas (have {matic / 1e18:.4f})")
                return

            CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
            PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
            private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")


            REDEEM_ABI = [{"inputs":[
                {"name":"collateralToken","type":"address"},
                {"name":"parentCollectionId","type":"bytes32"},
                {"name":"questionId","type":"bytes32"},
                {"name":"outcomeIndexes","type":"uint256[]"}
            ],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"}]

            ctf = w3.eth.contract(address=CTF, abi=REDEEM_ABI)
            outcome_index = 0 if leg == "up" else 1

            txn = ctf.functions.redeemPositions(
                PUSD,
                b'\x00' * 32,
                bytes.fromhex(condition_id[2:]),
                [outcome_index],
            ).build_transaction({
                "from": eoa,
                "nonce": w3.eth.get_transaction_count(Web3.to_checksum_address(eoa)),
                "gas": 200000,
                "maxFeePerGas": w3.eth.gas_price,
                "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
                "chainId": 137,
            })

            signed = w3.eth.account.sign_transaction(txn, private_key)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info(f"  [REDEEM] TX sent: {tx_hash.hex()}")
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt["status"] == 1:
                logger.info(f"  [REDEEM] SUCCESS — shares redeemed to pUSD")
                self.trader.refresh_balance()
                self.portfolio.sync_cash_from_clob(self.trader.balance_usd)
            else:
                logger.warning(f"  [REDEEM] FAILED on-chain")
        except Exception as e:
            logger.warning(f"  [REDEEM] Error: {e}")

    def _check_fills(self):
        """Check if any live orders have filled, cancel stale ones."""
        if not self.trader:
            return

        try:
            # Cancel orders older than 15 minutes
            cancelled = self.trader.cancel_stale_orders(max_age_seconds=900)
            if cancelled:
                logger.info(f"  [FILLS] Cancelled {cancelled} stale orders")

            # Refresh balance after fills
            old_cash = self.portfolio.cash
            self.trader.refresh_balance()
            new_balance = self.trader.balance_usd
            if abs(new_balance - old_cash) > 0.01:
                logger.info(f"  [FILLS] Balance changed: ${old_cash:.2f} → ${new_balance:.2f}")
                self.portfolio.sync_cash_from_clob(new_balance)

            # Clean up order IDs for orders no longer open
            open_orders = self.trader.get_open_orders()
            open_ids = {o.get("id") for o in open_orders if isinstance(o, dict)}
            filled = [oid for oid in self._live_order_ids if oid not in open_ids]
            if filled:
                logger.info(f"  [FILLS] {len(filled)} orders filled/closed")
            self._live_order_ids = [oid for oid in self._live_order_ids if oid in open_ids]

        except Exception as e:
            logger.warning(f"  [FILLS] Error: {e}")

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
    """Main loop for Polymarket V2 Reversed strategy — paper or live."""

    def __init__(self, live: bool = False):
        self.fetcher = PolymarketFetcher()
        self.signals = ReversedSignals()
        self.portfolio = PolymarketPortfolio(INITIAL_CASH)
        self.live = live
        self.trader: Optional[LiveTrader] = None
        if live:
            self.trader = LiveTrader()
            self.portfolio.sync_cash_from_clob(self.trader.balance_usd)
        self._tick_count = 0
        self._running = True
        self._equity_log = RUN_DIR / "equity.jsonl"

    def run(self):
        mode = "LIVE" if self.live else "PAPER"
        logger.info("=" * 60)
        logger.info(f"POLYMARKET V2 — REVERSED STRATEGY — {mode}")
        logger.info(f"Capital: ${self.portfolio.cash:.2f} | Max positions: {MAX_POSITIONS}")
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
                # Live order placement
                if self.live and self.trader:
                    # Get token_id from market
                    outcomes_raw = market.get("outcomes", "[]")
                    clob_ids_raw = market.get("clobTokenIds", "[]")
                    outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                    clob_ids = json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw

                    if len(outcomes) == 2 and len(clob_ids) == 2:
                        side_idx = 0 if signal["side"] == "YES" else 1
                        token_id = clob_ids[side_idx]
                        shares = max(5, int(signal["size_pct"] * self.portfolio.cash / signal["price"]))
                        shares = min(shares, 90)

                        if shares * signal["price"] >= 1.0 and shares * signal["price"] <= self.portfolio.cash:
                            live_order = self.trader.place_limit_buy(
                                token_id=token_id,
                                price=signal["price"],
                                size=shares,
                            )
                            if live_order:
                                logger.info(f"[LIVE] {signal['side']} {signal['question'][:40]} | {shares} shares @ ${signal['price']:.3f}")
                            else:
                                continue

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
    global INITIAL_CASH, TARGET_ASSETS

    import argparse

    parser = argparse.ArgumentParser(description="Polymarket V2 Paper Trader")
    parser.add_argument(
        "--ultra-cheap-live",
        action="store_true",
        help="Run Ultra-Cheap Dislocation live (BTC/ETH 15m windows)",
    )
    parser.add_argument(
        "--reverse-15m",
        action="store_true",
        help="Run 15-minute reverse bot (BTC/ETH Up/Down windows)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="LIVE MODE — place real orders on Polymarket",
    )
    parser.add_argument("--cash", type=float, default=INITIAL_CASH, help="Initial cash")
    parser.add_argument("--assets", nargs="+", default=TARGET_ASSETS, help="Assets for 15m bot")
    args = parser.parse_args()

    INITIAL_CASH = args.cash
    TARGET_ASSETS = args.assets

    # Live only if --live flag passed; DRY_RUN=true forces paper
    live = args.live and os.environ.get("DRY_RUN", "false").lower() != "true"

    log_file = "stdout_15m.log" if args.reverse_15m else "stdout.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(RUN_DIR / log_file)),
        ],
    )

    if args.ultra_cheap_live:
        orchestrator = UltraCheapLiveOrchestrator(live=live)
    elif args.reverse_15m:
        orchestrator = Reverse15mOrchestrator(live=live)
    else:
        orchestrator = PolymarketOrchestrator(live=live)

    orchestrator.run()


if __name__ == "__main__":
    main()
