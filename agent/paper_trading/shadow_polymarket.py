"""
Shadow Trader — copies Polymarket trades from target user.
Tracks position via Polymarket Data API, places paper trades scaled to our balance.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

import requests

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent.paper_trading.copy_trader import Activity, get_activity
from agent.paper_trading.reverse_15m import CLOB_HOST, Portfolio, RUN_DIR

logger = logging.getLogger(__name__)

SHADOW_STATE = RUN_DIR / "shadow_state.json"
TARGET_USER = os.environ.get("SHADOW_TARGET", "0xe2511c9e41c5e762887e538b1d6e7221807aa237")
POLL_INTERVAL = int(os.environ.get("SHADOW_POLL_MS", "30000")) // 1000
MAX_COPY_USDC = float(os.environ.get("SHADOW_MAX_PER_TRADE", "1.50"))
MIN_SHARES = 5
RESERVE_AMOUNT = float(os.environ.get("SHADOW_RESERVE", "1.00"))

def estimate_bankroll() -> float:
    """Estimate target's total invested capital from portfolio snapshot."""
    try:
        resp = requests.get(
            f"https://polymarket.com/@{os.environ.get('SHADOW_USERNAME', 'odahoa')}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        import re
        m = re.search(r'__NEXT_DATA__.*?</script>', resp.text)
        if not m:
            return 1000.0
        raw = m.group().replace('__NEXT_DATA__', '').replace('</script>', '').strip()
        start = raw.find('{')
        end = raw.rfind('}')
        d = json.loads(raw[start:end+1])
        pp = d['props']['pageProps']
        ds = pp['dehydratedState']
        for q in ds['queries']:
            data = q['state']['data']
            if isinstance(data, dict) and 'pages' in data:
                total = 0
                for page in data['pages']:
                    for item in page:
                        total += item.get('initialValue', 0)
                return max(total, 100.0)
    except Exception:
        pass
    return 1000.0

class ShadowPortfolio(Portfolio):
    def __init__(self):
        super().__init__(RESERVE_AMOUNT, SHADOW_STATE)
        self.tracked_keys: Set[str] = set()
        self._resolved_conditions: Set[str] = set()
        self._load_tracked()

    def _load_tracked(self):
        if SHADOW_STATE.exists():
            try:
                d = json.loads(SHADOW_STATE.read_text())
                self.tracked_keys = set(d.get("tracked_keys", []))
                self._resolved_conditions = set(d.get("resolved_conditions", []))
            except Exception:
                pass

    def save_state(self):
        state = {
            "cash": self.cash,
            "initial_cash": self.initial_cash,
            "positions": {k: v for k, v in self.positions.items()},
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


def _check_resolution_clob(condition_id: str, side: str) -> Optional[bool]:
    """Query CLOB /markets/{condition_id} to check resolution. Returns True if side won."""
    try:
        resp = requests.get(f"{CLOB_HOST}/markets/{condition_id}", timeout=10)
        if resp.status_code != 200:
            return None
        market = resp.json()
    except Exception as e:
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


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(RUN_DIR / "shadow.log")),
        ],
    )

    portfolio = ShadowPortfolio()
    bankroll = estimate_bankroll()
    scale = portfolio.initial_cash / bankroll

    logger.info("=" * 60)
    logger.info("SHADOW TRADER — PAPER MODE")
    logger.info(f"Target: {TARGET_USER[:20]}...")
    logger.info(f"Target bankroll: ~${bankroll:.0f}")
    logger.info(f"Scale factor: {scale:.4f}x")
    logger.info(f"Our cash: ${portfolio.cash:.2f}")
    logger.info("=" * 60)

    last_ts = int(time.time()) - 7200  # catch last 2 hours on first run

    while True:
        try:
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
                    cycle_scale = min(portfolio.cash / total_spend, 1.0)
                    logger.info(f"Cycle scale: {cycle_scale:.4f}x (${portfolio.cash:.2f} / ${total_spend:.2f})")
                else:
                    cycle_scale = scale
                for a in new_trades:
                    _process_trade(portfolio, a, cycle_scale)

                portfolio.save_state()

            if activities:
                last_ts = max(a.timestamp for a in activities)

            _resolve_shadow_positions(portfolio)

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Tick error: {e}", exc_info=True)
            time.sleep(10)

    logger.info("Stopped.")


def _process_trade(portfolio: ShadowPortfolio, a: Activity, scale: float):
    price = a.price or 0.1
    cost = a.usdcSize or 0

    if cost <= 0 or price <= 0:
        return

    our_cost = min(cost * scale, MAX_COPY_USDC, portfolio.cash)
    if our_cost <= 0:
        return
    shares = our_cost / price

    if shares < 1 and price * 1 <= portfolio.cash:
        shares = 1
        our_cost = shares * price
    elif shares < 1:
        logger.info(f"SKIP {a.outcome} @ ${price:.2f} | {shares:.2f} shares too small")
        return

    if our_cost > portfolio.cash or shares < 1:
        logger.info(f"SKIP {a.outcome} @ ${price:.2f} | need ${our_cost:.2f}, have ${portfolio.cash:.2f}")
        return

    if f"resolved_{a.conditionId}" in portfolio.tracked_keys:
        return

    opp_key = f"shadow_{a.conditionId}_{a.outcome}"
    if any(opp_key in k for k in portfolio.positions):
        return

    portfolio.positions[opp_key] = {
        "side": a.outcome or "?",
        "condition_id": a.conditionId,
        "price_label": f"shadow@{price:.2f}",
        "entry_price": price,
        "current_price": price,
        "shares": shares,
        "notional": our_cost,
        "entry_time": time.time(),
        "question": (a.title or "")[:60],
        "leg": (a.outcome or "").lower(),
        "asset": a.asset,
        "event_end": "",
        "resolved": False,
    }
    portfolio.cash -= our_cost
    portfolio.total_trades += 1
    portfolio.total_invested += our_cost

    logger.info(
        f"[COPY] {a.outcome} @ ${price:.2f} | {shares:.1f} shares | "
        f"Cost=${our_cost:.2f} | Cash=${portfolio.cash:.2f} | {a.title}"
    )


def _resolve_shadow_positions(portfolio: ShadowPortfolio):
    now = time.time()
    to_remove = []

    for key, pos in portfolio.positions.items():
        if pos.get("resolved"):
            continue

        cid = pos.get("condition_id", "")
        if not cid:
            continue

        won = _check_resolution_clob(cid, pos.get("leg", ""))
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
        portfolio.save_state()


if __name__ == "__main__":
    main()
