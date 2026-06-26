"""
Polymarket Ultra-Cheap Dislocation Bot - LIVE VERSION
Strategy: Lottery tickets (5-10 cents) on both sides of 15m BTC/ETH windows.
Hold to resolution for max payout.
"""

import json
import logging
import os
import time
import requests
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Any

# Install py-clob-client-v2 if not present
try:
    from py_clob_client_v2 import ClobClient, ApiCreds, OrderType, Side
    from py_clob_client_v2.clob_types import BalanceAllowanceParams, OrderArgs, CreateOrderOptions
except ImportError:
    print("Error: py-clob-client-v2 not installed. Please run 'pip install py-clob-client-v2'")
    exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("UltraCheapLive")

# ── Config ────────────────────────────────────────────────────────────────────
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"

# Strategy Parameters
CHEAP_BUY_MIN = 0.05
CHEAP_BUY_MAX = 0.10
ORDER_SHARES = 5             # Small size for $1 budget
MAX_NOTIONAL_PER_MARKET = 1.0 

TARGET_ASSETS = ["btc", "eth"]
POLL_INTERVAL_SEC = 5
INITIAL_BUDGET = 1.0         # Total live budget for this session

ENTRY_DELAY_SEC = 45         
CANCEL_BEFORE_SEC = 30       

RUN_DIR = Path("./paper_runs/ultra_cheap_live")
RUN_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = RUN_DIR / "live_state.json"

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

# ── Live Trading Execution ────────────────────────────────────────────────────

class LiveTrader:
    def __init__(self):
        self.host = CLOB_HOST
        self.chain_id = 137
        self.deposit_wallet = "0x3B4D8B57a729799a49ce259580cADaC29B4d1aB8"
        self.private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        self._client = None
        self._init_client()

    def _init_client(self):
        try:
            boot = ClobClient(
                host=self.host,
                chain_id=self.chain_id,
                key=self.private_key,
                signature_type=3,
                funder=self.deposit_wallet,
            )
            try:
                creds = boot.derive_api_key()
            except Exception:
                creds = boot.create_or_derive_api_key()

            self._client = ClobClient(
                host=self.host,
                chain_id=self.chain_id,
                key=self.private_key,
                creds=creds,
                signature_type=3,
                funder=self.deposit_wallet,
            )
            logger.info(f"LiveTrader initialized for {self.deposit_wallet[:12]}...")
        except Exception as e:
            logger.error(f"Failed to init ClobClient: {e}")

    def get_balance(self) -> float:
        if not self._client: return 0.0
        try:
            params = BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=3)
            self._client.update_balance_allowance(params)
            bal = self._client.get_balance_allowance(params)
            return int(bal.get("balance", 0)) / 10**6
        except Exception as e:
            logger.warning(f"Balance fetch failed: {e}")
            return 0.0

    def place_limit_buy(self, token_id: str, price: float, size: int) -> Optional[str]:
        if not self._client: return None
        try:
            # Ensure $1 minimum order value (CLOB requirement)
            if price * size < 1.0:
                size = max(5, int(1.0 / price) + 1)
            
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
                oid = resp.get("orderID")
                logger.info(f"LIVE-LIMIT: Placed {size} shares @ {price} | ID: {oid[:12]}...")
                return oid
            logger.warning(f"Order failed: {resp}")
        except Exception as e:
            logger.error(f"Order error: {e}")
        return None

    def cancel_order(self, order_id: str):
        if not self._client: return
        try:
            self._client.cancel(order_id)
            logger.info(f"LIVE-CANCEL: {order_id[:12]}...")
        except Exception as e:
            logger.warning(f"Cancel failed: {e}")

    def get_open_orders(self) -> List[Dict]:
        if not self._client: return []
        try:
            return self._client.get_open_orders()
        except Exception as e:
            logger.warning(f"Get open orders failed: {e}")
            return []

# ── Portfolio & State ─────────────────────────────────────────────────────────

