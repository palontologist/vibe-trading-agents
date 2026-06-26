"""
Polymarket Ultra-Cheap Dislocation Bot
Strategy: Lottery tickets (1-3 cents) on both sides of 15m BTC/ETH windows.
"""

import json
import logging
import os
import time
import requests
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("UltraCheapBot")

# ── Config ────────────────────────────────────────────────────────────────────
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"

# Strategy Parameters (Aligned with TS implementation)
CHEAP_BUY_MIN = 0.05
CHEAP_BUY_MAX = 0.10
EXPENSIVE_BUY_MIN = 0.90
EXPENSIVE_BUY_MAX = 0.95

CHEAP_ORDER_USDC = 0.50    # budget per price level
EXPENSIVE_ORDER_USDC = 0.50
ORDER_SHARES = 5             # Reduced for $1 budget (allows multiple windows)
MAX_SHARES_PER_ORDER = 20 
ENABLE_EXPENSIVE_HEDGE = True

# TP DISABLED - Hold to resolution for max payout
ENTRY_DELAY_SEC = 45         # Start trading 45s after open
CANCEL_BEFORE_SEC = 30       # Cancel unfilled 30s before close
MAX_NOTIONAL_PER_MARKET = 1.0 # Hard cap per window

TARGET_ASSETS = ["btc", "eth"]
MARKET_SLUG_PREFIXES = ["btc-updown-15m", "eth-updown-15m"]
POLL_INTERVAL_SEC = 5
INITIAL_CASH = 1.0

RUN_DIR = Path("./paper_runs/ultra_cheap")
RUN_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = RUN_DIR / "state.json"

@dataclass
class LimitOrder:
    token_id: str
    side: str
    price: float
    shares: int
    outcome: str
    market_id: str
    entry_time: float
    filled: bool = False

@dataclass
class Position:
    token_id: str
    outcome: str
    entry_price: float
    shares: float
    notional: float
    tp1_sold: bool = False
    tp2_sold: bool = False

# ── Market Data ────────────────────────────────────────────────────────────────

