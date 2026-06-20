"""Real-time autonomous trading agent (technical-only, no LLM).

Runs a continuous loop that:
  1. Fetches live tick prices every N seconds
  2. Runs technical analysis signals
  3. Executes paper trades with Kelly sizing
  4. Monitors open positions with trailing stops, SL, TP
  5. Only closes positions at profit

Designed to run fully autonomous — no human intervention needed.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# NOTE: All LLM prompts and timing agent removed — system is now purely technical signal-based.
from paper_trading.multi_agent import (
    MarketState,
    MarketAnalyst,
    PortfolioManager,
    TradeStrategist,
    ExecutionAgent,
    TradeAuditor,
)
import pandas as pd

# ── Real-time data fetcher ─────────────────────────────────────────────


class RealtimeFetcher:
    """Fetch live prices and tick data via Hyperliquid API (no rate limits)."""

    def __init__(self, symbols: List[str]):
        self.symbols = symbols
        self._exchange = None
        self._tick_history: Dict[str, List[float]] = {}
        self._max_ticks = 60
        self._info = None

    def _get_info(self):
        if self._info is None:
            from hyperliquid.info import Info
            from hyperliquid.utils import constants

            self._info = Info(constants.MAINNET_API_URL, skip_ws=True)
        return self._info

    def _get_exchange(self):
        if self._exchange is None:
            import ccxt

            self._exchange = ccxt.okx({"enableRateLimit": True})
        return self._exchange

    def fetch_prices(self) -> Dict[str, float]:
        prices = {}
        info = self._get_info()
        mids = info.all_mids()

        for sym in self.symbols:
            coin = sym.split("-")[0]
            try:
                # all_mids() uses coin names as keys (e.g., 'BTC', 'ETH')
                if coin in mids:
                    price = float(mids[coin])
                else:
                    logger.warning("No mid price for %s (tried %s)", sym, coin)
                    continue
                prices[sym] = price
                if sym not in self._tick_history:
                    self._tick_history[sym] = []
                self._tick_history[sym].append(price)
                if len(self._tick_history[sym]) > self._max_ticks:
                    self._tick_history[sym] = self._tick_history[sym][-self._max_ticks :]
            except Exception as exc:
                logger.warning("Price fetch failed for %s: %s", sym, exc)
        return prices

    def get_ticks(self, symbol: str, count: int = 10) -> List[float]:
        return self._tick_history.get(symbol, [])[-count:]

    def get_recent_change(self, symbol: str, ticks: int = 5) -> float:
        history = self._tick_history.get(symbol, [])
        if len(history) < ticks + 1:
            return 0.0
        old = history[-(ticks + 1)]
        new = history[-1]
        return (new - old) / old * 100


# ── Technical signal generator ─────────────────────────────────────────


class TechnicalSignals:
    """Pure technical analysis signals (no LLM needed for basics)."""

    def __init__(self, symbols: List[str]):
        self.symbols = symbols
        self._exchange = None
        self._caches: Dict[str, Any] = {}

    def _get_info(self):
        if not hasattr(self, "_info") or self._info is None:
            from hyperliquid.info import Info
            from hyperliquid.utils import constants

            self._info = Info(constants.MAINNET_API_URL, skip_ws=True)
        return self._info

    def fetch_indicators(self, symbols: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
        targets = symbols or self.symbols
        info = self._get_info()
        indicators = {}
        now = int(time.time() * 1000)
        start = now - 100 * 3600 * 1000  # 100 hours ago

        for sym in targets:
            coin = sym.split("-")[0]
            try:
                candles = info.candles_snapshot(coin, "1h", start, now)
                if len(candles) < 30:
                    continue

                closes = [float(c["c"]) for c in candles]
                highs = [float(c["h"]) for c in candles]
                lows = [float(c["l"]) for c in candles]
                volumes = [float(c["v"]) for c in candles]
                current = closes[-1]

                ma10 = sum(closes[-10:]) / 10
                ma20 = sum(closes[-20:]) / 20
                ma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else current

                deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
                recent = deltas[-14:]
                gains = sum(d for d in recent if d > 0)
                losses = sum(-d for d in recent if d < 0)
                rsi = 100 - (100 / (1 + gains / losses)) if losses > 0 else (100 if gains > 0 else 50)

                mean20 = sum(closes[-20:]) / 20
                std20 = (sum((c - mean20) ** 2 for c in closes[-20:]) / 20) ** 0.5
                boll_lo = mean20 - 2 * std20
                boll_hi = mean20 + 2 * std20

                vol_mean = sum(volumes[-20:]) / 20
                vol_ratio = volumes[-1] / vol_mean if vol_mean > 0 else 1.0

                atr = sum(highs[-14:]) / 14 - sum(lows[-14:]) / 14

                indicators[sym] = {
                    "price": current,
                    "ma10": round(ma10, 4),
                    "ma20": round(ma20, 4),
                    "ma50": round(ma50, 4),
                    "rsi": round(rsi, 1),
                    "boll_lo": round(boll_lo, 4),
                    "boll_hi": round(boll_hi, 4),
                    "atr": round(atr, 4),
                    "vol_ratio": round(vol_ratio, 2),
                }
                self._caches[sym] = indicators[sym]
            except Exception as exc:
                logger.warning("Indicator fetch failed for %s: %s", sym, exc)

        return indicators

    def build_data_map(self, symbols: Optional[List[str]] = None) -> Dict[str, pd.DataFrame]:
        """Fetch candles and return as Dict[symbol -> OHLCV DataFrame] for LLM engine."""
        targets = symbols or self.symbols
        info = self._get_info()
        data_map = {}
        now = int(time.time() * 1000)
        start = now - 100 * 3600 * 1000

        for sym in targets:
            coin = sym.split("-")[0]
            try:
                candles = info.candles_snapshot(coin, "1h", start, now)
                if len(candles) < 10:
                    continue
                df = pd.DataFrame(candles)
                df["open"] = df["o"].astype(float)
                df["close"] = df["c"].astype(float)
                df["high"] = df["h"].astype(float)
                df["low"] = df["l"].astype(float)
                df["volume"] = df["v"].astype(float)
                data_map[sym] = df[["open", "high", "low", "close", "volume"]]
            except Exception as exc:
                logger.warning("Data map build failed for %s: %s", sym, exc)

        return data_map

    def generate_entry_signals(
        self, indicators: Optional[Dict[str, Dict[str, Any]]] = None
    ) -> Dict[str, Dict[str, Any]]:
        if not indicators:
            indicators = self.fetch_indicators()

        signals = {}
        for sym, ind in indicators.items():
            price = ind["price"]
            ma20 = ind["ma20"]
            rsi = ind["rsi"]
            boll_lo = ind["boll_lo"]
            boll_hi = ind["boll_hi"]
            vol_ratio = ind["vol_ratio"]

            score = 0
            reasons = []

            # ── RSI Mean Reversion (Sharpe 1.29, WR=67.6% on backtest) ──
            # LONG when RSI is low (oversold bounce)
            # SHORT when RSI is high (overbought reversal)
            if rsi < 30:
                score += 30
                reasons.append(f"RSI oversold ({rsi:.0f})")
            elif rsi > 70:
                score -= 30
                reasons.append(f"RSI overbought ({rsi:.0f})")
            elif rsi < 45:
                score += 10
                reasons.append(f"RSI bearish ({rsi:.0f})")
            elif rsi > 55:
                score -= 10
                reasons.append(f"RSI bullish ({rsi:.0f})")

            # Bollinger confirmation
            if price < boll_lo:
                score += 15
                reasons.append("below Bollinger")
            elif price > boll_hi:
                score -= 15
                reasons.append("above Bollinger")

            # MA20 momentum confirmation
            if price < ma20:
                score -= 8
                reasons.append("below MA20")
            elif price > ma20:
                score += 8
                reasons.append("above MA20")

            if score == 0:
                signals[sym] = {"action": "FLAT", "confidence": 50, "weight": 0.0, "score": score, "reasons": reasons}
                continue

            action = "LONG" if score > 0 else "SHORT"
            confidence = min(90, 50 + abs(score))
            weight = min(0.3, abs(score) / 100) * (1 if score > 0 else -1)

            signals[sym] = {
                "action": action,
                "confidence": confidence,
                "weight": round(weight, 3),
                "score": score,
                "reasons": reasons,
            }

        return signals


# ── Position Monitor ───────────────────────────────────────────────────


class PositionMonitor:
    """Monitors open positions and triggers exits based on multiple signals."""

    def __init__(self, trailing_stop_pct: float = 2.0, max_hold_minutes: int = 120):
        self.trailing_stop_pct = trailing_stop_pct
        self.max_hold_minutes = max_hold_minutes
        self._highest: Dict[str, float] = {}
        self._lowest: Dict[str, float] = {}
        self._entry_times: Dict[str, float] = {}

    def update(self, symbol: str, price: float, direction: int):
        if symbol not in self._highest:
            self._highest[symbol] = price
            self._lowest[symbol] = price
        self._highest[symbol] = max(self._highest[symbol], price)
        self._lowest[symbol] = min(self._lowest[symbol], price)

    def set_entry_time(self, symbol: str):
        self._entry_times[symbol] = time.time()

    def check_exit(
        self,
        symbol: str,
        price: float,
        direction: int,
        entry_price: float,
        llm_advice: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        # Time-based exit
        if symbol in self._entry_times:
            elapsed = (time.time() - self._entry_times[symbol]) / 60
            if elapsed > self.max_hold_minutes:
                return "max_hold_time"

        # Trailing stop
        if direction == -1:
            trail = self._lowest.get(symbol, price) * (1 + self.trailing_stop_pct / 100)
            if price >= trail:
                return "trailing_stop"
        else:
            trail = self._highest.get(symbol, price) * (1 - self.trailing_stop_pct / 100)
            if price <= trail:
                return "trailing_stop"

        # LLM advice
        if llm_advice and symbol in llm_advice:
            advice = llm_advice[symbol]
            if isinstance(advice, dict):
                if advice.get("action") == "EXIT_NOW" and advice.get("confidence", 0) >= 60:
                    return f"llm_exit: {advice.get('reasoning', '')}"
                if advice.get("action") == "EXIT_PARTIAL" and advice.get("confidence", 0) >= 70:
                    return f"llm_partial: {advice.get('reasoning', '')}"

        return None


# ── Paper trade engine (lightweight) ───────────────────────────────────


class LightweightPaperEngine:
    """Self-contained paper trading engine for the real-time loop."""

    def __init__(self, initial_capital: float, leverage: float = 3.0, commission_rate: float = 0.0005):
        self.capital = initial_capital
        self.leverage = leverage
        self.commission_rate = commission_rate
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.trade_log: List[Dict[str, Any]] = []
        self.equity_history: List[Dict[str, Any]] = []

    def open_position(
        self,
        symbol: str,
        direction: int,
        size_usd: float,
        price: float,
        sl: float,
        tp: float,
    ) -> Optional[Dict[str, Any]]:
        notional = size_usd
        margin = notional / self.leverage
        commission = notional * self.commission_rate
        if margin + commission > self.capital * 0.9:
            return None

        self.capital -= margin + commission
        size = size_usd / price

        self.positions[symbol] = {
            "direction": direction,
            "size": size,
            "entry": price,
            "margin": margin,
            "sl": sl,
            "tp": tp,
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }

        trade = {
            "time": datetime.now(timezone.utc).isoformat(),
            "sym": symbol,
            "action": "OPEN",
            "dir": "SHORT" if direction == -1 else "LONG",
            "size": size,
            "price": price,
            "size_usd": size_usd,
            "margin": margin,
            "sl": sl,
            "tp": tp,
        }
        self.trade_log.append(trade)
        return trade

    def close_position(self, symbol: str, price: float, reason: str = "signal") -> Optional[float]:
        if symbol not in self.positions:
            return None
        pos = self.positions.pop(symbol)
        pnl = (price - pos["entry"]) * pos["size"] * pos["direction"]
        commission = pos["size"] * price * self.commission_rate
        self.capital += pos["margin"] + pnl - commission

        trade = {
            "time": datetime.now(timezone.utc).isoformat(),
            "sym": symbol,
            "action": "CLOSE",
            "dir": "SHORT" if pos["direction"] == -1 else "LONG",
            "size": pos["size"],
            "entry": pos["entry"],
            "exit": price,
            "pnl": round(pnl, 4),
            "reason": reason,
        }
        self.trade_log.append(trade)
        return pnl

    def get_unrealized_pnl(self, prices: Dict[str, float]) -> Dict[str, float]:
        pnl = {}
        for sym, pos in self.positions.items():
            if sym in prices:
                pnl[sym] = (prices[sym] - pos["entry"]) * pos["size"] * pos["direction"]
        return pnl

    def get_equity(self, prices: Dict[str, float]) -> float:
        equity = self.capital
        for sym, pos in self.positions.items():
            equity += pos["margin"]
            if sym in prices:
                equity += (prices[sym] - pos["entry"]) * pos["size"] * pos["direction"]
        return equity

    def get_summary(self, prices: Dict[str, float]) -> Dict[str, Any]:
        unrealized = self.get_unrealized_pnl(prices)
        total_unrealized = sum(unrealized.values())
        realized = sum(t.get("pnl", 0) for t in self.trade_log if t["action"] == "CLOSE")
        total_trades = len([t for t in self.trade_log if t["action"] == "CLOSE"])
        wins = len([t for t in self.trade_log if t["action"] == "CLOSE" and t.get("pnl", 0) > 0])
        equity = self.get_equity(prices)

        return {
            "equity": round(equity, 2),
            "capital": round(self.capital, 2),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(total_unrealized, 2),
            "total_pnl": round(realized + total_unrealized, 2),
            "total_trades": total_trades,
            "wins": wins,
            "win_rate": round(wins / total_trades * 100, 1) if total_trades else 0,
            "open_positions": len(self.positions),
            "positions": {
                sym: {
                    "dir": "S" if p["direction"] == -1 else "L",
                    "size": p["size"],
                    "entry": p["entry"],
                    "unrealized_pnl": round(unrealized.get(sym, 0), 2),
                }
                for sym, p in self.positions.items()
            },
        }


# ── Main Real-Time Orchestrator ────────────────────────────────────────


class RealtimeAutonomousTrader:
    """Fully autonomous real-time trading agent.

    Loop (every tick_interval seconds):
      1. Fetch live prices
      2. Fetch technical indicators (every indicator_interval ticks)
      3. Generate entry signals
      4. Check open positions for exits (technical + LLM)
      5. Execute new entries
      6. Log state + PnL
    """

    def __init__(self, config: Dict[str, Any]):
        self.symbols = config.get("symbols", ["BTC-USDT", "ETH-USDT", "SOL-USDT"])
        self.tick_interval = config.get("tick_interval_seconds", 30)
        self.indicator_interval = config.get("indicator_interval_ticks", 2)
        self.initial_cash = config.get("initial_cash", 10000.0)
        self.leverage = config.get("leverage", 3.0)
        self.max_positions = config.get("max_positions", 3)
        self.min_confidence = config.get("min_confidence", 40)
        self.run_dir = config.get("run_dir", "./paper_runs")
        self.wallet_address = config.get("wallet_address", "")
        self.live_mode = config.get("live_mode", False)
        # Aggressive scalping params
        self.tp_pct = config.get("tp_pct", 0.02)
        self.sl_pct = config.get("sl_pct", 0.01)
        self.position_pct = config.get("position_pct", 0.50)

        # Components
        self.fetcher = RealtimeFetcher(self.symbols)
        self.signals = TechnicalSignals(self.symbols)
        self.monitor = PositionMonitor(
            trailing_stop_pct=config.get("trailing_stop_pct", 2.0),
            max_hold_minutes=config.get("max_hold_minutes", 120),
        )
        self.engine = LightweightPaperEngine(
            initial_capital=self.initial_cash,
            leverage=self.leverage,
        )

        # Live executor (on-chain swaps via QuickSwap on Polygon or GMX v2 on Arbitrum)
        self.live_executor: Optional[Any] = None
        self.executor_chain: str = ""
        if self.live_mode:
            chain = config.get("chain", os.environ.get("CHAIN", "polygon")).lower()
            try:
                if chain == "ethereal":
                    from paper_trading.ethereal_client import EtherealClient

                    self.live_executor = EtherealClient(
                        private_key=os.environ.get("PRIVATE_KEY", ""),
                    )
                    self.executor_chain = "ethereal"
                    if self.live_executor.initialize():
                        logger.info(
                            "LIVE MODE (Ethereal): executor ready | %s | products: %d",
                            self.live_executor.address,
                            len(self.live_executor.products),
                        )
                    else:
                        logger.warning(
                            "LIVE MODE requested but Ethereal executor not ready — falling back to paper mode"
                        )
                        self.live_mode = False
                elif chain in ("binance", "bybit", "okx", "gate", "bitget"):
                    from paper_trading.cex_perp_executor import CexPerpExecutor

                    self.live_executor = CexPerpExecutor(
                        exchange_name=chain,
                        api_key=os.environ.get("CEX_API_KEY", ""),
                        secret=os.environ.get("CEX_SECRET", ""),
                        password=os.environ.get("CEX_PASSWORD", ""),
                        run_dir=self.run_dir,
                        sandbox=config.get("sandbox", os.environ.get("CEX_SANDBOX", "false").lower() == "true"),
                    )
                    self.executor_chain = chain
                    if self.live_executor.is_ready:
                        balance = self.live_executor.get_balance()
                        logger.info(
                            "LIVE MODE (%s): executor ready | USDT total: %.2f | free: %.2f",
                            chain.upper(),
                            balance.get("total", 0),
                            balance.get("free", 0),
                        )
                    else:
                        logger.warning(
                            "LIVE MODE requested but %s executor not ready — falling back to paper mode", chain.upper()
                        )
                        self.live_mode = False
                elif chain == "hyperliquid":
                    from paper_trading.hyperliquid_executor import HyperliquidExecutor

                    self.live_executor = HyperliquidExecutor(
                        private_key=os.environ.get("PRIVATE_KEY", ""),
                        run_dir=self.run_dir,
                        testnet=config.get("testnet", os.environ.get("HYPERLIQUID_TESTNET", "false").lower() == "true"),
                    )
                    self.executor_chain = "hyperliquid"
                    if self.live_executor.is_ready:
                        balance = self.live_executor.get_balance()
                        logger.info(
                            "LIVE MODE (Hyperliquid): executor ready | %s | balance: $%.2f | free: $%.2f",
                            self.live_executor._exchange_address,
                            balance.get("total", 0),
                            balance.get("free", 0),
                        )
                    else:
                        logger.warning(
                            "LIVE MODE requested but Hyperliquid executor not ready — falling back to paper mode"
                        )
                        self.live_mode = False
                elif chain == "arbitrum":
                    from paper_trading.gmx_v2_executor import GmxV2Executor

                    self.live_executor = GmxV2Executor(
                        private_key=os.environ.get("PRIVATE_KEY", ""),
                        rpc_url=os.environ.get("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc"),
                        run_dir=self.run_dir,
                        slippage_pct=config.get("slippage_pct", 3.0),
                    )
                    self.executor_chain = "arbitrum"
                    if self.live_executor.is_ready:
                        logger.info(
                            "LIVE MODE (GMX v2): executor ready | %s | USDC: %.4f | ETH: %.6f",
                            self.live_executor.address,
                            self.live_executor.get_usdc_balance(),
                            self.live_executor.get_eth_balance(),
                        )
                    else:
                        logger.warning("LIVE MODE requested but GMX v2 executor not ready — falling back to paper mode")
                        self.live_mode = False
                else:
                    from paper_trading.live_onchain_executor import LiveOnChainExecutor

                    self.live_executor = LiveOnChainExecutor(
                        private_key=os.environ.get("PRIVATE_KEY", ""),
                        rpc_url=os.environ.get("POLYGON_RPC_URL", "https://1rpc.io/matic"),
                        run_dir=self.run_dir,
                        slippage_pct=config.get("slippage_pct", 3.0),
                    )
                    self.executor_chain = "polygon"
                    if self.live_executor.is_ready:
                        logger.info(
                            "LIVE MODE: executor ready | %s | USDC: %.4f | MATIC: %.4f",
                            self.live_executor.address,
                            self.live_executor.get_usdc_balance(),
                            self.live_executor.get_matic_balance(),
                        )
                    else:
                        logger.warning("LIVE MODE requested but executor not ready — falling back to paper mode")
                        self.live_mode = False
            except Exception as exc:
                logger.error("Failed to init live executor: %s — falling back to paper mode", exc)
                self.live_mode = False

        # State
        self._running = False
        self._tick_count = 0
        self._last_indicator_fetch = 0
        self._current_indicators: Dict[str, Dict[str, Any]] = {}
        self._cooldown_until: Dict[str, int] = {}
        self._cooldown_ticks = 12
        self._live_positions: Dict[str, Dict[str, Any]] = {}

        # Multi-agent system (technical-only, no LLM)
        self._state = MarketState(
            target_equity=config.get("goal", 1000.0),
            hard_stop=config.get("hard_stop", 14.0),
        )
        self._market_analyst = MarketAnalyst(symbols=self.symbols)
        self._portfolio_manager = PortfolioManager(
            target_equity=config.get("goal", 1000.0),
            hard_stop=config.get("hard_stop", 14.0),
        )
        self._trade_strategist = TradeStrategist()
        self._execution_agent = ExecutionAgent(
            engine=self.engine,
            live_executor=self.live_executor,
            live_mode=self.live_mode,
        )
        self._trade_auditor = TradeAuditor(None)

        Path(self.run_dir).mkdir(parents=True, exist_ok=True)

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        mode = "LIVE" if self.live_mode else "PAPER"
        logger.info(
            "RealtimeTrader: %s | %ds ticks | $%.2f | %dx | max %d positions | %s MODE",
            ", ".join(self.symbols),
            self.tick_interval,
            self.initial_cash,
            self.leverage,
            self.max_positions,
            mode,
        )

    def _signal_handler(self, signum, frame):
        logger.info("Shutdown signal received")
        self._running = False

    def run(self):
        self._running = True
        logger.info("=" * 70)
        logger.info("REAL-TIME AUTONOMOUS TRADER STARTING")
        logger.info("Symbols: %s", self.symbols)
        logger.info("Tick interval: %ds", self.tick_interval)
        logger.info("Capital: $%.2f | Leverage: %dx", self.initial_cash, self.leverage)
        logger.info("=" * 70)

        while self._running:
            try:
                self._tick()
            except Exception as exc:
                logger.error("Tick failed: %s", exc, exc_info=True)

            if self._running:
                time.sleep(self.tick_interval)

        logger.info("Trader stopped after %d ticks", self._tick_count)
        self._print_final_summary()

    def run_once(self):
        self._tick()

    def _tick(self):
        self._tick_count += 1

        # 1. Fetch live prices
        prices = self.fetcher.fetch_prices()
        if not prices:
            return

        # 2. Fetch indicators periodically (always fetch on first tick)
        if self._tick_count <= 1 or self._tick_count % self.indicator_interval == 0:
            self._current_indicators = self.signals.fetch_indicators(self.symbols)

        # 3. Update position monitors
        for sym, pos in list(self.engine.positions.items()):
            if sym in prices:
                self.monitor.update(sym, prices[sym], pos["direction"])

        # 4. Check exits — only close at profit
        for sym in list(self.engine.positions.keys()):
            if sym not in prices:
                continue
            pos = self.engine.positions[sym]
            price = prices[sym]

            # Calculate unrealized PnL
            if pos["direction"] > 0:
                pnl = (price - pos["entry"]) * pos["size"]
            else:
                pnl = (pos["entry"] - price) * pos["size"]

            # Hard TP (always profitable)
            if pos["direction"] == -1:
                if price <= pos["tp"]:
                    self.engine.close_position(sym, price, "take_profit")
                    self._cooldown_until[sym] = self._tick_count + self._cooldown_ticks
                    logger.info("TP: %s @ %.4f pnl=$%.2f", sym, price, pnl)
                    continue
            else:
                if price >= pos["tp"]:
                    self.engine.close_position(sym, price, "take_profit")
                    self._cooldown_until[sym] = self._tick_count + self._cooldown_ticks
                    logger.info("TP: %s @ %.4f pnl=$%.2f", sym, price, pnl)
                    continue

            # Monitor-based exits — only if profitable
            exit_reason = self.monitor.check_exit(
                sym,
                price,
                pos["direction"],
                pos["entry"],
                None,
            )
            if exit_reason and pnl > 0:
                self.engine.close_position(sym, price, exit_reason)
                logger.info("EXIT_PROFIT: %s @ %.4f | PnL: $%.2f | %s", sym, price, pnl, exit_reason)
            elif exit_reason and pnl <= 0:
                logger.info("BLOCK EXIT: %s at loss ($%.2f), waiting for breakeven+", sym, pnl)

        # ══════════════════════════════════════════════════════════════════
        # MULTI-AGENT ORCHESTRATION (technical-only)
        # ══════════════════════════════════════════════════════════════════

        # Update MarketState (single source of truth)
        equity = self.engine.get_equity(prices)
        self._state.tick = self._tick_count
        self._state.prices = prices
        self._state.equity = equity
        self._state.positions = self.engine.positions
        self._state.trade_log = self.engine.trade_log

        # Agent 1: Market Analyst — compute indicators + technical signals
        self._market_analyst.analyze(self._state, self.fetcher, self.signals)

        # Agent 2: Portfolio Manager — set risk policy (every 2 ticks)
        if self._tick_count % 2 == 0:
            policy = self._portfolio_manager.manage(self._state)
            if self._tick_count <= 2 or self._tick_count % 20 == 0:
                logger.info(
                    "Portfolio policy: mode=%s max_pos=%d pos_pct=%.0f%% bias=%s | %s",
                    policy.mode,
                    policy.max_positions,
                    policy.position_pct * 100,
                    policy.direction_bias,
                    policy.reasoning,
                )

        # Agent 3: Trade Strategist — reconcile all inputs, make final decision
        decisions = self._trade_strategist.decide(self._state)
        if self._tick_count <= 4 or self._tick_count % 10 == 0:
            logger.info(
                "Tick %d | Decisions: %s",
                self._tick_count,
                {s: f"{d.action} conf={d.confidence} ({d.source}) [{d.reasoning[:60]}]" for s, d in decisions.items()},
            )

        # Agent 4: Execution Agent — carry out decisions
        executed = self._execution_agent.execute(self._state)
        for trade in executed:
            sym = trade.get("sym", "")
            self.monitor.set_entry_time(sym)

        # Agent 5: Trade Auditor — review outcomes
        self._trade_auditor.audit(self._state, executed)

        # 8. Log state
        summary = self.engine.get_summary(prices)
        self._log_tick(summary)

        # Print summary every 10 ticks
        if self._tick_count % 10 == 0:
            self._print_tick_summary(summary)

    def _execute_live_trade(
        self,
        symbol: str,
        direction: int,
        size_usd: float,
        price: float,
    ):
        """Execute a live trade on the configured chain.

        - Ethereal: Perp futures via REST API + EIP-712
        - Arbitrum: GMX v2 perpetual swaps
        - Polygon: QuickSwap DEX spot swaps
        """
        if not self.live_executor:
            return

        # ── Ethereal perps ──
        if self.executor_chain == "ethereal":
            ticker = symbol.replace("-", "")
            side = "BUY" if direction == 1 else "SELL"

            product = self.live_executor.get_product(ticker)
            if not product:
                logger.warning("LIVE ETHEREAL: %s not found on Ethereal", symbol)
                return

            min_qty = float(product.get("minQuantity", 0))
            lot_size = float(product.get("lotSize", 1))
            quantity = size_usd / price
            quantity = round(quantity / lot_size) * lot_size
            if quantity < min_qty:
                logger.warning(
                    "LIVE ETHEREAL: %s quantity %.6f < min %.6f",
                    symbol,
                    quantity,
                    min_qty,
                )
                return

            quantity_str = f"{quantity:.6f}".rstrip("0").rstrip(".")
            logger.info(
                "LIVE ETHEREAL: %s %s %s @ %.2f (qty: %s)",
                symbol,
                side,
                ticker,
                price,
                quantity_str,
            )

            result = self.live_executor.place_market_order(
                ticker=ticker,
                side=side,
                quantity=quantity_str,
            )

            if result and "error" not in result:
                order_id = result.get("orderId", result.get("id", "unknown"))
                logger.info("LIVE ETHEREAL CONFIRMED: order %s", order_id)
                self._live_positions[symbol] = {
                    "order_id": order_id,
                    "direction": direction,
                    "size_usd": size_usd,
                    "price": price,
                    "timestamp": result.get("timestamp", datetime.now(timezone.utc).isoformat()),
                }
            else:
                logger.error("LIVE ETHEREAL FAILED: %s", result.get("error", "unknown") if result else "no result")
            return

        # ── CEX perps (Binance, Bybit, OKX, etc.) ──
        if self.executor_chain in ("binance", "bybit", "okx", "gate", "bitget"):
            cex_symbol = symbol.replace("-", "/")
            side = "buy" if direction == 1 else "sell"

            balance = self.live_executor.get_balance()
            if balance.get("free", 0) < size_usd * 1.05:
                logger.warning(
                    "LIVE %s: insufficient USDT (%.2f < %.2f needed)",
                    self.executor_chain.upper(),
                    balance.get("free", 0),
                    size_usd,
                )
                return

            logger.info(
                "LIVE %s: %s %s $%.2f @ %.2f",
                self.executor_chain.upper(),
                cex_symbol,
                side.upper(),
                size_usd,
                price,
            )

            result = self.live_executor.open_position(
                symbol=cex_symbol,
                direction=side,
                size_usd=size_usd,
                price=price,
                leverage=self.leverage,
            )

            if result and result.get("success"):
                logger.info(
                    "LIVE %s CONFIRMED: order %s | amount %.6f @ %.2f",
                    self.executor_chain.upper(),
                    result["order_id"],
                    result.get("amount", 0),
                    result.get("price", 0),
                )
                self._live_positions[symbol] = {
                    "order_id": result["order_id"],
                    "direction": direction,
                    "size_usd": size_usd,
                    "price": result.get("price", price),
                    "timestamp": result["timestamp"],
                }
            else:
                logger.error(
                    "LIVE %s FAILED: %s",
                    self.executor_chain.upper(),
                    result.get("error", "unknown") if result else "no result",
                )
            return

        # ── Hyperliquid perps ──
        if self.executor_chain == "hyperliquid":
            coin = symbol.split("-")[0]
            side = "Buy" if direction == 1 else "Sell"

            balance = self.live_executor.get_balance()
            if balance.get("free", 0) < size_usd * 1.05:
                logger.warning(
                    "LIVE Hyperliquid: insufficient USDC (%.2f < %.2f needed)",
                    balance.get("free", 0),
                    size_usd,
                )
                return

            logger.info(
                "LIVE Hyperliquid: %s %s $%.2f @ %.2f",
                coin,
                side,
                size_usd,
                price,
            )

            self.live_executor.set_leverage(coin, self.leverage)

            result = self.live_executor.open_position(
                coin=coin,
                direction=side,
                size_usd=size_usd,
                price=price,
                leverage=self.leverage,
            )

            if result and result.get("success"):
                logger.info(
                    "LIVE Hyperliquid CONFIRMED: %s %s %.4f @ %.2f",
                    coin,
                    side,
                    result.get("amount", 0),
                    result.get("price", 0),
                )
                self._live_positions[symbol] = {
                    "direction": direction,
                    "size_usd": size_usd,
                    "price": result.get("price", price),
                    "timestamp": result["timestamp"],
                }
            else:
                logger.error("LIVE Hyperliquid FAILED: %s", result.get("error", "unknown") if result else "no result")
            return

        if not self.live_executor.is_ready:
            return

        # ── GMX v2 on Arbitrum ──
        if self.executor_chain == "arbitrum":
            balance = self.live_executor.get_usdc_balance()
            if balance < size_usd * 1.05:
                logger.warning(
                    "LIVE GMX: insufficient USDC (%.4f < %.4f needed)",
                    balance,
                    size_usd,
                )
                return

            logger.info(
                "LIVE GMX: %s %s $%.2f @ %.2f",
                symbol,
                "LONG" if direction == 1 else "SHORT",
                size_usd,
                price,
            )

            result = self.live_executor.open_position(symbol, direction, size_usd, price)

            if result and result.get("success"):
                logger.info(
                    "LIVE GMX CONFIRMED: TX %s | block %d | gas %d",
                    result["tx_hash"][:16],
                    result.get("block_number", 0),
                    result.get("gas_used", 0),
                )
                self._live_positions[symbol] = {
                    "tx_hash": result["tx_hash"],
                    "direction": direction,
                    "size_usd": size_usd,
                    "price": price,
                    "timestamp": result["timestamp"],
                }
            else:
                logger.error("LIVE GMX FAILED: %s", result.get("error", "unknown") if result else "no result")
            return

        # ── QuickSwap on Polygon (legacy) ──
        token_map = {
            "ETH-USDT": ("USDC_POLYGON", "WETH_POLYGON"),
            "BTC-USDT": ("USDC_POLYGON", "WBTC_POLYGON"),
            "SOL-USDT": ("USDC_POLYGON", "SOL_POLYGON"),
        }

        if symbol not in token_map:
            logger.warning("Live trade: %s not mapped to Polygon tokens", symbol)
            return

        from paper_trading.live_onchain_executor import (
            USDC_POLYGON,
            WETH_POLYGON,
        )

        if direction == 1:
            token_in = USDC_POLYGON
            token_out = WETH_POLYGON
            decimals_in = 6
        else:
            token_in = WETH_POLYGON
            token_out = USDC_POLYGON
            decimals_in = 18

        if direction == 1:
            balance = self.live_executor.get_usdc_balance()
            if balance < size_usd * 1.05:
                logger.warning(
                    "Live trade: insufficient USDC (%.4f < %.4f needed)",
                    balance,
                    size_usd,
                )
                return
        else:
            balance = self.live_executor.get_weth_balance()
            if balance < size_usd / price * 1.05:
                logger.warning(
                    "Live trade: insufficient WETH for short — need to bridge first",
                )
                return

        logger.info(
            "LIVE SWAP: %s %s %.2f USDC @ %.2f",
            symbol,
            "BUY" if direction == 1 else "SELL",
            size_usd,
            price,
        )

        result = self.live_executor.swap_tokens(
            token_in=token_in,
            token_out=token_out,
            amount_in=size_usd if direction == 1 else size_usd / price,
            decimals_in=decimals_in,
        )

        if result and result.get("success"):
            logger.info(
                "LIVE CONFIRMED: TX %s | block %d | gas %d | balance_after: %.6f",
                result["tx_hash"][:16],
                result.get("block_number", 0),
                result.get("gas_used", 0),
                result.get("balance_after", 0),
            )
            self._live_positions[symbol] = {
                "tx_hash": result["tx_hash"],
                "direction": direction,
                "size_usd": size_usd,
                "price": price,
                "timestamp": result["timestamp"],
            }
        else:
            logger.error("LIVE SWAP FAILED: %s", result.get("tx_hash", "unknown") if result else "no result")

    def _log_tick(self, summary: Dict[str, Any]):
        record = {
            "tick": self._tick_count,
            "time": datetime.now(timezone.utc).isoformat(),
            **summary,
        }
        with open(Path(self.run_dir) / "equity.jsonl", "a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def _print_tick_summary(self, summary: Dict[str, Any]):
        logger.info(
            "Tick %d | Equity: $%.2f | PnL: $%.2f | Trades: %d | Wins: %d (%.1f%%) | Open: %d",
            self._tick_count,
            summary["equity"],
            summary["total_pnl"],
            summary["total_trades"],
            summary["wins"],
            summary["win_rate"],
            summary["open_positions"],
        )
        for sym, pos in summary.get("positions", {}).items():
            logger.info(
                "  %s [%s] size=%.4f entry=$%.2f unrealized=$%.2f",
                sym,
                pos["dir"],
                pos["size"],
                pos["entry"],
                pos["unrealized_pnl"],
            )

    def _print_final_summary(self):
        prices = self.fetcher.fetch_prices()
        summary = self.engine.get_summary(prices)
        logger.info("=" * 70)
        logger.info("FINAL SUMMARY — %d ticks", self._tick_count)
        logger.info("Equity: $%.2f", summary["equity"])
        logger.info("Realized PnL: $%.2f", summary["realized_pnl"])
        logger.info("Unrealized PnL: $%.2f", summary["unrealized_pnl"])
        logger.info("Total PnL: $%.2f", summary["total_pnl"])
        logger.info("Total trades: %d | Win rate: %.1f%%", summary["total_trades"], summary["win_rate"])
        logger.info("=" * 70)


# ── CLI entry point ────────────────────────────────────────────────────


def create_default_config() -> Dict[str, Any]:
    return {
        "wallet_address": os.environ.get("WALLET_ADDRESS", ""),
        "symbols": ["BTC-USDT", "ETH-USDT", "SOL-USDT"],
        "tick_interval_seconds": int(os.environ.get("TICK_INTERVAL", "15")),
        "indicator_interval_ticks": 1,
        "initial_cash": float(os.environ.get("INITIAL_CASH", "10000")),
        "leverage": int(os.environ.get("LEVERAGE", "5")),
        "max_positions": 3,
        "min_confidence": int(os.environ.get("MIN_CONFIDENCE", "40")),
        "trailing_stop_pct": float(os.environ.get("TRAILING_STOP", "1.5")),
        "max_hold_minutes": int(os.environ.get("MAX_HOLD_MIN", "60")),
        "tp_pct": float(os.environ.get("TP_PCT", "0.02")),
        "sl_pct": float(os.environ.get("SL_PCT", "0.01")),
        "position_pct": float(os.environ.get("POSITION_PCT", "0.50")),
        "run_dir": os.environ.get("RUN_DIR", "./paper_runs"),
        "chain": os.environ.get("CHAIN", "polygon").lower(),
        "live_mode": os.environ.get("LIVE_MODE", "false").lower() == "true",
        "testnet": os.environ.get("HYPERLIQUID_TESTNET", "false").lower() == "true",
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    config = create_default_config()

    # Override from env if set
    symbols_env = os.environ.get("SYMBOLS")
    if symbols_env:
        config["symbols"] = [s.strip() for s in symbols_env.split(",")]

    trader = RealtimeAutonomousTrader(config)
    trader.run()
