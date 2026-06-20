"""Autonomous multi-agent trading orchestrator.

Inspired by atlas-gic architecture: sub-agents assess market data,
portfolio state, and risk before executing trades in a continuous loop.

Agent roles:
  - Market Analyst: fetches data, computes indicators, assesses regime
  - Signal Generator: LLM-driven signal generation with full context
  - Risk Assessor: validates signals against risk constraints
  - Executor: manages order placement, stop-loss, take-profit, trailing stops
  - Portfolio Manager: tracks PnL, rebalancing, performance attribution

State is persisted to JSON files for inspection and recovery.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from paper_trading.wallet_connector import WalletConnector
from paper_trading.paper_engine import PaperTradingEngine
from paper_trading.risk_manager import PaperRiskManager
from paper_trading.order_manager import OrderManager
from paper_trading.llm_signal_engine import LLMSignalEngine
from paper_trading.agent_scorecard import AgentScorecard
from paper_trading.darwinian_weights import DarwinianWeightManager
from paper_trading.cio_agent import CIOAgent
from paper_trading.autoresearch import AutoresearchEngine
from paper_trading.cohort_regime import CohortRegimeDetector
from paper_trading.reflexivity_engine import ReflexivityEngine
from backtest.loaders.ccxt_loader import DataLoader as CCXTLoader

logger = logging.getLogger(__name__)


class MarketAnalyst:
    """Fetches market data and computes technical indicators."""

    def __init__(self, symbols: List[str], interval: str = "1H", lookback_days: int = 7):
        self.symbols = symbols
        self.interval = interval
        self.lookback_days = lookback_days
        self.loader = CCXTLoader()
        self._prices: Dict[str, float] = {}

    def fetch_data(self) -> Dict[str, pd.DataFrame]:
        end = datetime.utcnow()
        start = end - timedelta(days=self.lookback_days)
        return self.loader.fetch(
            self.symbols,
            start.strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d"),
            interval=self.interval,
        )

    def fetch_prices(self) -> Dict[str, float]:
        prices = {}
        exchange = self.loader._get_exchange()
        for sym in self.symbols:
            try:
                ticker = exchange.fetch_ticker(sym.replace("-", "/").upper())
                prices[sym] = ticker["last"]
            except Exception as exc:
                logger.warning("Price fetch failed for %s: %s", sym, exc)
                if sym in self._prices:
                    prices[sym] = self._prices[sym]
        self._prices.update(prices)
        return prices

    def compute_indicators(self, data_map: Dict[str, pd.DataFrame]) -> Dict[str, Dict[str, Any]]:
        indicators = {}
        for sym, df in data_map.items():
            if len(df) < 20:
                continue
            close = df["close"]
            current = close.iloc[-1]
            ma10 = close.tail(10).mean()
            ma20 = close.tail(20).mean()
            ma50 = close.tail(50).mean() if len(close) >= 50 else current
            rsi = self._calc_rsi(close, 14)
            std = close.tail(20).std()
            boll_upper = close.tail(20).mean() + 2 * std
            boll_lower = close.tail(20).mean() - 2 * std
            volume = df["volume"].tail(20)
            vol_ratio = volume.iloc[-1] / volume.mean() if volume.mean() > 0 else 1.0
            atr = self._calc_atr(df.tail(14))
            change_24h = (current - close.iloc[-24]) / close.iloc[-24] * 100 if len(close) >= 24 else 0

            indicators[sym] = {
                "price": current,
                "ma10": round(ma10, 4),
                "ma20": round(ma20, 4),
                "ma50": round(ma50, 4),
                "rsi": round(rsi, 1),
                "boll_upper": round(boll_upper, 4),
                "boll_lower": round(boll_lower, 4),
                "atr": round(atr, 4),
                "volume_ratio": round(vol_ratio, 2),
                "change_24h_pct": round(change_24h, 2),
                "price_vs_ma20_pct": round((current - ma20) / ma20 * 100, 2),
            }
        return indicators

    def assess_regime(self, indicators: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        avg_rsi = sum(i["rsi"] for i in indicators.values()) / max(len(indicators), 1)
        avg_change = sum(i["change_24h_pct"] for i in indicators.values()) / max(len(indicators), 1)
        avg_vol_ratio = sum(i["volume_ratio"] for i in indicators.values()) / max(len(indicators), 1)

        if avg_rsi > 70 and avg_change > 3:
            regime = "OVERBOUGHT_BULLISH"
            risk_level = "high"
        elif avg_rsi < 30 and avg_change < -3:
            regime = "OVERSOLD_BEARISH"
            risk_level = "high"
        elif avg_rsi > 60 and avg_change > 1:
            regime = "BULLISH"
            risk_level = "medium"
        elif avg_rsi < 40 and avg_change < -1:
            regime = "BEARISH"
            risk_level = "medium"
        else:
            regime = "NEUTRAL"
            risk_level = "low"

        if avg_vol_ratio > 2.0:
            regime += "_HIGH_VOL"

        return {
            "regime": regime,
            "risk_level": risk_level,
            "avg_rsi": round(avg_rsi, 1),
            "avg_24h_change": round(avg_change, 2),
            "avg_volume_ratio": round(avg_vol_ratio, 2),
        }

    @staticmethod
    def _calc_rsi(series: pd.Series, period: int = 14) -> float:
        if len(series) < period + 1:
            return 50.0
        delta = series.diff()
        gain = delta.where(delta > 0, 0).tail(period).mean()
        loss = (-delta.where(delta < 0, 0)).tail(period).mean()
        if loss == 0:
            return 100.0
        return 100 - (100 / (1 + gain / loss))

    @staticmethod
    def _calc_atr(df: pd.DataFrame, period: int = 14) -> float:
        if len(df) < period:
            return 0.0
        high_low = df["high"] - df["low"]
        return high_low.tail(period).mean()


class SignalGenerator:
    """LLM-driven signal generation with full market context."""

    def __init__(self, llm_engine: LLMSignalEngine):
        self.engine = llm_engine

    def generate(
        self,
        data_map: Dict[str, pd.DataFrame],
        indicators: Dict[str, Dict[str, Any]],
        regime: Dict[str, Any],
        equity: float,
        positions: Dict[str, Any],
        order_manager: OrderManager,
    ) -> Dict[str, Any]:
        """Generate signals using technical analysis only (no LLM)."""
        return self.engine._fallback_tech_signals(data_map)


class RiskAssessor:
    """Validates signals against risk constraints."""

    def __init__(self, risk_manager: PaperRiskManager, max_positions: int = 3):
        self.risk_manager = risk_manager
        self.max_positions = max_positions

    def assess(
        self,
        signals: Dict[str, Any],
        equity: float,
        prices: Dict[str, float],
        positions: Dict[str, Any],
        regime: Dict[str, Any],
    ) -> Dict[str, Dict[str, Any]]:
        approved = {}
        sig_map = signals.get("signals", signals) if isinstance(signals, dict) else {}

        for symbol, sig in sig_map.items():
            if isinstance(sig, dict):
                action = sig.get("action", "CLOSE")
                confidence = sig.get("confidence", 0)
                target_weight = sig.get("target_weight", 0.0)
                risk_score = sig.get("risk_score", 50)
                reasoning = sig.get("reasoning", "")
            else:
                continue

            if action == "CLOSE":
                approved[symbol] = {**sig, "status": "approved", "reason": "close signal"}
                continue

            if symbol not in prices:
                approved[symbol] = {**sig, "status": "rejected", "reason": "no price data"}
                continue

            price = prices[symbol]
            direction = 1 if target_weight > 0 else -1

            allowed, reason = self.risk_manager.check_trade_allowed(
                symbol, direction, target_weight, price, equity, positions
            )
            if not allowed:
                approved[symbol] = {**sig, "status": "rejected", "reason": reason}
                continue

            if len(positions) >= self.max_positions and symbol not in positions:
                approved[symbol] = {
                    **sig,
                    "status": "rejected",
                    "reason": f"max positions ({self.max_positions}) reached",
                }
                continue

            regime_risk = regime.get("risk_level", "medium")
            if regime_risk == "high" and confidence < 80:
                approved[symbol] = {
                    **sig,
                    "status": "rejected",
                    "reason": f"high regime risk, need confidence >= 80 (got {confidence})",
                }
                continue

            if risk_score > 50 and confidence < 75:
                approved[symbol] = {
                    **sig,
                    "status": "rejected",
                    "reason": f"high risk score ({risk_score}) with low confidence ({confidence})",
                }
                continue

            approved[symbol] = {**sig, "status": "approved", "reason": "passed all checks"}

        return approved


class TradeExecutor:
    """Executes trades with order management."""

    def __init__(
        self,
        paper_engine: PaperTradingEngine,
        order_manager: OrderManager,
        risk_manager: PaperRiskManager,
    ):
        self.paper_engine = paper_engine
        self.order_manager = order_manager
        self.risk_manager = risk_manager

    def execute(
        self,
        approved_signals: Dict[str, Dict[str, Any]],
        prices: Dict[str, float],
        equity: float,
    ) -> List[Dict[str, Any]]:
        executed = []

        for symbol, sig in approved_signals.items():
            if sig.get("status") != "approved":
                continue

            target_weight = sig.get("target_weight", 0.0)
            if target_weight == 0:
                self._close_position(symbol, prices.get(symbol, 0), equity)
                continue

            price = prices.get(symbol)
            if not price:
                continue

            direction = 1 if target_weight > 0 else -1
            confidence = sig.get("confidence", 50)

            size_usd = self.order_manager.calculate_position_size(equity, confidence)
            size = size_usd / price

            trade = self.paper_engine.execute_paper_trade(
                symbol=symbol,
                direction=direction,
                target_weight=target_weight,
                price=price,
                equity=equity,
            )

            if trade:
                sl_pct = 0.05
                tp_pct = 0.10
                sl = price * (1 - sl_pct) if direction > 0 else price * (1 + sl_pct)
                tp = price * (1 + tp_pct) if direction > 0 else price * (1 - tp_pct)

                self.order_manager.open_position(
                    symbol=symbol,
                    direction=direction,
                    size=trade["size"],
                    entry_price=price,
                    stop_loss=sl,
                    take_profit=tp,
                    trailing_stop_pct=3.0,
                )

                executed.append(
                    {
                        "symbol": symbol,
                        "action": trade.get("action", "open"),
                        "direction": direction,
                        "size": trade["size"],
                        "price": price,
                        "size_usd": size_usd,
                        "confidence": confidence,
                        "stop_loss": sl,
                        "take_profit": tp,
                        "reasoning": sig.get("reasoning", ""),
                    }
                )
                self.risk_manager.update_after_trade(trade.get("pnl", 0.0))

        return executed

    def check_trailing_stops(self, prices: Dict[str, float]) -> List[Dict[str, Any]]:
        closed = []
        for pos in self.order_manager.get_open_positions():
            if pos.symbol in prices:
                result = self.order_manager.check_orders(pos.symbol, prices[pos.symbol])
                if result:
                    reason, close_price = result
                    self._close_position(pos.symbol, close_price, 0)
                    self.order_manager.close_position(pos.symbol, close_price, reason=reason)
                    closed.append(
                        {
                            "symbol": pos.symbol,
                            "reason": reason,
                            "close_price": close_price,
                            "entry_price": pos.entry_price,
                        }
                    )
        return closed

    def _close_position(self, symbol: str, price: float, equity: float):
        if price and equity:
            self.paper_engine.execute_paper_trade(
                symbol=symbol,
                direction=0,
                target_weight=0.0,
                price=price,
                equity=equity,
            )


class PortfolioManager:
    """Tracks PnL, performance, and state persistence."""

    def __init__(self, state_dir: str = "./paper_runs"):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cycle_history: List[Dict[str, Any]] = []
        self._load_history()

    def _load_history(self):
        path = self.state_dir / "cycle_history.json"
        if path.exists():
            try:
                self.cycle_history = json.loads(path.read_text())
            except Exception:
                self.cycle_history = []

    def save_state(self, state: Dict[str, Any]):
        state["saved_at"] = datetime.now(timezone.utc).isoformat()
        path = self.state_dir / "state.json"
        path.write_text(json.dumps(state, indent=2, default=str))

    def log_cycle(self, cycle: Dict[str, Any]):
        self.cycle_history.append(cycle)
        if len(self.cycle_history) > 1000:
            self.cycle_history = self.cycle_history[-500:]
        path = self.state_dir / "cycle_history.json"
        path.write_text(json.dumps(self.cycle_history, indent=2, default=str))

    def get_performance(self) -> Dict[str, Any]:
        if not self.cycle_history:
            return {"cycles": 0}
        pnls = []
        for c in self.cycle_history:
            pnl = c.get("summary", {}).get("total_pnl", 0)
            if pnl:
                pnls.append(pnl)
        return {
            "total_cycles": len(self.cycle_history),
            "avg_pnl": sum(pnls) / len(pnls) if pnls else 0,
            "best_cycle": max(pnls) if pnls else 0,
            "worst_cycle": min(pnls) if pnls else 0,
        }


class AutonomousOrchestrator:
    """Main orchestrator that runs the multi-agent trading loop.

    Schedule per cycle:
      1. Market Analyst: fetch data + indicators + regime
      2. Signal Generator: LLM signals with full context
      3. Risk Assessor: validate against constraints
      4. Trade Executor: execute approved signals
      5. Check trailing stops on current prices
      6. Portfolio Manager: log state + PnL
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.symbols = config.get("symbols", ["BTC-USDT", "ETH-USDT"])
        self.interval = config.get("interval", "1H")
        self.cycle_interval = config.get("cycle_interval_seconds", 300)
        self.paper_mode = config.get("paper_mode", True)
        self.wallet_address = config.get("wallet_address", "")
        self.chain = config.get("chain", "polygon")
        self.run_dir = config.get("run_dir", "./paper_runs")

        initial_cash = config.get("initial_cash", 10000.0)
        leverage = config.get("leverage", 3.0)

        # Market Analyst
        self.analyst = MarketAnalyst(
            symbols=self.symbols,
            interval=self.interval,
            lookback_days=config.get("lookback_days", 7),
        )

        # Paper Engine
        self.paper_engine = PaperTradingEngine(
            {
                "initial_cash": initial_cash,
                "leverage": leverage,
                "wallet_address": self.wallet_address,
                "run_dir": self.run_dir,
                "maker_rate": config.get("maker_rate", 0.0002),
                "taker_rate": config.get("taker_rate", 0.0005),
                "slippage": config.get("slippage", 0.0005),
            }
        )

        # Risk Manager
        risk_cfg = config.get("risk_config", {})
        self.risk_manager = PaperRiskManager(risk_cfg)

        # Order Manager
        order_cfg = config.get("order_config", {})
        self.order_manager = OrderManager(
            default_stop_loss_pct=order_cfg.get("default_stop_loss_pct", 5.0),
            default_take_profit_pct=order_cfg.get("default_take_profit_pct", 10.0),
            default_trailing_stop_pct=order_cfg.get("default_trailing_stop_pct", 3.0),
            kelly_fraction=order_cfg.get("kelly_fraction", 0.25),
            max_position_pct=order_cfg.get("max_position_pct", 0.25),
        )

        # Technical Signal Engine (no LLM)
        llm_cfg = config.get("llm_config", {})
        self.llm_engine = LLMSignalEngine(
            min_confidence=llm_cfg.get("min_confidence", 70),
            max_risk_score=llm_cfg.get("max_risk_score", 50),
        )

        # Signal Generator
        self.signal_gen = SignalGenerator(self.llm_engine)

        # Risk Assessor
        self.risk_assessor = RiskAssessor(
            self.risk_manager,
            max_positions=config.get("max_positions", 3),
        )

        # Trade Executor
        self.executor = TradeExecutor(
            self.paper_engine,
            self.order_manager,
            self.risk_manager,
        )

        # Portfolio Manager
        self.portfolio_mgr = PortfolioManager(self.run_dir)

        # Wallet Connector (read-only)
        self.wallet = WalletConnector(self.wallet_address, chain=self.chain) if self.wallet_address else None

        # === ATLAS-GIC inspired modules ===

        # Agent Scorecard: per-agent signal tracking with Sharpe scoring
        self.scorecard = AgentScorecard(self.run_dir)

        # Darwinian Weight Manager: rolling weight adjustment (0.3-2.5)
        self.weight_manager = DarwinianWeightManager(self.run_dir)

        # CIO Agent: weighted signal aggregation + active PM rules
        cio_cfg = config.get("cio_config", {})
        self.cio = CIOAgent(
            self.run_dir,
            min_conviction=cio_cfg.get("min_conviction", 55),
            max_positions=cio_cfg.get("max_positions", 5),
        )

        # Autoresearch Engine: prompt self-modification
        ar_cfg = config.get("autoresearch_config", {})
        self.autoresearch = AutoresearchEngine(
            self.run_dir,
            prompt_dir=ar_cfg.get("prompt_dir", "./prompts"),
        )
        # Register the LLM signal engine as a trainable agent
        self.autoresearch.register_agent(
            "llm_engine",
            ar_cfg.get("initial_prompt", "You are a quantitative trading signal engine."),
        )

        # Cohort Regime Detector: emergent regime via cohort differentials
        cohort_cfg = config.get("cohort_config", {})
        self.cohort_detector = CohortRegimeDetector(self.run_dir)
        self.cohort_detector.register_cohort("short_window", lookback_hours=cohort_cfg.get("short_window_hours", 168))
        self.cohort_detector.register_cohort("long_window", lookback_hours=cohort_cfg.get("long_window_hours", 2160))

        # Reflexivity Engine: 5 Soros feedback loops + reversal detection
        self.reflexivity = ReflexivityEngine(self.run_dir)

        # State
        self._running = False
        self._cycle_count = 0

        # Signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        logger.info(
            "Orchestrator initialized: %s | %s | $%.2f | %s mode | scorecard+darwinian+cio+autoresearch+cohort+reflexivity",
            ", ".join(self.symbols),
            self.interval,
            initial_cash,
            "paper" if self.paper_mode else "live",
        )

    def _signal_handler(self, signum, frame):
        logger.info("Shutdown signal received")
        self._running = False

    def run_cycle(self) -> Dict[str, Any]:
        """Execute one full trading cycle through all agents."""
        cycle_start = datetime.now(timezone.utc)
        self._cycle_count += 1

        result = {
            "cycle": self._cycle_count,
            "timestamp": cycle_start.isoformat(),
            "agents": {},
        }

        # === AGENT 1: Market Analyst ===
        logger.info("[Cycle %d] Market Analyst: fetching data...", self._cycle_count)
        try:
            data_map = self.analyst.fetch_data()
            prices = self.analyst.fetch_prices()
            indicators = self.analyst.compute_indicators(data_map)
            regime = self.analyst.assess_regime(indicators)
            result["agents"]["market_analyst"] = {
                "symbols": list(data_map.keys()),
                "regime": regime["regime"],
                "risk_level": regime["risk_level"],
                "indicators": indicators,
            }
        except Exception as exc:
            logger.error("Market Analyst failed: %s", exc, exc_info=True)
            return {**result, "error": str(exc)}

        # === Calculate equity ===
        equity = self._calc_equity(prices)
        positions = dict(self.paper_engine.positions)

        # === AGENT 2: Signal Generator ===
        logger.info("[Cycle %d] Signal Generator: generating signals...", self._cycle_count)
        try:
            signals = self.signal_gen.generate(
                data_map,
                indicators,
                regime,
                equity,
                positions,
                self.order_manager,
            )
            result["agents"]["signal_generator"] = signals
        except Exception as exc:
            logger.error("Signal Generator failed: %s", exc, exc_info=True)
            signals = {"signals": {}}

        # === Scorecard: Log signals for tracking ===
        sig_map = signals.get("signals", signals) if isinstance(signals, dict) else {}
        for sym, sig in sig_map.items():
            if isinstance(sig, dict) and sig.get("action", "CLOSE") != "CLOSE":
                self.scorecard.log_signal(
                    agent_id="llm_engine",
                    symbol=sym,
                    direction=1 if sig.get("target_weight", 0) > 0 else -1,
                    conviction=sig.get("confidence", 50),
                    entry_price=prices.get(sym, 0),
                    reasoning=sig.get("reasoning", "")[:200],
                )
                # Cohort scoring (short window for recent signals)
                self.cohort_detector.score_signal(
                    cohort_id="short_window",
                    symbol=sym,
                    direction=1 if sig.get("target_weight", 0) > 0 else -1,
                    conviction=sig.get("confidence", 50),
                    entry_price=prices.get(sym, 0),
                )

        # === AGENT 3: Risk Assessor ===
        logger.info("[Cycle %d] Risk Assessor: evaluating signals...", self._cycle_count)
        try:
            approved = self.risk_assessor.assess(
                signals,
                equity,
                prices,
                positions,
                regime,
            )
            result["agents"]["risk_assessor"] = {
                k: {"status": v["status"], "reason": v.get("reason", "")} for k, v in approved.items()
            }
        except Exception as exc:
            logger.error("Risk Assessor failed: %s", exc, exc_info=True)
            approved = {}

        # === CIO: Weighted signal aggregation + active PM ===
        try:
            agent_weights = self.weight_manager.get_all_weights()
            # Ensure llm_engine is registered
            if "llm_engine" not in agent_weights:
                self.weight_manager.register_agent("llm_engine")
                agent_weights = self.weight_manager.get_all_weights()

            cio_actions = self.cio.synthesize(
                agent_signals={"llm_engine": signals},
                agent_weights=agent_weights,
                equity=equity,
                positions=positions,
                regime=regime,
                prices=prices,
            )
            result["agents"]["cio"] = [
                {
                    "symbol": a.symbol,
                    "action": a.action,
                    "conviction": a.conviction,
                    "size_usd": a.size_usd,
                    "agents": a.contributing_agents,
                }
                for a in cio_actions
            ]
            # Convert CIO actions into approved signals format for executor
            if cio_actions:
                cio_approved = {}
                for a in cio_actions:
                    if a.action in ("BUY", "SELL"):
                        cio_approved[a.symbol] = {
                            "action": a.action,
                            "confidence": a.conviction,
                            "target_weight": a.target_weight,
                            "reasoning": a.reasoning,
                            "status": "approved",
                            "reason": "cio_synthesis",
                        }
                approved.update(cio_approved)
        except Exception as exc:
            logger.error("CIO synthesis failed: %s", exc, exc_info=True)

        # === Reflexivity: Assess feedback loops ===
        try:
            for sym, ind in indicators.items():
                change = ind.get("change_24h_pct", 0) / 100
                self.reflexivity.assess_price_fundamentals(sym, change, f"regime={regime.get('regime', '?')}")
            # PnL behavior loop
            summary = self.paper_engine.get_summary(prices)
            total_pnl = summary.get("total_pnl", 0)
            if equity > 0:
                self.reflexivity.assess_pnl_behavior("orchestrator", total_pnl / equity, f"equity=${equity:.0f}")
            ref_signal = self.reflexivity.evaluate()
            result["agents"]["reflexivity"] = {
                "loops_active": ref_signal.loops_active,
                "loops_extreme": ref_signal.loops_extreme,
                "fragility": ref_signal.regime_fragility,
                "action": ref_signal.recommended_action,
            }
            # Apply reflexivity action to scale approved signals
            if ref_signal.recommended_action == "reduce_exposure":
                for sym in list(approved.keys()):
                    sig = approved[sym]
                    if isinstance(sig, dict):
                        sig["confidence"] = sig.get("confidence", 50) * 0.7
        except Exception as exc:
            logger.error("Reflexivity assessment failed: %s", exc, exc_info=True)

        # === AGENT 4: Trade Executor ===
        logger.info("[Cycle %d] Trade Executor: executing approved signals...", self._cycle_count)
        try:
            executed = self.executor.execute(approved, prices, equity)
            result["agents"]["trade_executor"] = executed
            for trade in executed:
                logger.info(
                    "  EXECUTED: %s %s %.4f @ %.2f (SL: %.2f, TP: %.2f)",
                    trade["symbol"],
                    "LONG" if trade["direction"] > 0 else "SHORT",
                    trade["size"],
                    trade["price"],
                    trade["stop_loss"],
                    trade["take_profit"],
                )
        except Exception as exc:
            logger.error("Trade Executor failed: %s", exc, exc_info=True)
            executed = []

        # === Check trailing stops ===
        try:
            stopped = self.executor.check_trailing_stops(prices)
            for s in stopped:
                logger.info("  STOPPED: %s at %.2f (%s)", s["symbol"], s["close_price"], s["reason"])
            result["agents"]["trailing_stops"] = stopped
        except Exception as exc:
            logger.error("Trailing stop check failed: %s", exc)

        # === Post-execution scoring ===
        try:
            # Score signal outcomes against current prices
            for sym in prices:
                self.scorecard.score_outcome(sym, prices[sym])
                self.cohort_detector.score_outcome(sym, prices[sym])

            # Update cohort weights and detect regime
            cohort_weights = self.cohort_detector.update_weights()
            cohort_regime = self.cohort_detector.detect_regime()
            result["cohort_regime"] = {
                "regime": cohort_regime,
                "weights": cohort_weights,
            }

            # Update Darwinian weights periodically (every 10 cycles)
            if self._cycle_count % 10 == 0:
                metrics = self.scorecard.get_all_agent_metrics()
                weight_updates = self.weight_manager.update_weights(metrics)
                if weight_updates:
                    result["darwinian_updates"] = weight_updates

            # Autoresearch: check for prompt improvements (every 20 cycles)
            if self._cycle_count % 20 == 0:
                ar_cfg = self.config.get("autoresearch_config", {})
                if ar_cfg.get("enabled", True):
                    metrics = self.scorecard.get_all_agent_metrics()
                    worst = self.autoresearch.identify_worst_agent(
                        metrics,
                        min_signals=ar_cfg.get("min_signals", 3),
                    )
                    if worst:
                        mod = self.autoresearch.generate_modification(worst, metrics)
                        if mod:
                            self.autoresearch.apply_modification(mod.mod_id)
                            result["autoresearch"] = {
                                "agent": worst,
                                "modification": mod.modification[:150],
                                "mod_id": mod.mod_id,
                            }

                    # Advance any active tests
                    for mod in self.autoresearch.get_active_modifications():
                        if self.autoresearch.advance_test(mod.mod_id):
                            agent_metrics = self.scorecard.get_all_agent_metrics()
                            new_sharpe = agent_metrics.get(mod.agent_id, {}).get("sharpe", 0)
                            outcome = self.autoresearch.evaluate_modification(mod.mod_id, new_sharpe)
                            result["autoresearch_evaluated"] = {
                                "mod_id": mod.mod_id,
                                "outcome": outcome,
                                "pre_sharpe": mod.pre_sharpe,
                                "post_sharpe": new_sharpe,
                            }

            # Log weight leaderboard
            result["weight_leaderboard"] = self.weight_manager.get_leaderboard()
        except Exception as exc:
            logger.error("Post-execution scoring failed: %s", exc, exc_info=True)

        # === AGENT 5: Portfolio Manager ===
        summary = self.paper_engine.get_summary(prices)
        self.paper_engine.log_equity_snapshot(current_prices=prices)

        wallet_info = {}
        if self.wallet:
            try:
                wallet_info = self.wallet.get_all_balances()
            except Exception:
                pass

        result["agents"]["portfolio_manager"] = summary
        result["wallet_balances"] = wallet_info
        result["risk_status"] = self.risk_manager.get_status()

        # Log cycle
        self.portfolio_mgr.log_cycle(result)

        # Save full state
        self.portfolio_mgr.save_state(
            {
                "cycle": self._cycle_count,
                "equity": summary.get("equity", 0),
                "total_pnl": summary.get("total_pnl", 0),
                "positions": summary.get("positions", {}),
                "regime": regime,
            }
        )

        # Print cycle summary
        self._print_cycle_summary(result, summary)

        return result

    def run(self):
        """Run the autonomous loop."""
        self._running = True
        logger.info("=" * 60)
        logger.info("AUTONOMOUS TRADING ORCHESTRATOR STARTING")
        logger.info("Symbols: %s", self.symbols)
        logger.info("Cycle interval: %ds", self.cycle_interval)
        logger.info("Mode: %s", "paper" if self.paper_mode else "live")
        logger.info("=" * 60)

        while self._running:
            try:
                self.run_cycle()
            except Exception as exc:
                logger.error("Cycle failed: %s", exc, exc_info=True)

            if self._running:
                logger.info("Sleeping %ds...", self.cycle_interval)
                sleep_end = time.time() + self.cycle_interval
                while self._running and time.time() < sleep_end:
                    time.sleep(1)

        logger.info("Orchestrator stopped after %d cycles", self._cycle_count)
        self._print_final_summary()

    def run_once(self) -> Dict[str, Any]:
        """Run a single cycle."""
        return self.run_cycle()

    def _calc_equity(self, prices: Dict[str, float]) -> float:
        equity = self.paper_engine.capital
        for sym, pos in self.paper_engine.positions.items():
            margin = self.paper_engine._calc_margin(
                sym,
                pos.size,
                pos.entry_price,
                pos.leverage,
            )
            equity += margin
            if sym in prices:
                pnl = self.paper_engine._calc_pnl(
                    sym,
                    pos.direction,
                    pos.size,
                    pos.entry_price,
                    prices[sym],
                )
                equity += pnl
        return equity

    def _print_cycle_summary(self, result: Dict[str, Any], summary: Dict[str, Any]):
        regime = result.get("agents", {}).get("market_analyst", {}).get("regime", "?")
        trades = len(result.get("agents", {}).get("trade_executor", []))
        stopped = len(result.get("agents", {}).get("trailing_stops", []))

        logger.info(
            "Cycle %d | %s | Equity: $%.2f | PnL: $%.2f | Trades: %d | Stops: %d",
            self._cycle_count,
            regime,
            summary.get("equity", 0),
            summary.get("total_pnl", 0),
            trades,
            stopped,
        )

        for sym, pos_info in summary.get("positions", {}).items():
            upnl = pos_info.get("unrealized_pnl", 0)
            direction = "L" if pos_info.get("direction", 0) > 0 else "S"
            logger.info(
                "  %s [%s] size=%.4f entry=%.2f unrealized=$%.2f",
                sym,
                direction,
                pos_info["size"],
                pos_info["entry"],
                upnl,
            )

    def _print_final_summary(self):
        perf = self.portfolio_mgr.get_performance()
        summary = self.paper_engine.get_summary()
        logger.info("=" * 60)
        logger.info("FINAL SUMMARY")
        logger.info("Cycles: %d", self._cycle_count)
        logger.info("Equity: $%.2f", summary.get("equity", 0))
        logger.info("Realized PnL: $%.2f", summary.get("realized_pnl", 0))
        logger.info("Unrealized PnL: $%.2f", summary.get("unrealized_pnl", 0))
        logger.info("Total PnL: $%.2f", summary.get("total_pnl", 0))
        logger.info("Commission: $%.4f", summary.get("total_commission", 0))
        logger.info("Win rate: %.1f%%", summary.get("win_rate", 0))
        logger.info("=" * 60)


