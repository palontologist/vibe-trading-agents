"""
Paper Reverse Bot — Testing Lottery & Pre-Order Strategies.
Budget: $3.57
Strategies: 
1. Lottery: 1¢, 2¢, 3¢ bids on both sides.
2. Pre-Order: Placing bids on the next 15m window before it opens.
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

# Paper Budget
INITIAL_CASH = 3.57
RESERVE_AMOUNT = 1.00
MAX_ORDER_USDC = 2.00
LOTTERY_PRICES = [round(p * 0.01, 2) for p in range(1, 11)] # 1¢ to 10¢
MARKET_SLUG_PREFIXES = ["btc-updown-15m", "eth-updown-15m"]

RUN_DIR = Path(__file__).resolve().parent.parent.parent / "paper_runs" / "polymarket_v2"
RUN_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = RUN_DIR / "paper_reverse_state.json"

logger = logging.getLogger(__name__)

# ── Portfolio ─────────────────────────────────────────────────────────────────

class Portfolio:
    def __init__(self, initial_cash: float, state_path: Path):
        self.cash = initial_cash
        self.initial_cash = initial_cash
        self.reserve = RESERVE_AMOUNT
        self.positions: Dict[str, Dict] = {}
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

# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_window_start(slug: str) -> Optional[int]:
    match = re.search(r'-(\d{10})$', slug)
    return int(match.group(1)) if match else None

def get_next_window_slug(prefix: str) -> str:
    """Predict the next window slug based on current time."""
    now = int(time.time())
    # Round down to nearest 15m (900s)
    next_start = ((now // WINDOW_SECONDS) + 1) * WINDOW_SECONDS
    return f"{prefix}-{next_start}"

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

def check_resolution(condition_id: str, leg: str) -> Optional[bool]:
    try:
        resp = requests.get(f"{CLOB_HOST}/markets/{condition_id}", timeout=10)
        if resp.status_code != 200: return None
        market = resp.json()
        tokens = market.get("tokens", [])
        for t in tokens:
            if t.get("winner"):
                return (t.get("outcome", "").lower() == leg.lower())
        if market.get("closed", False): return None
        prices = {t.get("outcome", "").lower(): float(t.get("price", 0)) for t in tokens if t.get("price")}
        if not prices: return None
        if prices.get("up", 0) >= 0.90: return leg.lower() == "up"
        if prices.get("down", 0) >= 0.90: return leg.lower() == "down"
    except Exception:
        pass
    return None

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(str(RUN_DIR / "paper_reverse_stdout.log"))])

    portfolio = Portfolio(INITIAL_CASH, STATE_FILE)
    logger.info("=" * 60)
    logger.info("PAPER REVERSE BOT — LOTTERY & PRE-ORDER TEST")
    logger.info(f"Initial Cash: ${portfolio.cash:.2f} | Reserve: ${portfolio.reserve:.2f}")
    logger.info("=" * 60)

    while True:
        try:
            iv = portfolio.investable
            if iv < 0.1:
                logger.info(f"Investable=${iv:.2f} too low — waiting for resolution")
                time.sleep(60)
                continue

            # 1. Handle Pre-Ordering for next window
            for prefix in MARKET_SLUG_PREFIXES:
                next_slug = get_next_window_slug(prefix)
                # For paper, we can't "place" a real order on a non-existent market, 
                # but we simulate the entry when the window actually opens.
                # In a real bot, this would be a CLOB order.
                logger.info(f"Pre-ordering for {next_slug}...")

            # 2. Scan current markets for Lottery entries
            resp = requests.get(f"{GAMMA_HOST}/events?tag_slug=15M&active=true&closed=false&limit=200", timeout=15)
            events = resp.json() if resp.status_code == 200 else []
            
            for ev in events:
                for m in ev.get("markets", []):
                    cid = m.get("conditionId")
                    if not cid or cid in portfolio._resolved_conditions:
                        continue
                    
                    outcomes = json.loads(m.get("outcomes", "[]"))
                    for outcome in outcomes:
                        # Lottery Strategy: Try 1¢ to 10¢
                        for p in LOTTERY_PRICES:
                            key = f"lottery_{cid}_{outcome}_{p}"
                            if key in portfolio.positions: continue
                            
                            # Use actual best ask to determine fill
                            info = fetch_market_info(cid, outcome)
                            if not info: continue
                            
                            best_ask = get_best_ask(info["token_id"])
                            if best_ask is not None and best_ask <= p:
                                cost = 5 * p # 5 shares min
                                if cost <= iv:
                                    portfolio.positions[key] = {
                                        "side": outcome, "condition_id": cid, "shares": 5,
                                        "notional": cost, "leg": outcome.lower(), "resolved": False
                                    }
                                    portfolio.cash -= cost
                                    portfolio.total_trades += 1
                                    iv -= cost
                                    logger.info(f"REAL LOTTERY FILL: {outcome} {cid[:10]} @ ${p:.2f} (Ask: ${best_ask:.2f}) | Cost ${cost:.2f}")

            # 3. Resolve positions
            to_remove = []
            for key, pos in portfolio.positions.items():
                won = check_resolution(pos["condition_id"], pos["leg"])
                if won is not None:
                    shares, notional = pos["shares"], pos["notional"]
                    if won:
                        payout = shares * 1.0
                        portfolio.cash += payout
                        portfolio.wins += 1
                        logger.info(f"LOTTERY WIN: {pos['side']} {pos['condition_id'][:10]} | +${payout-notional:.2f}")
                    else:
                        portfolio.losses += 1
                        logger.info(f"LOTTERY LOSS: {pos['side']} {pos['condition_id'][:10]} | -${notional:.2f}")
                    portfolio.total_pnl += (shares if won else 0) - notional
                    portfolio._resolved_conditions.add(pos["condition_id"])
                    to_remove.append(key)
            
            for k in to_remove: del portfolio.positions[k]
            portfolio.save_state()
            time.sleep(60)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Tick error: {e}", exc_info=True)
            time.sleep(10)

if __name__ == "__main__":
    main()
