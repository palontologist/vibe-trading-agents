"""Growth Orchestrator: $10 -> $10,000 in 10 Days.

Continuous trading loop:
  1. Fetch live prices (multi-source)
  2. Compute multi-signal confluence
  3. Size positions (Kelly criterion)
  4. Execute paper trades
  5. Monitor exits (trailing stop, hard stop, time)
  6. Track progress toward target
  7. Log everything

Runs every 15-30 seconds for crypto, every 60 seconds for forex/commodities.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import requests

logger = logging.getLogger(__name__)

# Import growth modules
from paper_trading.growth.strategy import GrowthStrategy, CRYPTO_ASSETS, FOREX_ASSETS, COMMODITY_ASSETS
from paper_trading.growth.portfolio import GrowthPortfolio
from paper_trading.growth.risk import GrowthRiskManager


# ── Data Fetchers ───────────────────────────────────────────────────────

class HyperliquidDataFetcher:
    """Fetch live crypto data from Hyperliquid."""

    BASE_URL = "https://api.hyperliquid.xyz/info"

    def get_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Get current mid prices."""
        try:
            resp = requests.post(
                self.BASE_URL,
                json={"type": "allMids"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            prices = {}
            for sym in symbols:
                # Hyperliquid uses bare symbol names now (e.g. "BTC" not "BTC-USDC")
                if sym in data:
                    prices[sym] = float(data[sym])
                elif f"{sym}-USDC" in data:
                    prices[sym] = float(data[f"{sym}-USDC"])
            return prices
        except Exception as e:
            logger.warning(f"Hyperliquid price fetch failed: {e}")
            return {}

    def get_candles(self, symbol: str, interval: str = "1h", limit: int = 100) -> Optional[pd.DataFrame]:
        """Get OHLCV candles."""
        try:
            end_time = int(time.time() * 1000)
            interval_ms = {"1m": 60000, "5m": 300000, "15m": 900000, "1h": 3600000, "4h": 14400000}
            start_time = end_time - (limit * interval_ms.get(interval, 3600000))

            resp = requests.post(
                self.BASE_URL,
                json={
                    "type": "candleSnapshot",
                    "req": {
                        "coin": symbol,
                        "interval": interval,
                        "startTime": start_time,
                        "endTime": end_time,
                    }
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            if not data:
                return None

            df = pd.DataFrame(data)
            df["close"] = df["c"].astype(float)
            df["open"] = df["o"].astype(float)
            df["high"] = df["h"].astype(float)
            df["low"] = df["l"].astype(float)
            df["volume"] = df["v"].astype(float)
            df["timestamp"] = pd.to_datetime(df["t"], unit="ms")
            df = df.set_index("timestamp")

            return df[["open", "high", "low", "close", "volume"]].tail(limit)
        except Exception as e:
            logger.warning(f"Candle fetch failed for {symbol}: {e}")
            return None


class ForexDataFetcher:
    """Fetch forex data from Frankfurter API (ECB rates)."""

    BASE_URL = "https://api.frankfurter.app"

    def __init__(self):
        self._cache: Dict[str, float] = {}
        self._cache_time = 0
        self._volatility = {
            "MXN": 0.004, "BRL": 0.006, "ZAR": 0.005,
            "NZD": 0.003, "HUF": 0.004, "MYR": 0.005, "PLN": 0.003,
        }

    def get_prices(self, pairs: List[str]) -> Dict[str, float]:
        """Get current forex prices with simulated intraday moves."""
        now = time.time()

        # Refresh base rates every 5 minutes
        if now - self._cache_time > 300 or not self._cache:
            try:
                resp = requests.get(f"{self.BASE_URL}/latest?from=USD&to=MXN,BRL,ZAR,NZD,HUF,MYR,PLN", timeout=10)
                resp.raise_for_status()
                rates = resp.json().get("rates", {})
                self._cache = rates
                self._cache_time = now
            except Exception as e:
                logger.warning(f"Forex fetch failed: {e}")
                return {}

        prices = {}
        for pair in pairs:
            if pair in self._cache:
                base = self._cache[pair]
                vol = self._volatility.get(pair, 0.003)
                # Add small random walk for intraday simulation
                noise = np.random.normal(0, vol / 100)
                prices[pair] = base * (1 + noise)

        return prices


class BinanceDataFetcher:
    """Fetch commodity data from Binance via CCXT."""

    # Binance perp symbol mapping
    SYMBOL_MAP = {
        "XAU": "XAUUSDT",
        "XAG": "XAGUSDT",
        "NATGAS": "NATGASUSDT",
        "PAXG": "PAXGUSDT",
        "SPX": "1000SHEES",
        "BTC": "BTCUSDT",
        "ETH": "ETHUSDT",
        "SOL": "SOLUSDT",
        "DOGE": "DOGEUSDT",
        "WIF": "WIFUSDT",
        "JUP": "JUPUSDT",
        "RENDER": "RENDERUSDT",
        "SUI": "SUIUSDT",
        "LINK": "LINKUSDT",
        "NEAR": "NEARUSDT",
        "AAVE": "AAVEUSDT",
        "FET": "FETUSDT",
    }

    def __init__(self):
        self._exchange = None

    def _get_exchange(self):
        if self._exchange is None:
            try:
                import ccxt
                self._exchange = ccxt.binance({"enableRateLimit": True})
            except ImportError:
                logger.warning("ccxt not available for Binance data")
        return self._exchange

    def get_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Get current commodity prices."""
        exchange = self._get_exchange()
        if not exchange:
            return {}

        prices = {}
        for sym in symbols:
            binance_sym = self.SYMBOL_MAP.get(sym, f"{sym}USDT")
            try:
                ticker = exchange.fetch_ticker(f"{binance_sym}:USDT")
                prices[sym] = ticker["last"]
            except Exception:
                try:
                    ticker = exchange.fetch_ticker(f"{sym}USDT")
                    prices[sym] = ticker["last"]
                except Exception as e:
                    logger.debug(f"Binance fetch failed for {sym}: {e}")
        return prices

    def get_candles(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> Optional[pd.DataFrame]:
        """Get OHLCV candles."""
        exchange = self._get_exchange()
        if not exchange:
            return None

        binance_sym = self.SYMBOL_MAP.get(symbol, f"{symbol}USDT")
        try:
            ohlcv = exchange.fetch_ohlcv(f"{binance_sym}:USDT", timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df = df.set_index("timestamp")
            return df
        except Exception:
            try:
                ohlcv = exchange.fetch_ohlcv(f"{symbol}USDT", timeframe, limit=limit)
                df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                df = df.set_index("timestamp")
                return df
            except Exception as e:
                logger.debug(f"Candle fetch failed for {symbol}: {e}")
                return None


class FearGreedFetcher:
    """Fetch Fear & Greed Index."""

    def get_value(self) -> int:
        """Get current F&G value (0-100)."""
        try:
            resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
            resp.raise_for_status()
            data = resp.json().get("data", [{}])
            return int(data[0].get("value", 50))
        except Exception:
            return 50  # Neutral default


# ── Orchestrator ────────────────────────────────────────────────────────

class GrowthOrchestrator:
    """Main trading loop for $10 -> $10K growth target.

    Coordinates data fetching, signal generation, position management,
    and risk controls in a continuous loop.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.run_dir = Path(self.config.get("run_dir", "./paper_runs/growth"))
        self.run_dir.mkdir(parents=True, exist_ok=True)

        # Components
        self.strategy = GrowthStrategy(self.config.get("strategy", {}))
        self.portfolio = GrowthPortfolio(self.config.get("portfolio", {}))
        self.risk = GrowthRiskManager(self.config.get("risk", {}))

        # Data fetchers
        self.hyperliquid = HyperliquidDataFetcher()
        self.forex = ForexDataFetcher()
        self.binance = BinanceDataFetcher()
        self.fear_greed = FearGreedFetcher()

        # Config
        self.crypto_interval = self.config.get("crypto_interval", 15)  # seconds
        self.forex_interval = self.config.get("forex_interval", 60)
        self.commodity_interval = self.config.get("commodity_interval", 30)

        # Tracking
        self._tick_count = 0
        self._last_fg_update = 0
        self._start_time = time.time()
        self._target_days = self.config.get("target_days", 10)
        self._target_equity = self.config.get("target_equity", 10000.0)

        # Graceful shutdown
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
        self._running = True

    def _shutdown(self, signum, frame):
        logger.info("Shutdown signal received, stopping...")
        self._running = False

    def run(self) -> None:
        """Main trading loop."""
        logger.info("=" * 60)
        logger.info("GROWTH ORCHESTRATOR STARTING")
        logger.info(f"Target: ${self.portfolio.initial_cash} -> ${self._target_equity} in {self._target_days} days")
        logger.info(f"Daily compound target: {((self._target_equity/self.portfolio.initial_cash)**(1/self._target_days) - 1)*100:.1f}%")
        logger.info("=" * 60)

        self.risk.reset_daily(self.portfolio.state.equity)
        self.portfolio.reset_daily()

        while self._running:
            try:
                self._tick()
                time.sleep(self.crypto_interval)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Tick error: {e}", exc_info=True)
                time.sleep(5)

        self._save_final_state()
        logger.info("Orchestrator stopped.")

    def _tick(self) -> None:
        """Single tick of the trading loop."""
        self._tick_count += 1
        now = time.time()

        # 1. Update Fear & Greed (every 5 min)
        if now - self._last_fg_update > 300:
            fg = self.fear_greed.get_value()
            self.strategy.update_fear_greed(fg)
            self._last_fg_update = now
            logger.info(f"Fear & Greed Index: {fg}")

        # 2. Fetch prices
        crypto_prices = self.hyperliquid.get_prices(CRYPTO_ASSETS)
        forex_prices = self.forex.get_prices(FOREX_ASSETS)
        commodity_prices = self.binance.get_prices(COMMODITY_ASSETS)

        all_prices = {**crypto_prices, **forex_prices, **commodity_prices}

        if not all_prices:
            logger.warning("No prices fetched, skipping tick")
            return

        # 3. Update portfolio equity
        self.portfolio.update_equity(all_prices)

        # 4. Check exits for open positions
        self._check_exits(all_prices)

        # 5. Update position tracking for rotation signal
        for symbol, pos in self.portfolio.state.positions.items():
            age = now - pos.entry_time
            self.strategy.update_position_tracking(symbol, pos.pnl / pos.size_usd if pos.size_usd > 0 else 0, age)

        # 6. Fetch historical data for signals (every 3 ticks for crypto)
        historical_data = {}
        if self._tick_count % 3 == 0 or not hasattr(self, "_last_historical"):
            # Prioritize crypto - fetch ALL crypto assets from Hyperliquid
            for sym in crypto_prices.keys():
                candles = self.hyperliquid.get_candles(sym, "1h", 100)
                if candles is not None:
                    historical_data[sym] = candles
                else:
                    # Fallback to Binance for crypto
                    candles = self.binance.get_candles(sym, "1h", 100)
                    if candles is not None:
                        historical_data[sym] = candles

            # Also fetch commodity data
            for sym in COMMODITY_ASSETS:
                candles = self.binance.get_candles(sym, "1h", 100)
                if candles is not None:
                    historical_data[sym] = candles

            self._last_historical = now

        # 7. Generate signals
        day_progress = (now - self._start_time) / (self._target_days * 86400)
        signals = self.strategy.generate_signals(
            prices=all_prices,
            historical_data=historical_data,
            open_positions=self.portfolio.state.positions,
            equity=self.portfolio.state.equity,
            day_progress=min(1.0, day_progress),
        )

        # 8. Execute new trades
        for sig in signals:
            if sig.action in ("LONG", "SHORT") and sig.symbol not in self.portfolio.state.positions:
                self._execute_entry(sig, all_prices)

        # 9. Log state
        self._log_state(all_prices)

        # 10. Check daily reset
        self._check_daily_reset()

        # 11. Print progress
        if self._tick_count % 10 == 0:
            self._print_progress()

    def _execute_entry(self, signal, prices: Dict[str, float]) -> None:
        """Execute a new position entry."""
        price = prices.get(signal.symbol)
        if not price:
            return

        # Risk check
        notional = self.portfolio.state.equity * signal.size_pct
        risk_check = self.risk.check_pre_trade(
            symbol=signal.symbol,
            side=signal.action,
            notional=notional,
            equity=self.portfolio.state.equity,
            open_positions=self.portfolio.state.positions,
        )

        if not risk_check.allowed:
            logger.info(f"BLOCKED {signal.action} {signal.symbol}: {risk_check.reason}")
            return

        # Calculate position
        pos_info = self.portfolio.calculate_position(
            symbol=signal.symbol,
            side=signal.action,
            price=price,
            signal_confidence=signal.confidence,
            signal_size_pct=signal.size_pct,
        )

        if pos_info:
            pos = self.portfolio.open_position(pos_info)
            pos.stop_loss = signal.stop_loss
            pos.take_profit = signal.take_profit

            logger.info(
                f"SIGNAL {signal.action} {signal.symbol} "
                f"(conf={signal.confidence}%, reason={signal.reason[:80]})"
            )

    def _check_exits(self, prices: Dict[str, float]) -> None:
        """Check all open positions for exits."""
        symbols_to_close = []

        for symbol, pos in self.portfolio.state.positions.items():
            current_price = prices.get(symbol, pos.entry_price)

            risk_check = self.risk.check_position_exits(
                symbol=symbol,
                position=pos,
                current_price=current_price,
                equity=self.portfolio.state.equity,
            )

            if risk_check.action in ("CLOSE", "CLOSE_ALL"):
                symbols_to_close.append((symbol, current_price, risk_check.reason))

            # Circuit breaker = close all
            if risk_check.action == "CLOSE_ALL":
                for s, p in self.portfolio.state.positions.items():
                    if s != symbol:
                        close_price = prices.get(s, p.entry_price)
                        symbols_to_close.append((s, close_price, "Circuit breaker"))
                break

        for symbol, price, reason in symbols_to_close:
            pnl = self.portfolio.close_position(symbol, price, reason)
            if pnl is not None:
                self.risk.record_daily_loss(pnl)

    def _check_daily_reset(self) -> None:
        """Check if we need to reset daily tracking."""
        now = datetime.now(timezone.utc)
        if now.hour == 0 and now.minute == 0:
            self.risk.reset_daily(self.portfolio.state.equity)
            self.portfolio.reset_daily()
            logger.info("Daily reset complete")

    def _log_state(self, prices: Dict[str, float]) -> None:
        """Log current state to files."""
        state = {
            "tick": self._tick_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "equity": self.portfolio.state.equity,
            "cash": self.portfolio.state.cash,
            "target": self._target_equity,
            "progress_pct": (self.portfolio.state.equity - self.portfolio.initial_cash) / (self._target_equity - self.portfolio.initial_cash) * 100,
            "daily_pnl": self.portfolio.state.daily_pnl,
            "total_pnl": self.portfolio.state.total_pnl,
            "total_trades": self.portfolio.state.total_trades,
            "win_rate": self.portfolio.state.wins / max(1, self.portfolio.state.total_trades),
            "max_drawdown": self.portfolio.state.max_drawdown,
            "open_positions": len(self.portfolio.state.positions),
            "positions": {
                s: {
                    "side": p.side,
                    "entry": p.entry_price,
                    "current": prices.get(s, p.entry_price),
                    "pnl": p.pnl,
                    "age_s": time.time() - p.entry_time,
                }
                for s, p in self.portfolio.state.positions.items()
            },
            "fear_greed": self.strategy._fear_greed_history[-1] if self.strategy._fear_greed_history else 50,
            "risk": self.risk.get_risk_status(self.portfolio.state.equity),
        }

        # Append to equity log
        equity_file = self.run_dir / "equity.jsonl"
        with open(equity_file, "a") as f:
            f.write(json.dumps(state) + "\n")

        # Update state file
        state_file = self.run_dir / "state.json"
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)

    def _print_progress(self) -> None:
        """Print progress summary."""
        progress = self.portfolio.get_progress()
        elapsed_days = (time.time() - self._start_time) / 86400
        target_curve = self.portfolio.get_target_curve(self._target_days)
        expected = target_curve[min(int(elapsed_days), len(target_curve) - 1)]

        logger.info(
            f"[Tick {self._tick_count}] "
            f"Equity: ${progress['equity']:.2f} | "
            f"Target: ${expected:.2f} | "
            f"Progress: {progress['progress_pct']:.1f}% | "
            f"Trades: {progress['total_trades']} | "
            f"Win Rate: {progress['win_rate']*100:.0f}% | "
            f"Positions: {progress['open_positions']} | "
            f"Max DD: {progress['max_drawdown']*100:.1f}%"
        )

    def _save_final_state(self) -> None:
        """Save final state on shutdown."""
        progress = self.portfolio.get_progress()
        final = {
            "final_equity": progress["equity"],
            "target": self._target_equity,
            "achieved": progress["equity"] >= self._target_equity,
            "progress_pct": progress["progress_pct"],
            "multiplier": progress["multiplier"],
            "total_trades": progress["total_trades"],
            "win_rate": progress["win_rate"],
            "max_drawdown": progress["max_drawdown"],
            "total_pnl": progress["total_pnl"],
            "elapsed_seconds": time.time() - self._start_time,
        }

        with open(self.run_dir / "final_state.json", "w") as f:
            json.dump(final, f, indent=2)

        logger.info(f"Final: ${progress['equity']:.2f} ({progress['multiplier']:.1f}x)")
