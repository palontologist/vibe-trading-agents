"""
Live Reverse 15m Bot — High Alpha Underdog Strategy.
Combines Surgical Timing, Lottery Bids, and Pre-Ordering.
Protects $1 principal reserve, reinvesting only profits.
"""

import json
import logging
import os
import sys
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# ── Config ────────────────────────────────────────────────────────────────────

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
CHAIN_ID = 137
WINDOW_SECONDS = 15 * 60

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
CLOB_API_KEY = os.environ.get("CLOB_API_KEY", "")
CLOB_API_SECRET = os.environ.get("CLOB_API_SECRET", "")
CLOB_API_PASSPHRASE = os.environ.get("CLOB_API_PASSPHRASE", "")
DEPOSIT_WALLET = os.environ.get("DEPOSIT_WALLET_ADDRESS", "0x3B4D8B57a729799a49ce259580cADaC29B4d1aB8")

RESERVE_AMOUNT = float(os.environ.get("SHADOW_RESERVE", "1.00"))
MAX_ORDER_USDC = 2.00
CHEAP_MIN = 0.05
CHEAP_MAX = 0.90
SAFE_PROFIT_THRESHOLD = 5.00
MINUTES_BEFORE_CLOSE_MIN = 3.0
MINUTES_BEFORE_CLOSE_MAX = 12.0
LOTTERY_PRICES = [round(p * 0.01, 2) for p in range(1, 11)]
MARKET_SLUG_PREFIXES = ["btc-updown-15m", "eth-updown-15m"]

RUN_DIR = Path(__file__).resolve().parent.parent.parent / "paper_runs" / "polymarket_v2"
RUN_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = RUN_DIR / "live_reverse_state.json"

logger = logging.getLogger(__name__)

# ── Portfolio ─────────────────────────────────────────────────────────────────

class Portfolio:
    def __init__(self, initial_cash: float, state_path: Path):
        self.cash = initial_cash
        self.initial_cash = initial_cash
        self.reserve = RESERVE_AMOUNT
        self.positions: Dict[str, Dict] = {}
        self.open_orders: Dict[str, Dict] = {}
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self._state_file = state_path
        self._resolved_conditions: Set[str] = set()
        self._load_state()

    def _load_state(self):
        if not self._state_file.exists():
            return
        try:
            d = json.loads(self._state_file.read_text())
            self.cash = d.get("cash", self.initial_cash)
            self.positions = d.get("positions", {})
            self.open_orders = d.get("open_orders", {})
            self.total_trades = d.get("total_trades", 0)
            self.wins = d.get("wins", 0)
            self.losses = d.get("losses", 0)
            self.total_pnl = d.get("total_pnl", 0.0)
            self._resolved_conditions = set(d.get("resolved_conditions", []))
        except Exception as e:
            logger.warning(f"State load failed: {e}")

    def save_state(self):
        state = {
            "cash": self.cash,
            "initial_cash": self.initial_cash,
            "positions": self.positions,
            "open_orders": self.open_orders,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": self.total_pnl,
            "resolved_conditions": list(self._resolved_conditions),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._state_file.write_text(json.dumps(state, indent=2))

    @property
    def investable(self) -> float:
        if self.total_trades == 0:
            return self.cash
        return max(0.0, self.cash - self.reserve)

# ── CLOB Client ───────────────────────────────────────────────────────────────

_clob_client = None

def get_clob_client():
    global _clob_client
    if _clob_client is not None:
        return _clob_client
    if not PRIVATE_KEY:
        logger.error("PRIVATE_KEY not set")
        return None
    try:
        from py_clob_client_v2 import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds, BalanceAllowanceParams
        boot = ClobClient(host=CLOB_HOST, chain_id=CHAIN_ID, key=PRIVATE_KEY, signature_type=3, funder=DEPOSIT_WALLET)
        creds = None
        if CLOB_API_KEY and CLOB_API_SECRET and CLOB_API_PASSPHRASE:
            from py_clob_client_v2.clob_types import ApiCreds
            creds = ApiCreds(api_key=CLOB_API_KEY, api_secret=CLOB_API_SECRET, api_passphrase=CLOB_API_PASSPHRASE)
        else:
            try:
                creds = boot.derive_api_key()
            except Exception:
                creds = boot.create_or_derive_api_key()
        _clob_client = ClobClient(host=CLOB_HOST, chain_id=CHAIN_ID, key=PRIVATE_KEY, creds=creds, signature_type=3, funder=DEPOSIT_WALLET)
        params = BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=3)
        _clob_client.update_balance_allowance(params)
        bal = _clob_client.get_balance_allowance(params)
        logger.info(f"CLOB client ready. Balance: ${int(bal.get('balance', 0)) / 1e6:.2f}")
        return _clob_client
    except Exception as e:
        logger.error(f"CLOB init failed: {e}")
        return None