class LivePortfolio:
    def __init__(self, initial_budget: float):
        self.budget = initial_budget
        self.spent = 0.0
        self.pending_orders: List[LimitOrder] = []
        self._load_state()

    def _load_state(self):
        if STATE_FILE.exists():
            try:
                d = json.loads(STATE_FILE.read_text())
                self.spent = d.get("spent", 0.0)
                # Pending orders are tracked by the CLOB, but we keep a local map
                # For simplicity in a $1 run, we'll just sync with get_open_orders
            except Exception:
                pass

    def save_state(self):
        STATE_FILE.write_text(json.dumps({
            "spent": self.spent,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }, indent=2))

    def can_afford(self, cost: float) -> bool:
        return (self.spent + cost) <= self.budget

    def record_spend(self, cost: float):
        self.spent += cost
        self.save_state()

# ── Bot Orchestrator ───────────────────────────────────────────────────────────

class UltraCheapLiveBot:
    def __init__(self):
        self.scanner = MarketScanner()
        self.trader = LiveTrader()
        self.portfolio = LivePortfolio(INITIAL_BUDGET)
        self.market_spend = {} # market_id -> float
        self._tick_count = 0

    def run(self):
        logger.info("=" * 60)
        logger.info("ULTRA-CHEAP LIVE BOT")
        logger.info(f"Range: {CHEAP_BUY_MIN}-{CHEAP_BUY_MAX} | Budget: ${INITIAL_BUDGET}")
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
        markets = self.scanner.get_15m_markets(TARGET_ASSETS)
        
        if self._tick_count % 10 == 0:
            logger.info(f"Tick {self._tick_count}: Scanning... Found {len(markets)} markets | Budget Spent: ${self.portfolio.spent:.2f}/{INITIAL_BUDGET}")

        # 1. Sync Pending Orders from CLOB
        open_orders = self.trader.get_open_orders()
        # We don't strictly need to track fills in real-time for this strategy 
        # as the PnL is settled at resolution.

        # 2. Entry and Cancellation
        for m in markets:
            cid = m.get("conditionId", "")
            start_str = m.get("_event_start")
            end_str = m.get("_event_end")
            if not start_str or not end_str: continue
            
            try:
                start_ts = datetime.fromisoformat(start_str.replace("Z", "+00:00")).timestamp()
                end_ts = datetime.fromisoformat(end_str.replace("Z", "+00:00")).timestamp()
            except Exception: continue

            # Cancel unfilled 30s before close
            if now > (end_ts - CANCEL_BEFORE_SEC):
                # In a real live scenario, we'd filter open orders by token_id
                # For a $1 test, we can just let them expire or use trader.cancel_all()
                # if the window is over.
                pass

            # Place orders after delay
            if start_ts + ENTRY_DELAY_SEC < now < end_ts:
                if self.market_spend.get(cid, 0) >= MAX_NOTIONAL_PER_MARKET:
                    continue

                tokens = json.loads(m.get("clobTokenIds", "[]"))
                outcomes = json.loads(m.get("outcomes", "[]"))
                
                # Check if we already have orders for this window to avoid spamming
                # In live mode, we check the order book or our local state
                # For this simple bot, we'll rely on market_spend
                
                # To avoid duplicate orders every tick, we check if we've placed a 'base' order
                if self.market_spend.get(cid, 0) > 0:
                    continue

                for i, token_id in enumerate(tokens):
                    outcome = outcomes[i]
                    p = CHEAP_BUY_MIN
                    while p <= CHEAP_BUY_MAX + 0.0001:
                        price = round(p, 2)
                        cost = price * ORDER_SHARES
                        
                        if self.portfolio.can_afford(cost):
                            oid = self.trader.place_limit_buy(token_id, price, ORDER_SHARES)
                            if oid:
                                self.portfolio.record_spend(cost)
                                self.market_spend[cid] = self.market_spend.get(cid, 0) + cost
                                logger.info(f"LIVE-LIMIT: {outcome} @ {price} placed for {cid[:8]}")
                        p += 0.01
        
        self._tick_count += 1

if __name__ == "__main__":
    bot = UltraCheapLiveBot()
    bot.run()
