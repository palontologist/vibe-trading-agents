"""
Polymarket 15-Minute Reverse Bot — ported from TypeScript source.

Architecture:
1. MarketScanner: Gamma /events?tag_slug=15M → filter by slug prefix + window timing
2. TokenBook: CLOB /book → best bid/ask per token
3. Strategy: pickReverseToken = cheapest by bestAsk → place limit orders at price ladder
4. Trader: CLOB v2 createAndPostOrder with proper tickSize + negRisk
5. TradeKeys: Set-based dedup per slug+outcome+kind+price
"""

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logger = logging.getLogger(__name__)

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

MARKET_SLUG_PREFIXES = ["btc-updown-15m", "eth-updown-15m"]
CHEAP_BUY_MIN = float(os.environ.get("CHEAP_BUY_MIN", "0.07"))
CHEAP_BUY_MAX = float(os.environ.get("CHEAP_BUY_MAX", "0.10"))
EXPENSIVE_BUY_MIN = float(os.environ.get("EXPENSIVE_BUY_MIN", "0.90"))
EXPENSIVE_BUY_MAX = float(os.environ.get("EXPENSIVE_BUY_MAX", "0.95"))
ENABLE_EXPENSIVE_HEDGE = os.environ.get("ENABLE_EXPENSIVE_HEDGE", "true").lower() == "true"
CHEAP_ORDER_USDC = float(os.environ.get("CHEAP_ORDER_USDC", "1.00"))
EXPENSIVE_ORDER_USDC = float(os.environ.get("EXPENSIVE_ORDER_USDC", "1.00"))
MAX_SHARES_PER_ORDER = int(os.environ.get("MAX_SHARES_PER_ORDER", "90"))
POLL_INTERVAL_MS = int(os.environ.get("POLL_INTERVAL_MS", "5000"))
MINUTES_BEFORE_CLOSE_MIN = float(os.environ.get("MINUTES_BEFORE_CLOSE_MIN", "0"))
MINUTES_BEFORE_CLOSE_MAX = float(os.environ.get("MINUTES_BEFORE_CLOSE_MAX", "15"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

RUN_DIR = Path(__file__).resolve().parent.parent.parent / "paper_runs" / "polymarket_v2"
RUN_DIR.mkdir(parents=True, exist_ok=True)
LIVE_STATE = RUN_DIR / "live_state.json"
PAPER_STATE = RUN_DIR / "paper_state.json"

# ── Data Types ────────────────────────────────────────────────────────────────

@dataclass
class GammaMarket:
    question: str = ""
    condition_id: str = ""
    slug: str = ""
    clob_token_ids: str = "[]"
    outcomes: str = "[]"
    neg_risk: bool = False
    order_price_min_tick_size: float = 0.01
    active: bool = True
    closed: bool = False
    accepting_orders: bool = True
    event_slug: str = ""
    event_end: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "GammaMarket":
        return cls(
            question=d.get("question", ""),
            condition_id=d.get("conditionId", ""),
            slug=d.get("slug", ""),
            clob_token_ids=d.get("clobTokenIds", "[]"),
            outcomes=d.get("outcomes", "[]"),
            neg_risk=d.get("negRisk", False),
            order_price_min_tick_size=float(d.get("orderPriceMinTickSize", 0.01)),
            active=d.get("active", True),
            closed=d.get("closed", False),
            accepting_orders=d.get("acceptingOrders", True),
            event_slug=d.get("_event_slug", ""),
            event_end=d.get("_event_end", ""),
        )


@dataclass
class UpDownEvent:
    title: str
    slug: str
    market: GammaMarket
    window_start: int
    window_end: int


@dataclass
class TokenBook:
    token_id: str
    outcome: str
    outcome_index: int
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None


@dataclass
class TradeOpportunity:
    kind: str  # "cheap" or "expensive"
    event: UpDownEvent
    token: TokenBook
    price: float
    size: int
    tick_size: str
    neg_risk: bool


# ── Market Scanner ────────────────────────────────────────────────────────────

class MarketScanner:
    """Port of TypeScript MarketScanner — uses Gamma /events?tag_slug=15M."""

    def __init__(self, traded_keys: Set[str]):
        self.traded_keys = traded_keys

    def scan(self) -> List[UpDownEvent]:
        """Find active 15m events within tradeable window."""
        url = f"{GAMMA_HOST}/events"
        params = {
            "tag_slug": "15M",
            "active": "true",
            "closed": "false",
            "limit": "200",
        }

        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            logger.warning(f"Gamma events API error: {e}")
            return []

        now = int(time.time())
        results = []

        for event in events:
            slug = event.get("slug", "")
            if not any(slug.startswith(p) for p in MARKET_SLUG_PREFIXES):
                continue

            markets = event.get("markets", [])
            if not markets:
                continue

            market = markets[0]
            if market.get("closed") or not market.get("active", True):
                continue

            end_date_str = event.get("endDate", "")
            if not end_date_str:
                continue
            try:
                if "T" in end_date_str:
                    end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    end_ts = end_dt.timestamp()
                else:
                    end_ts = float(end_date_str)
            except (ValueError, TypeError):
                continue

            if now > end_ts:
                continue

            minutes_left = (end_ts - now) / 60
            if minutes_left < MINUTES_BEFORE_CLOSE_MIN or minutes_left > MINUTES_BEFORE_CLOSE_MAX:
                continue

            window_start = self._parse_window_start(slug)
            if window_start is None:
                window_start = int(end_ts) - WINDOW_SECONDS
            window_end = int(end_ts)

            gm = GammaMarket.from_dict(market)
            gm.event_slug = slug
            gm.event_end = end_date_str

            results.append(UpDownEvent(
                title=event.get("title", ""),
                slug=slug,
                market=gm,
                window_start=window_start,
                window_end=window_end,
            ))

        return results

    def get_token_books(self, event: UpDownEvent) -> List[TokenBook]:
        """Fetch CLOB order book for each token in the event."""
        token_ids = json.loads(event.market.clob_token_ids)
        outcomes = json.loads(event.market.outcomes)
        books = []

        for i, token_id in enumerate(token_ids):
            if not token_id:
                continue
            book_data = self._fetch_order_book(token_id)
            best_bid = self._best_price(book_data.get("bids", []), "bid")
            best_ask = self._best_price(book_data.get("asks", []), "ask")
            books.append(TokenBook(
                token_id=token_id,
                outcome=outcomes[i] if i < len(outcomes) else f"Outcome {i}",
                outcome_index=i,
                best_bid=best_bid,
                best_ask=best_ask,
            ))

        return books

    def get_tick_size(self, market: GammaMarket) -> str:
        tick = market.order_price_min_tick_size
        if tick >= 0.1:
            return "0.1"
        if tick >= 0.01:
            return "0.01"
        return "0.001"

    def make_trade_key(self, event_slug: str, outcome: str, kind: str) -> str:
        return f"{event_slug}:{outcome}:{kind}"

    def has_traded(self, key: str) -> bool:
        return key in self.traded_keys

    def mark_traded(self, key: str):
        self.traded_keys.add(key)

    @staticmethod
    def _parse_window_start(slug: str) -> Optional[int]:
        match = re.search(r"-(\d{10})$", slug)
        return int(match.group(1)) if match else None

    @staticmethod
    def _fetch_order_book(token_id: str) -> dict:
        try:
            resp = requests.get(f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    @staticmethod
    def _best_price(levels: list, mode: str) -> Optional[float]:
        if not levels:
            return None
        prices = []
        for level in levels:
            try:
                prices.append(float(level.get("price", "nan")))
            except (ValueError, TypeError):
                continue
        if not prices:
            return None
        return max(prices) if mode == "bid" else min(prices)


# ── Strategy ──────────────────────────────────────────────────────────────────

def compute_size(usdc_budget: float, price: float, max_shares: int) -> int:
    shares = usdc_budget / max(price, 0.01)
    capped = min(shares, max_shares)
    return max(1, int(capped * 100) / 100)


def cheap_limit_prices() -> List[float]:
    prices = []
    p = CHEAP_BUY_MIN
    while p <= CHEAP_BUY_MAX + 0.0001:
        prices.append(round(p, 2))
        p += 0.01
    return prices


def expensive_limit_prices() -> List[float]:
    prices = []
    p = EXPENSIVE_BUY_MIN
    while p <= EXPENSIVE_BUY_MAX + 0.0001:
        prices.append(round(p, 2))
        p += 0.01
    return prices


def pick_reverse_token(books: List[TokenBook]) -> Optional[TokenBook]:
    """Pick the CHEAPEST outcome by bestAsk — the underdog / reversal bet."""
    with_ask = [b for b in books if b.best_ask is not None]
    if not with_ask:
        return None
    return min(with_ask, key=lambda b: b.best_ask or 1)


def find_opportunities(
    scanner: MarketScanner,
    event: UpDownEvent,
    books: List[TokenBook],
) -> List[TradeOpportunity]:
    """Port of TypeScript findOpportunities — generates cheap + expensive orders."""
    opportunities = []

    reverse_token = pick_reverse_token(books)
    if reverse_token:
        for price in cheap_limit_prices():
            key = scanner.make_trade_key(event.slug, reverse_token.outcome, f"cheap-{price:.2f}")
            if scanner.has_traded(key):
                continue
            opportunities.append(TradeOpportunity(
                kind="cheap",
                event=event,
                token=reverse_token,
                price=price,
                size=compute_size(CHEAP_ORDER_USDC, price, MAX_SHARES_PER_ORDER),
                tick_size=scanner.get_tick_size(event.market),
                neg_risk=event.market.neg_risk,
            ))

    if ENABLE_EXPENSIVE_HEDGE and reverse_token:
        favorite = next(
            (b for b in books if b.token_id != reverse_token.token_id and b.best_ask is not None),
            None,
        )
        if favorite:
            for price in expensive_limit_prices():
                key = scanner.make_trade_key(event.slug, favorite.outcome, f"expensive-{price:.2f}")
                if scanner.has_traded(key):
                    continue
                opportunities.append(TradeOpportunity(
                    kind="expensive",
                    event=event,
                    token=favorite,
                    price=price,
                    size=compute_size(EXPENSIVE_ORDER_USDC, price, MAX_SHARES_PER_ORDER),
                    tick_size=scanner.get_tick_size(event.market),
                    neg_risk=event.market.neg_risk,
                ))

    return opportunities


# ── Trader ────────────────────────────────────────────────────────────────────

class Trader:
    """Place real orders on Polymarket CLOB using py-clob-client-v2."""

    def __init__(self):
        self._client = None
        self._balance = 0
        self._init_client()

    def _init_client(self):
        if DRY_RUN:
            return
        if not PRIVATE_KEY:
            logger.error("PRIVATE_KEY required for live trading")
            return

        try:
            from py_clob_client_v2 import ClobClient

            boot = ClobClient(
                host=CLOB_HOST,
                chain_id=CHAIN_ID,
                key=PRIVATE_KEY,
                signature_type=3,
                funder=DEPOSIT_WALLET,
            )

            creds = None
            if CLOB_API_KEY and CLOB_API_SECRET and CLOB_API_PASSPHRASE:
                from py_clob_client_v2.clob_types import ApiCreds
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

            self._client = ClobClient(
                host=CLOB_HOST,
                chain_id=CHAIN_ID,
                key=PRIVATE_KEY,
                creds=creds,
                signature_type=3,
                funder=DEPOSIT_WALLET,
            )

            from py_clob_client_v2.clob_types import BalanceAllowanceParams
            params = BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=3)
            self._client.update_balance_allowance(params)
            bal = self._client.get_balance_allowance(params)
            self._balance = int(bal.get("balance", 0))
            logger.info(f"Trader init: wallet={DEPOSIT_WALLET[:12]}... balance=${self._balance / 1e6:.2f}")

        except Exception as e:
            logger.error(f"Trader init failed: {e}")

    @property
    def balance_usd(self) -> float:
        return self._balance / 1e6 if self._balance else 0.0

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

    def place_buy(self, opp: TradeOpportunity) -> Optional[Dict]:
        """Place a GTC limit BUY order. Returns order dict or None."""
        if DRY_RUN:
            return {"dry_run": True, "price": opp.price, "size": opp.size}

        if not self._client:
            return None

        if opp.size < 5:
            return None

        if opp.price * opp.size < 1.0:
            opp.size = max(5, int(1.0 / opp.price) + 1)

        try:
            from py_clob_client_v2 import OrderType, Side
            from py_clob_client_v2.clob_types import OrderArgs, CreateOrderOptions

            resp = self._client.create_and_post_order(
                OrderArgs(
                    price=opp.price,
                    size=opp.size,
                    side=Side.BUY,
                    token_id=opp.token.token_id,
                ),
                CreateOrderOptions(
                    tick_size=opp.tick_size,
                    neg_risk=opp.neg_risk,
                ),
                OrderType.GTC,
            )

            if resp.get("success"):
                logger.info(f"LIVE ORDER: {opp.kind.upper()} {opp.token.outcome} {opp.price:.2f} | {opp.size} shares | ID={resp.get('orderID', '?')[:16]}...")
                return {"order_id": resp.get("orderID"), "price": opp.price, "size": opp.size}
            else:
                logger.warning(f"Order failed: {resp}")
                return None

        except Exception as e:
            logger.error(f"Order error: {e}")
            return None

    def get_open_orders(self) -> List[Dict]:
        if not self._client:
            return []
        try:
            orders = self._client.get_open_orders()
            return orders if isinstance(orders, list) else []
        except Exception:
            return []


# ── Portfolio ─────────────────────────────────────────────────────────────────

class Portfolio:
    def __init__(self, initial_cash: float, state_path: Path):
        self.cash = initial_cash
        self.initial_cash = initial_cash
        self.positions: Dict[str, Dict] = {}
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.total_invested = 0.0
        self._state_file = state_path
        self._load_state()

    def _load_state(self):
        if self._state_file.exists():
            try:
                d = json.loads(self._state_file.read_text())
                self.cash = d.get("cash", self.initial_cash)
                self.initial_cash = d.get("initial_cash", self.initial_cash)
                self.positions = d.get("positions", {})
                self.total_trades = d.get("total_trades", 0)
                self.wins = d.get("wins", 0)
                self.losses = d.get("losses", 0)
                self.total_pnl = d.get("total_pnl", 0.0)
                self.total_invested = d.get("total_invested", 0.0)
            except Exception:
                pass

    def save_state(self):
        state = {
            "cash": self.cash,
            "initial_cash": self.initial_cash,
            "positions": self.positions,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": self.total_pnl,
            "total_invested": self.total_invested,
            "win_rate": self.wins / max(1, self.total_trades),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._state_file.write_text(json.dumps(state, indent=2))

    def can_place_order(self, condition_id: str, price_label: str) -> bool:
        key = f"{condition_id}_{price_label}"
        return key not in self.positions

    def record_order(self, opp: TradeOpportunity, cost: float, order_id: Optional[str] = None):
        key = f"{opp.event.market.condition_id}_{opp.kind}@{opp.price:.2f}"
        self.positions[key] = {
            "side": opp.token.outcome,
            "condition_id": opp.event.market.condition_id,
            "price_label": f"{opp.kind}@{opp.price:.2f}",
            "entry_price": opp.price,
            "current_price": opp.price,
            "shares": opp.size,
            "notional": cost,
            "entry_time": time.time(),
            "question": opp.event.title,
            "leg": opp.token.outcome.lower(),
            "event_end": opp.event.market.event_end,
            "order_id": order_id,
            "resolved": False,
        }
        self.cash -= cost
        self.total_trades += 1
        self.total_invested += cost
        self.save_state()

    def sync_cash(self, clob_balance: float):
        if abs(self.cash - clob_balance) > 0.01:
            logger.info(f"Syncing cash: ${self.cash:.2f} → ${clob_balance:.2f}")
            self.cash = clob_balance
            self.save_state()


# ── Main Loop ─────────────────────────────────────────────────────────────────

class ReverseBot:
    def __init__(self, live: bool = False):
        self.live = live
        self.traded_keys: Set[str] = set()
        self.scanner = MarketScanner(self.traded_keys)
        self.trader = Trader() if live else None
        self.portfolio = Portfolio(
            self.trader.balance_usd if live else 9.57,
            LIVE_STATE if live else PAPER_STATE,
        )
        if live and self.trader:
            self.portfolio.sync_cash(self.trader.balance_usd)
        self._tick_count = 0
        self._placed_this_window: Dict[str, Set[str]] = {}
        self._resolved_conditions: Set[str] = set()

    def run(self):
        mode = "LIVE" if self.live else "PAPER"
        logger.info("=" * 60)
        logger.info(f"REVERSE BOT — {mode} MODE")
        logger.info(f"Cheap: {CHEAP_BUY_MIN}-{CHEAP_BUY_MAX} (${CHEAP_ORDER_USDC}/order)")
        logger.info(f"Hedge: {ENABLE_EXPENSIVE_HEDGE} | Max shares: {MAX_SHARES_PER_ORDER}")
        if self.live:
            logger.info(f"Wallet: {DEPOSIT_WALLET}")
            logger.info(f"Balance: ${self.portfolio.cash:.2f}")
        else:
            logger.info(f"Capital: ${self.portfolio.cash:.2f}")
        logger.info("=" * 60)

        while True:
            try:
                self._tick()
                time.sleep(POLL_INTERVAL_MS / 1000)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Tick error: {e}", exc_info=True)
                time.sleep(10)

        logger.info("Stopped.")

    def _resolve_positions(self):
        """Check all unresolved positions. If window ended, query CLOB for outcome and settle."""
        now_ts = time.time()
        to_remove = []

        for key, pos in self.portfolio.positions.items():
            if pos.get("resolved"):
                continue

            event_end_str = pos.get("event_end", "")
            if not event_end_str:
                continue

            try:
                if "T" in event_end_str:
                    end_dt = datetime.fromisoformat(event_end_str.replace("Z", "+00:00"))
                    end_ts = end_dt.timestamp()
                else:
                    end_ts = float(event_end_str)
            except (ValueError, TypeError):
                continue

            if now_ts < end_ts + 60:
                continue

            cid = pos.get("condition_id", "")
            side = pos.get("leg", "").lower()
            shares = pos.get("shares", 0)
            entry_price = pos.get("entry_price", 0)

            won = self._check_resolution_clob(cid, side)

            if won is None:
                continue

            if won:
                payout = shares * 1.0
                pnl = payout - pos["notional"]
                self.portfolio.cash += payout
                self.portfolio.wins += 1
                logger.info(
                    f"[RESOLVE] WIN {pos.get('question','?')[:40]} | "
                    f"{side} @ {entry_price:.2f} | {shares} shares → +${pnl:.2f}"
                )
            else:
                pnl = -pos["notional"]
                self.portfolio.losses += 1
                logger.info(
                    f"[RESOLVE] LOSS {pos.get('question','?')[:40]} | "
                    f"{side} @ {entry_price:.2f} | {shares} shares → -${pos['notional']:.2f}"
                )

            pos["resolved"] = True
            pos["pnl"] = pnl
            self.portfolio.total_pnl += pnl
            to_remove.append(key)

        if to_remove:
            for key in to_remove:
                cid = self.portfolio.positions[key].get("condition_id", "")
                if cid:
                    self._resolved_conditions.add(cid)
                del self.portfolio.positions[key]
            self.portfolio.save_state()

    def _check_resolution_clob(self, condition_id: str, side: str) -> Optional[bool]:
        """Query CLOB /markets/{condition_id} to check resolution. Returns True if side won."""
        try:
            resp = requests.get(
                f"{CLOB_HOST}/markets/{condition_id}",
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            market = resp.json()
        except Exception as e:
            logger.warning(f"CLOB resolution check failed for {condition_id[:12]}...: {e}")
            return None

        tokens = market.get("tokens", [])

        for token in tokens:
            if token.get("winner"):
                winning_outcome = token.get("outcome", "").lower()
                return winning_outcome == side

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

    def _tick(self):
        self._tick_count += 1

        self._resolve_positions()

        events = self.scanner.scan()
        if not events:
            if self._tick_count % 60 == 0:
                logger.info(f"Tick {self._tick_count}: No active markets")
            return

        for event in events:
            if self.portfolio.cash < 0.50:
                logger.info(f"Low cash (${self.portfolio.cash:.2f}), waiting for resolution")
                break

            cid = event.market.condition_id
            if cid in self._resolved_conditions:
                continue
            if any(
                p.get("condition_id") == cid and not p.get("resolved")
                for p in self.portfolio.positions.values()
            ):
                continue

            books = self.scanner.get_token_books(event)
            opportunities = find_opportunities(self.scanner, event, books)

            if not opportunities:
                if self._tick_count % 60 == 0:
                    book_summary = ", ".join(f"{b.outcome}: ask={b.best_ask}" for b in books if b.best_ask)
                    logger.info(f"Watching: {event.title[:50]} | {book_summary}")
                continue

            for opp in opportunities:
                trade_key = self.scanner.make_trade_key(
                    event.slug, opp.token.outcome, f"{opp.kind}-{opp.price:.2f}"
                )

                cost = opp.price * opp.size

                if cost < 1.0 or opp.size < 5:
                    opp.size = max(5, int(1.0 / opp.price) + 1)
                    cost = opp.price * opp.size

                if cost > self.portfolio.cash:
                    continue

                if self.live and self.trader:
                    result = self.trader.place_buy(opp)
                    if not result:
                        continue
                    order_id = result.get("order_id")
                else:
                    order_id = None
                    tag = "PAPER"
                    logger.info(
                        f"[PAPER] {opp.kind.upper()} {opp.token.outcome} {opp.price:.2f} | "
                        f"{opp.size} shares @ ${opp.price:.2f} | Cost=${cost:.2f} | Cash=${self.portfolio.cash:.2f}"
                    )

                self.portfolio.record_order(opp, cost, order_id)
                self.scanner.mark_traded(trade_key)

                if self.live and not DRY_RUN:
                    tag = "LIVE"
                    logger.info(
                        f"[{tag}] {opp.kind.upper()} {opp.token.outcome} {opp.price:.2f} | "
                        f"{opp.size} shares @ ${opp.price:.2f} | Cost=${cost:.2f} | Cash=${self.portfolio.cash:.2f}"
                    )

            # Check fills periodically
            if self.live and self.trader and self._tick_count % 12 == 0:
                self._check_fills()

    def _check_fills(self):
        if not self.trader:
            return
        try:
            old = self.portfolio.cash
            self.trader.refresh_balance()
            new = self.trader.balance_usd
            if abs(new - old) > 0.01:
                logger.info(f"Balance changed: ${old:.2f} → ${new:.2f}")
                self.portfolio.sync_cash(new)
        except Exception as e:
            logger.warning(f"Fill check error: {e}")


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Polymarket 15m Reverse Bot")
    parser.add_argument("--live", action="store_true", help="Live trading mode")
    args = parser.parse_args()

    live = args.live and not DRY_RUN

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(RUN_DIR / ("stdout_live.log" if live else "stdout_paper.log"))),
        ],
    )

    bot = ReverseBot(live=live)
    bot.run()


if __name__ == "__main__":
    main()