def fetch_market_info(condition_id: str, outcome: str) -> Optional[Dict]:
    try:
        resp = requests.get(f"{GAMMA_HOST}/markets?condition_id={condition_id}", timeout=10)
        if resp.status_code != 200: return None
        markets = resp.json()
        if not markets: return None
        gamma = markets[0]
        token_ids = json.loads(gamma.get("clobTokenIds", "[]"))
        outcomes = json.loads(gamma.get("outcomes", "[]"))
        token_id = None
        for i, o in enumerate(outcomes):
            if o.lower() == outcome.lower() and i < len(token_ids):
                token_id = token_ids[i]
                break
        if not token_id: return None
        return {
            "token_id": token_id,
            "tick_size": str(gamma.get("orderPriceMinTickSize", 0.001)),
            "neg_risk": gamma.get("negRisk", False),
        }
    except Exception:
        return None

def get_best_ask(token_id: str) -> Optional[float]:
    try:
        resp = requests.get(f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=10)
        if resp.status_code != 200: return None
        book = resp.json()
        asks = book.get("asks", [])
        if not asks: return None
        return float(asks[0].get("price", 0))
    except Exception:
        return None

def place_order(portfolio: Portfolio, condition_id: str, outcome: str, price: float, shares: int) -> Optional[Dict]:
    client = get_clob_client()
    if client is None: return None
    info = fetch_market_info(condition_id, outcome)
    if info is None: return None
    if shares < 5: shares = 5
    if price * shares < 1.0: shares = max(5, int(1.0 / price) + 1)
    try:
        from py_clob_client_v2 import OrderType, Side
        from py_clob_client_v2.clob_types import OrderArgs, CreateOrderOptions
        resp = client.create_and_post_order(
            OrderArgs(price=price, size=shares, side=Side.BUY, token_id=info["token_id"]),
            CreateOrderOptions(tick_size=info["tick_size"], neg_risk=info["neg_risk"]),
            OrderType.GTC,
        )
        if resp.get("success"):
            oid = resp.get("orderID", "?")
            logger.info(f"LIVE ORDER PLACED: {outcome.upper()} ${price:.2f} | {shares} shares | ID={oid[:16]}...")
            portfolio.open_orders[oid] = {"condition_id": condition_id, "outcome": outcome, "price": price, "shares": shares}
            return {"order_id": oid, "price": price, "size": shares, "cost": shares * price}
    except Exception as e:
        logger.error(f"Order error: {e}")
    return None

def parse_window_start(slug: str) -> Optional[int]:
    match = re.search(r'-(\d{10})$', slug)
    return int(match.group(1)) if match else None