class MarketScanner:
    def __init__(self):
        self.gamma_url = GAMMA_HOST
        self.clob_url = CLOB_HOST

    def get_15m_markets(self, assets: List[str]) -> List[Dict]:
        now = int(time.time())
        aligned = (now // 900) * 900
        markets = []

        for asset in assets:
            # Scan current and next windows
            for offset in range(0, 3):
                ts = aligned + (offset * 900)
                slug = f"{asset}-updown-15m-{ts}"
                try:
                    resp = requests.get(f"{self.gamma_url}/events", params={"slug": slug}, timeout=10)
                    resp.raise_for_status()
                    events = resp.json()
                    if not events: continue
                    for event in events:
                        for m in event.get("markets", []):
                            m["_event_slug"] = slug
                            m["_event_end"] = event.get("endDate")
                            m["_event_start"] = event.get("startDate")
                            markets.append(m)
                except Exception:
                    continue
        return markets

    def get_best_ask(self, token_id: str) -> Optional[float]:
        try:
            resp = requests.get(f"{self.clob_url}/book", params={"token_id": token_id}, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            asks = data.get("asks", [])
            if not asks: return None
            return float(asks[0].get("price", 1.0))
        except Exception:
            return None

# ── Portfolio Management ───────────────────────────────────────────────────────

class PaperPortfolio:
    def __init__(self, initial_cash: float):
        self.cash = initial_cash
        self.positions: Dict[str, Position] = {}
        self.pending_orders: List[LimitOrder] = []
        self.total_pnl = 0.0
        self._load_state()

    def _load_state(self):
        if STATE_FILE.exists():
            try:
                d = json.loads(STATE_FILE.read_text())
                self.cash = d.get("cash", INITIAL_CASH)
                self.total_pnl = d.get("total_pnl", 0.0)
                
                # Load positions
                for pos_data in d.get("positions", []):
                    pos_id = pos_data["pos_id"]
                    self.positions[pos_id] = Position(**pos_data["data"])
                
                # Load pending orders
                for ord_data in d.get("pending_orders", []):
                    self.pending_orders.append(LimitOrder(**ord_data))
            except Exception:
                pass

    def save_state(self):
        # Convert dataclasses to dicts
        positions_data = [
            {"pos_id": pid, "data": pos.__dict__} 
            for pid, pos in self.positions.items()
        ]
        pending_data = [ord.__dict__ for ord in self.pending_orders]

        STATE_FILE.write_text(json.dumps({
            "cash": self.cash,
            "total_pnl": self.total_pnl,
            "positions": positions_data,
            "pending_orders": pending_data,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }, indent=2))

    def place_limit_order(self, order: LimitOrder):
        cost = order.price * order.shares
        if self.cash >= cost:
            self.pending_orders.append(order)
            self.cash -= cost
            return True
        return False

    def fill_order(self, order: LimitOrder):
        pos_id = f"{order.market_id}_{order.token_id}"
        self.positions[pos_id] = Position(
            token_id=order.token_id,
            outcome=order.outcome,
            entry_price=order.price,
            shares=float(order.shares),
            notional=order.price * order.shares
        )
        order.filled = True
        logger.info(f"FILL: {order.outcome} @ {order.price} | {order.shares} shares")

    def take_profit(self, pos_id: str, price: float, percent: float):
        pos = self.positions[pos_id]
        shares_to_sell = pos.shares * percent
        proceeds = shares_to_sell * price
        pnl = proceeds - (pos.notional * percent)
        
        self.cash += proceeds
        self.total_pnl += pnl
        pos.shares -= shares_to_sell
        
        logger.info(f"TP: Sold {shares_to_sell:.1f} shares of {pos.outcome} @ {price} | PnL=${pnl:.2f}")
        return pnl

    def cancel_order(self, order: LimitOrder):
        self.cash += (order.price * order.shares)
        order.filled = True 
        logger.info(f"CANCEL: {order.outcome} @ {order.price}")

# ── Bot Orchestrator ───────────────────────────────────────────────────────────

class UltraCheapBot:
    def __init__(self):
        self.scanner = MarketScanner()
        self.portfolio = PaperPortfolio(INITIAL_CASH)
        self.market_spend = {} # market_id -> float
        self._tick_count = 0

    def run(self):
        logger.info("=" * 60)
        logger.info("ULTRA-CHEAP DISLOCATION BOT")
        logger.info(f"Entry: {CHEAP_BUY_MIN}-{CHEAP_BUY_MAX} | Hold to Resolution")
        logger.info(f"Budget: ${INITIAL_CASH} | Delay: {ENTRY_DELAY_SEC}s")
        logger.info("=" * 60)

        while True:
            try:
                self._tick()
                time.sleep(POLL_INTERVAL_SEC)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Tick error: {e}", exc_info=True)
                time.sleep(10)

    def _tick(self):
        now = time.time()
        logger.info(f"Tick {self._tick_count}: Starting...")
        markets = self.scanner.get_15m_markets(TARGET_ASSETS)
        
        if self._tick_count % 10 == 0:
            logger.info(f"Tick {self._tick_count}: Scanning... Found {len(markets)} markets")

        # 1. Check for Fills
        for order in self.portfolio.pending_orders[:]:
            if order.filled: continue
            best_ask = self.scanner.get_best_ask(order.token_id)
            if best_ask and best_ask <= order.price:
                self.portfolio.fill_order(order)

        # TP REMOVED: Positions are now held to resolution
        
        # 2. Entry and Cancellation
        for m in markets:
            cid = m.get("conditionId", "")
            start_str = m.get("_event_start")
            end_str = m.get("_event_end")
            if not start_str or not end_str: continue
            
            try:
                start_ts = datetime.fromisoformat(start_str.replace("Z", "+00:00")).timestamp()
            except ValueError:
                try: import dateutil.parser
                except ImportError: start_ts = 0
                else: start_ts = dateutil.parser.isoparse(start_str).timestamp()

            try:
                end_ts = datetime.fromisoformat(end_str.replace("Z", "+00:00")).timestamp()
            except ValueError:
                try: import dateutil.parser
                except ImportError: end_ts = 0
                else: end_ts = dateutil.parser.isoparse(end_str).timestamp()
            
            if start_ts == 0 or end_ts == 0: continue

            # Cancel unfilled 30s before close
            if now > (end_ts - CANCEL_BEFORE_SEC):
                for order in self.portfolio.pending_orders[:]:
                    if order.market_id == cid and not order.filled:
                        self.portfolio.cancel_order(order)

            # Place orders after delay
            if start_ts + ENTRY_DELAY_SEC < now < end_ts:
                if self.market_spend.get(cid, 0) >= MAX_NOTIONAL_PER_MARKET:
                    continue

                tokens = json.loads(m.get("clobTokenIds", "[]"))
                outcomes = json.loads(m.get("outcomes", "[]"))
                
                already_placed = any(o.market_id == cid for o in self.portfolio.pending_orders)
                if not already_placed:
                    for i, token_id in enumerate(tokens):
                        outcome = outcomes[i]
                        p = CHEAP_BUY_MIN
                        while p <= CHEAP_BUY_MAX + 0.0001:
                            price = round(p, 2)
                            order = LimitOrder(
                                token_id=token_id,
                                side="BUY",
                                price=price,
                                shares=ORDER_SHARES,
                                outcome=outcome,
                                market_id=cid,
                                entry_time=now
                            )
                            if self.portfolio.place_limit_order(order):
                                self.market_spend[cid] = self.market_spend.get(cid, 0) + (price * ORDER_SHARES)
                                logger.info(f"LIMIT: {outcome} @ {price} placed for {cid[:8]}")
                            p += 0.01

        self.portfolio.save_state()
        self._tick_count += 1

if __name__ == "__main__":
    bot = UltraCheapBot()
    bot.run()
