"""Multi-agent trading system (technical-only, no LLM).

Coordinated agent framework using purely technical signals:
  - MarketAnalyst: Computes indicators + technical signals
  - PortfolioManager: Rule-based risk management
  - TradeStrategist: Rule-based decision layer
  - ExecutionAgent: Wraps Hyperliquid executor
  - TradeAuditor: Post-trade analysis
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ── Single Source of Truth ──────────────────────────────────────────────


@dataclass
class MarketView:
    """Unified view of a single symbol's market state."""

    symbol: str
    price: float
    indicators: Dict[str, Any] = field(default_factory=dict)
    technical_signal: Dict[str, Any] = field(default_factory=dict)
    llm_view: Dict[str, Any] = field(default_factory=dict)
    prediction_accuracy: Dict[str, Any] = field(default_factory=dict)
    recent_candles: List[Dict[str, float]] = field(default_factory=list)


@dataclass
class PortfolioPolicy:
    """Portfolio Manager's risk policy."""

    mode: str = "aggressive"  # aggressive / conservative
    max_positions: int = 3
    position_pct: float = 0.50
    min_confidence: int = 40
    hard_stop: float = 14.0
    goal: float = 1000.0
    direction_bias: str = "neutral"  # bullish / bearish / neutral
    reasoning: str = ""


@dataclass
class TradeDecision:
    """Trade Strategist's final decision for a symbol."""

    symbol: str
    action: str = "CLOSE"  # LONG / SHORT / CLOSE
    confidence: int = 0
    size_pct: float = 0.0
    sl_pct: float = 0.03
    tp_pct: float = 0.05
    source: str = "strategist"
    reasoning: str = ""


@dataclass
class MarketState:
    """Single source of truth shared by all agents."""

    tick: int = 0
    prices: Dict[str, float] = field(default_factory=dict)
    market_views: Dict[str, MarketView] = field(default_factory=dict)
    portfolio_policy: Optional[PortfolioPolicy] = None
    decisions: Dict[str, TradeDecision] = field(default_factory=dict)
    equity: float = 0.0
    positions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    trade_log: List[Dict[str, Any]] = field(default_factory=list)
    prediction_tracker: Any = None
    target_equity: float = 1000.0
    hard_stop: float = 14.0


# ── Market Analyst Agent ────────────────────────────────────────────────


class MarketAnalyst:
    """Computes indicators and technical signals only (no LLM)."""

    def __init__(
        self,
        symbols: List[str],
        llm_signal_engine: Any = None,
        prediction_tracker: Any = None,
        min_equity_for_llm: float = 100.0,
    ):
        self.symbols = symbols
        self.prediction_tracker = prediction_tracker

    def analyze(self, state: MarketState, fetcher: Any, signals: Any) -> Dict[str, MarketView]:
        """Produce MarketView for each symbol using only technical indicators."""
        views = {}
        prices = state.prices
        indicators = signals.fetch_indicators(self.symbols)
        data_map = signals.build_data_map(self.symbols)
        tech_signals = signals.generate_entry_signals(indicators)

        for sym in self.symbols:
            if sym not in prices:
                continue
            ind = indicators.get(sym, {})
            tech_sig = tech_signals.get(sym, {})

            # Prediction accuracy (kept for tracking but LLM never called)
            acc = {}
            if self.prediction_tracker:
                err_1h = self.prediction_tracker.get_accuracy(sym, "1h")
                dir_acc = self.prediction_tracker.get_directional_accuracy(sym, "1h")
                if err_1h is not None:
                    acc["1h_error"] = round(err_1h, 2)
                if dir_acc is not None:
                    acc["directional_hit"] = round(dir_acc, 1)

            # Recent candles
            candles = []
            if sym in data_map:
                df = data_map[sym].tail(10)
                for _, row in df.iterrows():
                    candles.append(
                        {
                            "o": float(row["open"]),
                            "h": float(row["high"]),
                            "l": float(row["low"]),
                            "c": float(row["close"]),
                            "v": float(row.get("volume", 0)),
                        }
                    )

            views[sym] = MarketView(
                symbol=sym,
                price=prices[sym],
                indicators=ind,
                technical_signal=tech_sig,
                llm_view={},
                prediction_accuracy=acc,
                recent_candles=candles,
            )

        state.market_views = views
        return views


# ── Portfolio Manager Agent ─────────────────────────────────────────────