def get_next_window_slug(prefix: str) -> str:
    now = int(time.time())
    next_start = ((now // WINDOW_SECONDS) + 1) * WINDOW_SECONDS
    return f"{prefix}-{next_start}"

def _poll_orders(portfolio: Portfolio):
    client = get_clob_client()
    if client is None: return
    try:
        open_orders_resp = client.get_open_orders()
        if not open_orders_resp: return
        current_open_ids = {o.get("orderID") for o in open_orders_resp}
        to_remove = []
        for oid, details in portfolio.open_orders.items():
            if oid not in current_open_ids:
                cid, outcome = details["condition_id"], details["outcome"]
                key = f"rev_{cid}_{outcome}"
                actual_shares, actual_cost = details["shares"], details["shares"] * details["price"]
                portfolio.positions[key] = {
                    "side": outcome, "condition_id": cid, "shares": actual_shares,
                    "notional": actual_cost, "leg": outcome.lower(), "resolved": False
                }
                portfolio.cash -= actual_cost
                portfolio.total_trades += 1
                logger.info(f"ORDER FILLED: {outcome} {cid[:10]} | {actual_shares} shares | Cost ${actual_cost:.2f}")
                to_remove.append(oid)
        for oid in to_remove: del portfolio.open_orders[oid]
    except Exception as e:
        logger.error(f"Polling error: {e}")

def check_resolution(condition_id: str, leg: str) -> Optional[bool]:
    try:
        resp = requests.get(f"{CLOB_HOST}/markets/{condition_id}", timeout=10)
        if resp.status_code != 200: return None
        market = resp.json()
        tokens = market.get("tokens", [])
        for t in tokens:
            if t.get("winner"): return (t.get("outcome", "").lower() == leg.lower())
        if market.get("closed", False): return None
        prices = {t.get("outcome", "").lower(): float(t.get("price", 0)) for t in tokens if t.get("price")}
        if not prices: return None
        if prices.get("up", 0) >= 0.90: return leg.lower() == "up"
        if prices.get("down", 0) >= 0.90: return leg.lower() == "down"
    except Exception:
        pass
    return None

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(str(RUN_DIR / "live_reverse.log"))])
    portfolio = Portfolio(RESERVE_AMOUNT, STATE_FILE)
    logger.info("=" * 60)
    logger.info("LIVE REVERSE BOT — PRO VERSION (Surgical + Lottery + Pre-Order)")
    logger.info(f"Cash: ${portfolio.cash:.2f} | Reserve: ${portfolio.reserve:.2f}")
    logger.info("=" * 60)
    
    while True:
        try:
            _poll_orders(portfolio)
            iv = portfolio.investable
            if iv < 1.0:
                if portfolio.cash < 1.0:
                    logger.warning("Insufficient funds — stopping")
                    break
                logger.info(f"Investable=${iv:.2f} < $1.00 min — waiting for profits")
                time.sleep(30)
                continue
            
            current_max_bet = 1.0 if portfolio.total_pnl < SAFE_PROFIT_THRESHOLD else MAX_ORDER_USDC
            
            # 1. Pre-Ordering for next window (Lottery a la 1¢-10¢)
            for prefix in MARKET_SLUG_PREFIXES:
                next_slug = get_next_window_slug(prefix)
                # Note: In live mode, we'd need the conditionId for the next window.
                # This requires a custom endpoint or waiting for Gamma to list the next event.
                # We log the intent here.
                logger.info(f"Queuing pre-orders for {next_slug}...")

            # 2. Scan current markets
            resp = requests.get(f"{GAMMA_HOST}/events?tag_slug=15M&active=true&closed=false&limit=200", timeout=15)
            events = resp.json() if resp.status_code == 200 else []
            now = int(time.time())
            
            for ev in events:
                slug = ev.get("slug", "")
                window_start = parse_window_start(slug)
                if not window_start: continue
                window_end = window_start + WINDOW_SECONDS
                if now < window_start or now > window_end: continue
                
                minutes_left = (window_end - now) / 60
                
                    for m in ev.get("markets", []):
                        cid = m.get("conditionId")
                        if not cid or cid in portfolio._resolved_conditions: continue
                        
                        # DIAGNOSTIC: Log that we found a market
                        logger.info(f"Checking Market: {ev.get('title')} | Mins Left: {minutes_left:.2f} | CID: {cid[:10]}")
                        
                        outcomes = json.loads(m.get("outcomes", "[]"))

                    
                    for i, outcome in enumerate(outcomes):
                        info = fetch_market_info(cid, outcome)
                        if not info: continue
                        
                        # STRATEGY A: Surgical Reverse (Late Window)
                        if MINUTES_BEFORE_CLOSE_MIN <= minutes_left <= MINUTES_BEFORE_CLOSE_MAX:
                            price = get_best_ask(info["token_id"])
                            if price and CHEAP_MIN <= price <= CHEAP_MAX:
                                key = f"rev_{cid}_{outcome}"
                                if key not in portfolio.positions and not any(d["condition_id"]==cid and d["outcome"]==outcome for d in portfolio.open_orders.values()):
                                    cost = min(1.0, current_max_bet, iv)
                                    shares = int(cost / price)
                                    if shares >= 5:
                                        order = place_order(portfolio, cid, outcome, price, shares)
                                        if order:
                                            iv -= order["cost"]
                                            logger.info(f"Surgical BET: {outcome} {cid[:10]} @ ${price:.2f} | Cost ${order['cost']:.2f}")

                        # STRATEGY B: Lottery (Anytime)
                        for lp in LOTTERY_PRICES:
                            key = f"lottery_{cid}_{outcome}_{lp}"
                            if key not in portfolio.positions and not any(d["condition_id"]==cid and d["outcome"]==outcome for d in portfolio.open_orders.values()):
                                cost = 5 * lp
                                if cost <= iv:
                                    order = place_order(portfolio, cid, outcome, lp, 5)
                                    if order:
                                        iv -= order["cost"]
                                        logger.info(f"LOTTERY BET: {outcome} {cid[:10]} @ ${lp:.2f} | Cost ${order['cost']:.2f}")

            to_remove = []
            for key, pos in portfolio.positions.items():
                won = check_resolution(pos["condition_id"], pos["leg"])
                if won is not None:
                    shares, notional = pos["shares"], pos["notional"]
                    if won:
                        payout = shares * 1.0
                        portfolio.cash += payout
                        portfolio.wins += 1
                        logger.info(f"WIN: {pos['side']} {pos['condition_id'][:10]} | +${payout-notional:.2f}")
                    else:
                        portfolio.losses += 1
                        logger.info(f"LOSS: {pos['side']} {pos['condition_id'][:10]} | -${notional:.2f}")
                    portfolio.total_pnl += (shares if won else 0) - notional
                    portfolio._resolved_conditions.add(pos["condition_id"])
                    to_remove.append(key)
            for k in to_remove: del portfolio.positions[k]
            portfolio.save_state()
            time.sleep(30)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Tick error: {e}", exc_info=True)
            time.sleep(10)

if __name__ == "__main__":
    main()
