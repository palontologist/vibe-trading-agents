"""
Combined Bot — alternates between reverse 15m strategy and shadow trading odahoa.
Shares a single paper portfolio with $1.60 simulated balance.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent.paper_trading.reverse_15m import (
    CLOB_HOST, RUN_DIR, cheap_limit_prices, expensive_limit_prices,
    compute_size, find_opportunities, pick_reverse_token, MarketScanner,
    CHEAP_ORDER_USDC, MAX_SHARES_PER_ORDER, CHEAP_BUY_MIN, CHEAP_BUY_MAX,
    EXPENSIVE_BUY_MIN, EXPENSIVE_BUY_MAX, ENABLE_EXPENSIVE_HEDGE,
    MINUTES_BEFORE_CLOSE_MIN, MINUTES_BEFORE_CLOSE_MAX,
)
from agent.paper_trading.copy_trader import get_activity

logger = logging.getLogger(__name__)

COMBINED_STATE = RUN_DIR / "combined_state.json"
TARGET_USER = "0xe2511c9e41c5e762887e538b1d6e7221807aa237"
SHADOW_POLL_MS = 30000
REVERSE_POLL_MS = 5000

class CombinedPortfolio:
    def __init__(self, state_path: Path):
        self.cash = 1.60
        self.initial_cash = 1.60
        self.positions = {}
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.total_invested = 0.0
        self.tracked_keys = set()
        self._state_file = state_path
        self._load()

    def _load(self):
        if self._state_file.exists():
            try:
                d = json.loads(self._state_file.read_text())
                self.cash = d.get("cash", self.cash)
                self.initial_cash = d.get("initial_cash", self.initial_cash)
                self.positions = d.get("positions", {})
                self.total_trades = d.get("total_trades", 0)
                self.wins = d.get("wins", 0)
                self.losses = d.get("losses", 0)
                self.total_pnl = d.get("total_pnl", 0.0)
                self.total_invested = d.get("total_invested", 0.0)
                self.tracked_keys = set(d.get("tracked_keys", []))
            except Exception:
                pass

    def save(self):
        state = {
            "cash": self.cash,
            "initial_cash": self.initial_cash,
            "positions": self.positions,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": self.total_pnl,
            "total_invested": self.total_invested,
            "tracked_keys": list(self.tracked_keys),
            "win_rate": self.wins / max(1, self.total_trades),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._state_file.write_text(json.dumps(state, indent=2))


def check_resolution(condition_id: str, side: str) -> Optional[bool]:
    import requests
    try:
        resp = requests.get(f"{CLOB_HOST}/markets/{condition_id}", timeout=10)
        if resp.status_code != 200:
            return None
        market = resp.json()
    except Exception:
        return None
    tokens = market.get("tokens", [])
    for token in tokens:
        if token.get("winner"):
            return (token.get("outcome", "").lower() == side)
    if market.get("closed", False):
        return None
    prices = {}
    for token in tokens:
        try:
            prices[token.get("outcome", "").lower()] = float(token.get("price", 0))
        except (ValueError, TypeError):
            continue
    if not prices:
        return None
    up_price = prices.get("up", 0)
    down_price = prices.get("down", 0)
    if up_price >= 0.90:
        return side == "up"
    elif down_price >= 0.90:
        return side == "down"
    return None


def resolve_positions(portfolio: CombinedPortfolio):
    now = time.time()
    to_remove = []
    for key, pos in portfolio.positions.items():
        if pos.get("resolved"):
            continue
        cid = pos.get("condition_id", "")
        if not cid:
            continue
        won = check_resolution(cid, pos.get("leg", ""))
        if won is None:
            continue
        shares = pos.get("shares", 0)
        if won:
            payout = shares * 1.0
            pnl = payout - pos["notional"]
            portfolio.cash += payout
            portfolio.wins += 1
            logger.info(f"[RESOLVE] WIN {pos.get('question','?')[:40]} | {pos.get('leg')} | {shares} shares → +${pnl:.2f}")
        else:
            pnl = -pos["notional"]
            portfolio.losses += 1
            logger.info(f"[RESOLVE] LOSS {pos.get('question','?')[:40]} | {pos.get('leg')} | {shares} shares → -${pos['notional']:.2f}")
        pos["resolved"] = True
        pos["pnl"] = pnl
        portfolio.total_pnl += pnl
        to_remove.append(key)
    if to_remove:
        for key in to_remove:
            cid = portfolio.positions[key].get("condition_id", "")
            if cid:
                portfolio.tracked_keys.add(f"resolved_{cid}")
            del portfolio.positions[key]
        portfolio.save()


def tick_reverse(portfolio: CombinedPortfolio, scanner: MarketScanner):
    """Reverse 15m strategy tick."""
    events = scanner.scan()
    if not events:
        return

    for event in events:
        if portfolio.cash < 0.50:
            break

        cid = event.market.condition_id
        if f"resolved_{cid}" in portfolio.tracked_keys:
            continue
        if any(p.get("condition_id") == cid and not p.get("resolved") for p in portfolio.positions.values()):
            continue

        books = scanner.get_token_books(event)
        opps = find_opportunities(scanner, event, books)
        if not opps:
            continue

        for opp in opps:
            trade_key = scanner.make_trade_key(event.slug, opp.token.outcome, f"{opp.kind}-{opp.price:.2f}")

            cost = opp.price * opp.size
            if cost < 1.0 or opp.size < 5:
                opp.size = max(5, int(1.0 / opp.price) + 1)
                cost = opp.price * opp.size
            if cost > portfolio.cash:
                continue

            order_id = None
            tag = "REVERSE"
            logger.info(f"[{tag}] {opp.kind.upper()} {opp.token.outcome} {opp.price:.2f} | {opp.size} shares @ ${opp.price:.2f} | Cost=${cost:.2f} | Cash=${portfolio.cash:.2f}")

            key = f"rev_{event.market.condition_id}_{opp.token.outcome}_{opp.price:.2f}"
            portfolio.positions[key] = {
                "side": opp.token.outcome, "condition_id": event.market.condition_id,
                "price_label": f"{opp.kind}@{opp.price:.2f}", "entry_price": opp.price,
                "current_price": opp.price, "shares": opp.size, "notional": cost,
                "entry_time": time.time(), "question": event.title[:60],
                "leg": opp.token.outcome.lower(), "event_end": event.market.event_end,
                "order_id": order_id, "resolved": False,
            }
            portfolio.cash -= cost
            portfolio.total_trades += 1
            portfolio.total_invested += cost
            scanner.mark_traded(trade_key)


def tick_shadow(portfolio: CombinedPortfolio):
    """Shadow odahoa tick."""
    import requests
    activities = get_activity(TARGET_USER, limit=30)
    if not activities:
        return

    last_ts = max(a.timestamp for a in activities)
    prev_ts = getattr(tick_shadow, "_last_ts", int(time.time()) - 7200)
    tick_shadow._last_ts = last_ts

    for a in activities:
        if a.timestamp <= prev_ts:
            continue
        if a.type != "TRADE" or a.side != "BUY":
            continue

        key = f"{a.transactionHash or ''}:{a.asset}:{a.side}"
        if key in portfolio.tracked_keys:
            continue
        portfolio.tracked_keys.add(key)

        price = a.price or 0.1
        cost = a.usdcSize or 0
        if cost <= 0 or price <= 0:
            continue

        shares = portfolio.cash / price if price > 0 else 0
        shares = min(shares, cost / price if price > 0 else 999)
        shares = max(5, int(shares))

        our_cost = shares * price
        if our_cost > portfolio.cash or our_cost < 1.0:
            logger.info(f"[SHADOW] SKIP {a.outcome} @ ${price:.2f} | need ${our_cost:.2f}, have ${portfolio.cash:.2f}")
            continue

        if f"resolved_{a.conditionId}" in portfolio.tracked_keys:
            continue
        pos_key = f"shd_{a.conditionId}_{a.outcome}"
        if any(pos_key in k for k in portfolio.positions):
            continue

        portfolio.positions[pos_key] = {
            "side": a.outcome or "?", "condition_id": a.conditionId,
            "price_label": f"shadow@{price:.2f}", "entry_price": price,
            "current_price": price, "shares": shares, "notional": our_cost,
            "entry_time": time.time(), "question": (a.title or "")[:60],
            "leg": (a.outcome or "").lower(), "asset": a.asset, "event_end": "",
            "resolved": False,
        }
        portfolio.cash -= our_cost
        portfolio.total_trades += 1
        portfolio.total_invested += our_cost
        logger.info(f"[SHADOW] COPY {a.outcome} @ ${price:.2f} | {shares} shares | Cost=${our_cost:.2f} | Cash=${portfolio.cash:.2f}")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(RUN_DIR / "combined.log")),
        ],
    )

    portfolio = CombinedPortfolio(COMBINED_STATE)
    scanner = MarketScanner(set())

    logger.info("=" * 60)
    logger.info("COMBINED BOT — REVERSE 15m + SHADOW odahoa")
    logger.info(f"Cash: ${portfolio.cash:.2f}")
    logger.info("=" * 60)

    tick_count = 0
    while True:
        try:
            resolve_positions(portfolio)

            tick_count += 1
            if tick_count % 2 == 1:
                tick_reverse(portfolio, scanner)
            if tick_count % 6 == 0:
                tick_shadow(portfolio)

            if tick_count % 12 == 0:
                portfolio.save()

            time.sleep(5)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Tick error: {e}", exc_info=True)
            time.sleep(10)

    portfolio.save()


if __name__ == "__main__":
    main()