class PortfolioManager:
    """Rule-based risk management (no LLM). Goal: grow equity to target.

    Runs every 2 ticks. Sets mode, position limits, direction bias.
    """

    def __init__(self, target_equity: float = 1000.0, hard_stop: float = 14.0, min_equity_for_llm: float = 100.0):
        self.target_equity = target_equity
        self.hard_stop = hard_stop

    def manage(self, state: MarketState) -> PortfolioPolicy:
        """Set risk policy based on portfolio state and market views."""
        equity = state.equity
        trade_log = state.trade_log

        # Hard stop check
        if equity < self.hard_stop:
            policy = PortfolioPolicy(
                mode="conservative",
                max_positions=1,
                position_pct=0.2,
                min_confidence=70,
                hard_stop=self.hard_stop,
                goal=self.target_equity,
                direction_bias="neutral",
                reasoning=f"HARD STOP: equity ${equity:.2f} below ${self.hard_stop}",
            )
            logger.warning("PORTFOLIO HARD STOP: %s", policy.reasoning)
            return policy

        progress = equity / self.target_equity * 100
        realized_pnl = sum(t.get("pnl", 0) for t in trade_log if t.get("action") == "CLOSE")
        total_trades = len([t for t in trade_log if t.get("action") == "CLOSE"])
        wins = len([t for t in trade_log if t.get("action") == "CLOSE" and t.get("pnl", 0) > 0])
        win_rate = wins / total_trades * 100 if total_trades else 50

        # Adaptive sizing based on progress (account for $10 minimum per position)
        min_order = 12.0
        max_affordable = int(equity // min_order)
        if max_affordable < 1:
            max_pos = 0
            position_pct = 0.0
        else:
            if progress < 2:
                position_pct = 0.50
                max_pos = min(3, max_affordable)
            elif progress < 10:
                position_pct = 0.40
                max_pos = min(3, max_affordable)
            elif progress < 50:
                position_pct = 0.30
                max_pos = min(2, max_affordable)
            else:
                position_pct = 0.20
                max_pos = min(2, max_affordable)

        # Mode based on win rate and PnL
        if win_rate > 60 and realized_pnl > 0:
            mode = "aggressive"
            min_conf = 40
        elif win_rate < 40 or realized_pnl < 0:
            mode = "conservative"
            min_conf = 55
        else:
            mode = "balanced"
            min_conf = 45

        policy = PortfolioPolicy(
            mode=mode,
            max_positions=max_pos,
            position_pct=position_pct,
            min_confidence=min_conf,
            hard_stop=self.hard_stop,
            goal=self.target_equity,
            direction_bias="neutral",
            reasoning=f"equity=${equity:.2f} ({progress:.1f}% of goal), win_rate={win_rate:.0f}%, trades={total_trades}",
        )

        state.portfolio_policy = policy
        return policy


# ── Trade Strategist Agent ──────────────────────────────────────────────


class TradeStrategist:
    """Purely rule-based decision layer (no LLM).

    The ONLY agent that decides LONG/SHORT/CLOSE.
    Uses technical signals with confidence filtering.
    """

    def __init__(self, min_equity_for_llm: float = 100.0):
        pass

    def decide(self, state: MarketState) -> Dict[str, TradeDecision]:
        """Make trade decisions for all symbols using technical signals only."""
        decisions = {}
        policy = state.portfolio_policy or PortfolioPolicy()

        for sym, mv in state.market_views.items():
            decision = self._rule_based_decide(sym, mv, policy)
            decisions[sym] = decision

        state.decisions = decisions
        return decisions

    def _rule_based_decide(self, sym: str, mv: MarketView, policy: PortfolioPolicy) -> TradeDecision:
        """Rule-based decision using technical signals only."""
        tech = mv.technical_signal

        tech_action = tech.get("action", "FLAT")
        tech_conf = tech.get("confidence", 50)

        # Only trade on strong technical signals
        if tech_action in ("LONG", "SHORT") and tech_conf >= policy.min_confidence:
            return TradeDecision(
                symbol=sym,
                action=tech_action,
                confidence=tech_conf,
                size_pct=policy.position_pct / policy.max_positions,
                source="technical",
                reasoning=f"Technical: {tech_action} (conf={tech_conf}, reasons={tech.get('reasons', [])})",
            )

        return TradeDecision(symbol=sym, action="CLOSE", confidence=0, reasoning="No valid technical signal")


# ── Execution Agent ─────────────────────────────────────────────────────


class ExecutionAgent:
    """Executes trade decisions on Hyperliquid."""

    def __init__(self, engine: Any, live_executor: Any, live_mode: bool):
        self.engine = engine
        self.live_executor = live_executor
        self.live_mode = live_mode
        self._peak_pnl: Dict[str, float] = {}  # Track best PnL per position for trailing stop
        self._entry_times: Dict[str, float] = {}  # Track entry time per position

    def _close_position(self, sym: str, price: float, reason: str) -> None:
        """Close a position on both paper engine and live executor."""
        self.engine.close_position(sym, price, reason)
        if self.live_mode and self.live_executor:
            try:
                coin = sym.split("-")[0]
                self.live_executor.close_position(coin, 0, price)
                logger.info("LIVE CLOSED: %s @ %.2f [%s]", sym, price, reason)
            except Exception as exc:
                logger.error("Live close failed for %s: %s", sym, exc)
        logger.info("CLOSED: %s @ %.2f [%s]", sym, price, reason)

    def _check_exits(self, state: "MarketState") -> List[Dict[str, Any]]:
        """Check all open positions for PnL-based, trailing, and time-based exits.

        KEY RULE: Never close a position at a loss unless it hits a hard stop loss.
        Positions must be profitable before any signal-based or time-based exit.
        """
        executed = []
        min_pnl_profit = 0.08  # Take profit at $0.08+ (faster turnover on vol coins)
        max_pnl_loss = -2.00  # Hard stop: only cut at $2+ loss (protects capital)
        trailing_drawdown = 0.05  # Tight trailing: close if drops $0.05 from peak
        max_hold_seconds = 600  # 10 min max hold — give positions time to work

        for sym in list(state.positions.keys()):
            mv = state.market_views.get(sym)
            if not mv:
                continue
            price = mv.price
            pos = state.positions[sym]
            direction = pos.get("direction", 0)
            entry = pos.get("entry", 0)
            size = pos.get("size", 0)

            if entry <= 0 or size <= 0:
                continue

            # Calculate unrealized PnL
            if direction > 0:  # LONG
                pnl = (price - entry) * size
            else:  # SHORT
                pnl = (entry - price) * size

            # Track peak PnL for trailing stop
            prev_peak = self._peak_pnl.get(sym, 0)
            if pnl > prev_peak:
                self._peak_pnl[sym] = pnl

            peak = self._peak_pnl.get(sym, 0)

            # 1. Take profit: close when profitable
            if pnl >= min_pnl_profit:
                self._close_position(sym, price, f"TAKE_PROFIT pnl=${pnl:.2f}")
                executed.append({"sym": sym, "action": "CLOSE", "pnl": pnl})
                self._peak_pnl.pop(sym, None)
                continue

            # 2. Hard stop loss: only cut at significant loss
            if pnl <= max_pnl_loss:
                self._close_position(sym, price, f"HARD_STOP pnl=${pnl:.2f}")
                executed.append({"sym": sym, "action": "CLOSE", "pnl": pnl})
                self._peak_pnl.pop(sym, None)
                continue

            # 3. Trailing stop: lock in profits when was profitable and dropped from peak
            if peak >= 0.20 and (peak - pnl) >= trailing_drawdown:
                # Only close if still profitable
                if pnl > 0:
                    self._close_position(sym, price, f"TRAILING_PROFIT peak=${peak:.2f} now=${pnl:.2f}")
                    executed.append({"sym": sym, "action": "CLOSE", "pnl": pnl})
                    self._peak_pnl.pop(sym, None)
                    continue

            # 4. Time-based exit: only close if profitable after max hold time
            entry_time = self._entry_times.get(sym, 0)
            if entry_time > 0 and (time.time() - entry_time) > max_hold_seconds:
                if pnl > 0:
                    self._close_position(sym, price, f"TIMEOUT_PROFIT {max_hold_seconds}s pnl=${pnl:.2f}")
                    executed.append({"sym": sym, "action": "CLOSE", "pnl": pnl})
                    self._peak_pnl.pop(sym, None)
                # If not profitable after timeout, let it run — don't force a loss
                continue

        return executed

    def _track_entry(self, sym: str) -> None:
        """Track when a position was opened."""
        self._entry_times[sym] = time.time()
        self._peak_pnl[sym] = 0.0

    def execute(self, state: MarketState) -> List[Dict[str, Any]]:
        """Execute all trade decisions."""
        executed = []
        policy = state.portfolio_policy or PortfolioPolicy()
        open_count = len(state.positions)
        min_order = 12.0  # Hyperliquid minimum order size — $12 for low-price coins

        # Phase 1: Active PnL monitoring — close winning/losing positions
        pnl_exits = self._check_exits(state)
        executed.extend(pnl_exits)
        for exit_trade in pnl_exits:
            sym = exit_trade.get("sym", "")
            if sym in state.positions:
                open_count -= 1

        # Phase 2: Execute strategist decisions
        for sym, decision in state.decisions.items():
            mv = state.market_views.get(sym)
            if not mv:
                continue

            price = mv.price

            # Handle CLOSE decisions — but NEVER close at a loss on signal alone
            if decision.action == "CLOSE":
                if sym in state.positions:
                    pos = state.positions[sym]
                    direction = pos.get("direction", 0)
                    entry = pos.get("entry", 0)
                    sz = pos.get("size", 0)
                    if direction > 0:
                        pnl = (price - entry) * sz
                    else:
                        pnl = (entry - price) * sz
                    if pnl > 0:
                        self._close_position(sym, price, f"strategist_profit: {decision.reasoning[:50]}")
                        executed.append({"sym": sym, "action": "CLOSE", "pnl": pnl})
                        open_count -= 1
                    else:
                        logger.info("BLOCK CLOSE: %s at loss ($%.2f), waiting for breakeven+", sym, pnl)
                continue

            if open_count >= policy.max_positions:
                continue

            direction = 1 if decision.action == "LONG" else -1
            size_usd = state.equity * decision.size_pct

            # Enforce minimum order size for live trading — bump to min if needed
            if self.live_mode and size_usd < min_order:
                if min_order > state.equity * 0.95:
                    logger.warning("SKIP: %s $%.2f < $%.2f minimum (not enough equity)", sym, size_usd, min_order)
                    continue
                size_usd = min_order
                logger.info("BUMP: %s $%.2f -> $%.2f (minimum)", sym, size_usd, min_order)

            sl = price * (1 - decision.sl_pct) if direction == 1 else price * (1 + decision.sl_pct)
            tp = price * (1 + decision.tp_pct) if direction == 1 else price * (1 - decision.tp_pct)

            # Check if position already exists with correct direction
            if sym in state.positions:
                current_dir = state.positions[sym].get("direction", 0)
                if current_dir == direction:
                    continue
                # Direction flip: close existing
                self._close_position(sym, price, "signal_flip")
                open_count -= 1

            trade = self.engine.open_position(sym, direction, size_usd, price, sl, tp)
            if trade:
                executed.append(trade)
                open_count += 1
                self._track_entry(sym)
                logger.info(
                    "ENTRY: %s %s $%.2f @ %.2f (SL:%.2f TP:%.2f) conf=%d [%s]",
                    sym,
                    decision.action,
                    size_usd,
                    price,
                    sl,
                    tp,
                    decision.confidence,
                    decision.reasoning,
                )

                # Live execution
                if self.live_mode and self.live_executor:
                    try:
                        coin = sym.split("-")[0]
                        direction_str = "Buy" if direction == 1 else "Sell"
                        result = self.live_executor.open_position(coin, direction_str, size_usd, price)
                        if result and not result.get("success", False):
                            logger.warning("Live trade failed for %s: %s", sym, result.get("error", "unknown"))
                        else:
                            logger.info("LIVE EXECUTED: %s %s $%.2f @ %.2f", sym, direction_str, size_usd, price)
                    except Exception as exc:
                        logger.error("Live execution failed for %s: %s", sym, exc)

        return executed


# ── Trade Auditor Agent ─────────────────────────────────────────────────


class TradeAuditor:
    """Reviews trade outcomes and updates prediction tracker."""

    def __init__(self, prediction_tracker: Any):
        self.prediction_tracker = prediction_tracker

    def audit(self, state: MarketState, executed_trades: List[Dict[str, Any]]) -> None:
        """Audit executed trades and update prediction accuracy."""
        for trade in executed_trades:
            sym = trade.get("sym", "")
            action = trade.get("action", "")
            if action == "CLOSE":
                pnl = trade.get("pnl", 0)
                reason = trade.get("reason", "")
                logger.info(
                    "AUDIT: %s closed | PnL: $%.2f | reason: %s",
                    sym,
                    pnl,
                    reason,
                )
                # Update prediction tracker with close price
                if sym in state.prices:
                    self.prediction_tracker.update_with_prices(state.prices)


# NOTE: All LLM prompts removed — system is now purely technical signal-based.