def create_default_config() -> Dict[str, Any]:
    """Create a default configuration for the orchestrator."""
    return {
        "wallet_address": os.environ.get("WALLET_ADDRESS", ""),
        "chain": os.environ.get("CHAIN", "polygon"),
        "symbols": ["BTC-USDT", "ETH-USDT", "SOL-USDT"],
        "interval": "1H",
        "cycle_interval_seconds": 300,
        "initial_cash": 10000.0,
        "leverage": 3.0,
        "paper_mode": True,
        "max_positions": 3,
        "lookback_days": 7,
        "run_dir": "./paper_runs",
        "risk_config": {
            "max_position_size": 0.25,
            "max_total_exposure": 0.8,
            "max_leverage": 5.0,
            "daily_loss_limit": 5.0,
            "max_trades_per_day": 20,
            "min_trade_size": 10.0,
        },
        "order_config": {
            "default_stop_loss_pct": 5.0,
            "default_take_profit_pct": 10.0,
            "default_trailing_stop_pct": 3.0,
            "kelly_fraction": 0.25,
            "max_position_pct": 0.25,
        },
        "llm_config": {
            "min_confidence": 70,
            "max_risk_score": 50,
        },
        "cio_config": {
            "min_conviction": 55,
            "max_positions": 5,
            "max_gross_exposure": 1.0,
            "drawdown_delever_threshold": 0.10,
        },
        "autoresearch_config": {
            "enabled": True,
            "test_cycles": 5,
            "min_signals": 3,
            "prompt_dir": "./prompts",
            "initial_prompt": "You are a quantitative trading signal engine. Analyze market data and generate trading signals with conviction scores.",
        },
        "cohort_config": {
            "short_window_hours": 168,
            "long_window_hours": 2160,
        },
    }
