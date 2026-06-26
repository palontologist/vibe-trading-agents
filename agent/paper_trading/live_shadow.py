"""
Live Shadow Trader — copies Polymarket trades from target user via real CLOB orders.
Protects $1 principal reserve — only reinvests profits above the reserve.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent.paper_trading.copy_trader import Activity, get_activity
from agent.paper_trading.reverse_15m import CLOB_HOST, CHAIN_ID, GAMMA_HOST

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
CLOB_API_KEY = os.environ.get("CLOB_API_KEY", "")
CLOB_API_SECRET = os.environ.get("CLOB_API_SECRET", "")
CLOB_API_PASSPHRASE = os.environ.get("CLOB_API_PASSPHRASE", "")
DEPOSIT_WALLET = os.environ.get("DEPOSIT_WALLET_ADDRESS", "0x3B4D8B57a729799a49ce259580cADaC29B4d1aB8")

TARGET_USER = os.environ.get("SHADOW_TARGET", "0xe2511c9e41c5e762887e538b1d6e7221807aa237")
POLL_INTERVAL = int(os.environ.get("SHADOW_POLL_MS", "30000")) // 1000
MAX_COPY_USDC = float(os.environ.get("SHADOW_MAX_PER_TRADE", "1.00"))
RESERVE_AMOUNT = float(os.environ.get("SHADOW_RESERVE", "1.00"))

RUN_DIR = Path(__file__).resolve().parent.parent.parent / "paper_runs" / "polymarket_v2"
RUN_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = RUN_DIR / "shadow_live_state.json"


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
        self.total_invested = 0.0
        self.tracked_keys: Set[str] = set()
        self._resolved_conditions: Set[str] = set()
        self._state_file = state_path
        self._load_state()

    def _load_state(self):
        if not self._state_file.exists():
            return
        try:
            d = json.loads(self._state_file.read_text())
            self.cash = d.get("cash", self.initial_cash)
            self.initial_cash = d.get("initial_cash", self.initial_cash)
            self.reserve = d.get("reserve", RESERVE_AMOUNT)
            self.positions = d.get("positions", {})
            self.total_trades = d.get("total_trades", 0)
            self.wins = d.get("wins", 0)
            self.losses = d.get("losses", 0)
            self.total_pnl = d.get("total_pnl", 0.0)
            self.total_invested = d.get("total_invested", 0.0)
            self.tracked_keys = set(d.get("tracked_keys", []))
            self._resolved_conditions = set(d.get("resolved_conditions", []))
        except Exception as e:
            logger.warning(f"State load failed: {e}")

    def save_state(self):
        state = {
            "cash": self.cash,
            "initial_cash": self.initial_cash,
            "reserve": self.reserve,
            "positions": self.positions,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": self.total_pnl,
            "total_invested": self.total_invested,
            "tracked_keys": list(self.tracked_keys),
            "resolved_conditions": list(self._resolved_conditions),
            "win_rate": self.wins / max(1, self.total_trades),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._state_file.write_text(json.dumps(state, indent=2))

    @property
    def investable(self) -> float:
        if self.total_trades == 0:
            return self.cash
        return max(0.0, self.cash - self.reserve)

    def has_position(self, condition_id: str, outcome: str) -> bool:
        key = f"shadow_{condition_id}_{outcome}"
        return any(key in k for k in self.positions)

    def is_resolved(self, condition_id: str) -> bool:
        return condition_id in self._resolved_conditions

    def mark_resolved(self, condition_id: str):
        self._resolved_conditions.add(condition_id)


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

        boot = ClobClient(
            host=CLOB_HOST,
            chain_id=CHAIN_ID,
            key=PRIVATE_KEY,
            signature_type=3,
            funder=DEPOSIT_WALLET,
        )

        creds = None
        if CLOB_API_KEY and CLOB_API_SECRET and CLOB_API_PASSPHRASE:
            creds = ApiCreds(
                api_key=CLOB_API_KEY,
                api_secret=CLOB_API_SECRET,
                api_passphrase=CLOB_API_PASSPHRASE,
            )
        else:
            try:
                creds = boot.derive_api_key()
            except Exception:
                creds = boot.create_or_derive_api_key()

        _clob_client = ClobClient(
            host=CLOB_HOST,
            chain_id=CHAIN_ID,
            key=PRIVATE_KEY,
            creds=creds,
            signature_type=3,
            funder=DEPOSIT_WALLET,
        )

        params = BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=3)
        _clob_client.update_balance_allowance(params)
        bal = _clob_client.get_balance_allowance(params)
        balance = int(bal.get("balance", 0))
        logger.info(f"CLOB client ready — wallet={DEPOSIT_WALLET[:12]}... balance=${balance / 1e6:.2f}")

        return _clob_client
    except Exception as e:
        logger.error(f"CLOB client init failed: {e}")
        return None




def fetch_market_info(condition_id: str, outcome: str) -> Optional[Dict]:
    """Get token_id from Gamma clobTokenIds, tick_size + neg_risk from Gamma market."""
    try:
        resp = requests.get(
            f"{GAMMA_HOST}/events?tag_slug=15M&active=true&closed=false&limit=200",
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        events = resp.json()
        if not events:
            return None

        market = None
        for ev in events:
            for m in ev.get("markets", []):
                if m.get("conditionId") == condition_id:
                    market = m
                    break
            if market:
                break

        if not market:
            return None

        token_ids = market.get("clobTokenIds", [])
        outcomes = market.get("outcomes", [])

        token_id = None
        for i, o in enumerate(outcomes):
            if o.lower() == outcome.lower() and i < len(token_ids):
                token_id = token_ids[i]
                break
        if not token_id:
            return None

        return {
            "token_id": token_id,
            "tick_size": str(market.get("orderPriceMinTickSize", 0.001)),
            "neg_risk": market.get("negRisk", False),
        }
    except Exception as e:
        logger.warning(f"fetch_market_info error: {e}")
        return None


def place_live_order(condition_id: str, outcome: str, price: float, shares: int) -> Optional[Dict]:
    """Place GTC limit BUY order via CLOB. Returns order dict or None."""
    client = get_clob_client()
    if client is None:
        return None

    info = fetch_market_info(condition_id, outcome)
    if info is None:
        logger.warning(f"Cannot fetch market info for {condition_id}")
        return None

    if shares < 5:
        shares = 5
    if price * shares < 1.0:
        shares = max(5, int(1.0 / price) + 1)

    try:
        from py_clob_client_v2 import OrderType, Side
        from py_clob_client_v2.clob_types import OrderArgs, CreateOrderOptions

        resp = client.create_and_post_order(
            OrderArgs(
                price=price,
                size=shares,
                side=Side.BUY,
                token_id=info["token_id"],
            ),
            CreateOrderOptions(
                tick_size=info["tick_size"],
                neg_risk=info["neg_risk"],
            ),
            OrderType.GTC,
        )

        if resp.get("success"):
            oid = resp.get("orderID", "?")
            actual_size = int(resp.get("size", shares))
            logger.info(f"LIVE ORDER: {outcome.upper()} ${price:.2f} | {actual_size} shares | ID={oid[:16]}...")
            return {"order_id": oid, "price": price, "size": actual_size, "cost": actual_size * price}
        else:
            logger.warning(f"Order rejected: {resp}")
            return None

    except Exception as e:
        logger.error(f"Order error: {e}")
        return None


# ── Resolution ────────────────────────────────────────────────────────────────

def check_resolution(condition_id: str, leg: str) -> Optional[bool]:
    """Query CLOB /markets/{condition_id} for winner. Returns True if leg won."""
    try:
        resp = requests.get(f"{CLOB_HOST}/markets/{condition_id}", timeout=10)
        if resp.status_code != 200:
            return None
        market = resp.json()
    except Exception:
        return None

    tokens = market.get("tokens", [])
    for t in tokens:
        if t.get("winner"):
            return (t.get("outcome", "").lower() == leg.lower())

    if market.get("closed", False):
        return None

    prices = {}
    for t in tokens:
        try:
            prices[t.get("outcome", "").lower()] = float(t.get("price", 0))
        except (ValueError, TypeError):
            continue

    if not prices:
        return None

    up = prices.get("up", 0)
    down = prices.get("down", 0)
    if up >= 0.90:
        return leg.lower() == "up"
    if down >= 0.90:
        return leg.lower() == "down"
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(RUN_DIR / "live_shadow.log")),
        ],
    )

    portfolio = Portfolio(RESERVE_AMOUNT, STATE_FILE)
    initial_cash = portfolio.cash

    logger.info("=" * 60)
    logger.info("LIVE SHADOW TRADER")
    logger.info(f"Target: {TARGET_USER[:20]}...")
    logger.info(f"Cash: ${portfolio.cash:.2f}")
    logger.info(f"Reserve: ${RESERVE_AMOUNT:.2f}")
    logger.info(f"Investable: ${portfolio.investable:.2f}")
    logger.info("=" * 60)

    last_ts = int(time.time()) - 7200

    while True:
        try:
            iv = portfolio.investable
            if iv < 0.01:
                if portfolio.cash < 0.01:
                    logger.warning("Out of funds — stopping")
                    break
                logger.info(f"Investable=${iv:.2f} (below reserve) — waiting for profits")
                time.sleep(POLL_INTERVAL)
                portfolio.save_state()
                continue

            activities = get_activity(TARGET_USER, limit=50)
            new_trades = []

            for a in activities:
                if a.timestamp <= last_ts:
                    continue
                if a.type != "TRADE" or a.side != "BUY":
                    continue
                key = f"{a.transactionHash or ''}:{a.asset}:{a.side}"
                if key in portfolio.tracked_keys:
                    continue
                new_trades.append(a)
                portfolio.tracked_keys.add(key)

            if new_trades:
                new_trades.reverse()
                total_spend = sum(a.usdcSize or 0 for a in new_trades)
                if total_spend > 0:
                    cycle_scale = min(iv / total_spend, 1.0)
                    logger.info(f"Cycle scale: {cycle_scale:.4f}x (investable=${iv:.2f} / total=${total_spend:.2f})")
                else:
                    cycle_scale = 0.0

                for a in new_trades:
                    _process_trade(portfolio, a, cycle_scale, iv)

                portfolio.save_state()

            if activities:
                last_ts = max(a.timestamp for a in activities)

            _resolve_positions(portfolio)
            portfolio.save_state()

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Tick error: {e}", exc_info=True)
            time.sleep(10)

    logger.info("Stopped.")
    portfolio.save_state()


def _process_trade(portfolio: Portfolio, a: Activity, scale: float, budget: float):
    price = a.price or 0.1
    cost = a.usdcSize or 0

    if cost <= 0 or price <= 0:
        return

    if portfolio.is_resolved(a.conditionId):
        return

    if portfolio.has_position(a.conditionId, a.outcome):
        return

    min_order = max(price * 5, 1.0)
    if min_order > budget:
        logger.info(f"SKIP {a.outcome} @ ${price:.2f} | need ${min_order:.2f} min, have ${budget:.2f}")
        return

    our_cost = min(cost * scale, MAX_COPY_USDC, budget)
    shares = max(5, int(our_cost / price))
    if price * shares > budget:
        shares = int(budget / price)
        if shares < 5:
            return

    # Place live order
    order = place_live_order(a.conditionId, a.outcome, price, shares)
    if order is None:
        return

    actual_shares = order.get("size", shares)
    actual_cost = order.get("cost", actual_shares * price)

    if actual_cost > portfolio.cash:
        logger.warning(f"Order cost ${actual_cost:.2f} exceeds cash ${portfolio.cash:.2f} — skipping tracking")
        return

    portfolio.positions[f"shadow_{a.conditionId}_{a.outcome}"] = {
        "side": a.outcome or "?",
        "condition_id": a.conditionId,
        "price_label": f"shadow@{price:.2f}",
        "entry_price": price,
        "current_price": price,
        "shares": actual_shares,
        "notional": actual_cost,
        "entry_time": time.time(),
        "question": (a.title or "")[:60],
        "leg": (a.outcome or "").lower(),
        "asset": a.asset,
        "resolved": False,
        "order_id": order.get("order_id", ""),
    }
    portfolio.cash -= actual_cost
    portfolio.total_trades += 1
    portfolio.total_invested += actual_cost

    logger.info(
        f"[COPY] {a.outcome} @ ${price:.2f} | {actual_shares} shares | "
        f"Cost=${actual_cost:.2f} | Cash=${portfolio.cash:.2f} | {a.title}"
    )


def _resolve_positions(portfolio: Portfolio):
    to_remove = []

    for key, pos in portfolio.positions.items():
        if pos.get("resolved"):
            continue

        cid = pos.get("condition_id", "")
        if not cid or portfolio.is_resolved(cid):
            continue

        won = check_resolution(cid, pos.get("leg", ""))
        if won is None:
            continue

        shares = pos.get("shares", 0)
        notional = pos.get("notional", 0)

        if won:
            payout = shares * 1.0
            pnl = payout - notional
            portfolio.cash += payout
            portfolio.wins += 1
            logger.info(f"[RESOLVE] WIN {pos.get('question','?')[:40]} | {pos.get('leg')} | {shares} shares → +${pnl:.2f}")
        else:
            pnl = -notional
            portfolio.losses += 1
            logger.info(f"[RESOLVE] LOSS {pos.get('question','?')[:40]} | {pos.get('leg')} | {shares} shares → -${notional:.2f}")

        pos["resolved"] = True
        pos["pnl"] = pnl
        portfolio.total_pnl += pnl
        to_remove.append(key)

    if to_remove:
        for key in to_remove:
            cid = portfolio.positions[key].get("condition_id", "")
            if cid:
                portfolio.mark_resolved(cid)
            del portfolio.positions[key]


if __name__ == "__main__":
    main()
