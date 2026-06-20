"""Multi-Exchange Commodity & Stock Paper Trading Launcher.

Runs the multi-agent orchestrator across Hyperliquid (PAXG, SPX) and
Binance (XAU, XAG, NATGAS) commodity/stock perps with a $10 wallet
and $10/day profit target.

Usage:
    cd /home/palontologist/Downloads/dev/Vibe-Trading/agent
    python -m paper_trading.forex_paper_launcher

Environment variables:
    INITIAL_CASH=10       Starting capital ($)
    LEVERAGE=3            Leverage multiplier
    DAILY_TARGET=10       Daily profit target ($)
    TICK_INTERVAL=60      Seconds between ticks
    EXCHANGES=binance,hyperliquid  Comma-separated exchanges
    SYMBOLS=XAU/USDT:USDT,PAXG,SPX  Override default symbols
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

import pandas as pd

logger = logging.getLogger(__name__)


# ── Multi-Exchange Data Fetcher ──────────────────────────────────────────


class HyperliquidFetcher:
    """Fetch prices and OHLCV from Hyperliquid (PAXG, SPX, etc.)."""

    def __init__(self, symbols: List[str]):
        self.symbols = symbols
        self._info = None
        self._tick_history: Dict[str, List[float]] = {}
        self._max_ticks = 200

    def _get_info(self):
        if self._info is None:
            from hyperliquid.info import Info
            from hyperliquid.utils import constants
            self._info = Info(constants.MAINNET_API_URL, skip_ws=True)
        return self._info

    def fetch_prices(self) -> Dict[str, float]:
        info = self._get_info()
        mids = info.all_mids()
        prices = {}
        for sym in self.symbols:
            coin = sym.split("-")[0] if "-" in sym else sym
            if coin in mids:
                price = float(mids[coin])
                prices[sym] = price
                if sym not in self._tick_history:
                    self._tick_history[sym] = []
                self._tick_history[sym].append(price)
                if len(self._tick_history[sym]) > self._max_ticks:
                    self._tick_history[sym] = self._tick_history[sym][-self._max_ticks:]
            else:
                logger.warning("No mid price for %s on Hyperliquid", sym)
        return prices

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> pd.DataFrame:
        info = self._get_info()
        coin = symbol.split("-")[0] if "-" in symbol else symbol
        try:
            import time as _time
            now = int(_time.time() * 1000)
            start = now - limit * 3600 * 1000
            candles = info.candles_snapshot(coin, timeframe, start, now)
            if not candles:
                return pd.DataFrame()
            df = pd.DataFrame(candles)
            df["open"] = df["o"].astype(float)
            df["high"] = df["h"].astype(float)
            df["low"] = df["l"].astype(float)
            df["close"] = df["c"].astype(float)
            df["volume"] = df["v"].astype(float)
            df["trade_date"] = pd.to_datetime(df["t"], unit="ms")
            df = df.set_index("trade_date").sort_index()
            return df[["open", "high", "low", "close", "volume"]]
        except Exception as exc:
            logger.warning("Hyperliquid OHLCV failed for %s: %s", symbol, exc)
            return pd.DataFrame()

    def get_ticks(self, symbol: str, count: int = 10) -> List[float]:
        return self._tick_history.get(symbol, [])[-count:]


class CCXTDataFetcher:
    """Fetch live prices and OHLCV via CCXT (Binance perps)."""

    def __init__(self, symbols: List[str], exchange_id: str = "binance"):
        self.symbols = symbols
        self.exchange_id = exchange_id
        self._exchange = None
        self._tick_history: Dict[str, List[float]] = {}
        self._max_ticks = 200

    def _get_exchange(self):
        if self._exchange is None:
            import ccxt
            exchange_cls = getattr(ccxt, self.exchange_id)
            self._exchange = exchange_cls({"enableRateLimit": True})
        return self._exchange

    def fetch_prices(self) -> Dict[str, float]:
        exchange = self._get_exchange()
        prices = {}
        for sym in self.symbols:
            try:
                ticker = exchange.fetch_ticker(sym)
                price = float(ticker["last"])
                prices[sym] = price
                if sym not in self._tick_history:
                    self._tick_history[sym] = []
                self._tick_history[sym].append(price)
                if len(self._tick_history[sym]) > self._max_ticks:
                    self._tick_history[sym] = self._tick_history[sym][-self._max_ticks:]
            except Exception as exc:
                logger.warning("Price fetch failed for %s: %s", sym, exc)
        return prices

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> pd.DataFrame:
        exchange = self._get_exchange()
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["trade_date"] = pd.to_datetime(df["timestamp"], unit="ms")
            df = df.set_index("trade_date").sort_index()
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            return df[["open", "high", "low", "close", "volume"]]
        except Exception as exc:
            logger.warning("OHLCV fetch failed for %s: %s", symbol, exc)
            return pd.DataFrame()

    def get_ticks(self, symbol: str, count: int = 10) -> List[float]:
        return self._tick_history.get(symbol, [])[-count:]


class MultiExchangeFetcher:
    """Unified fetcher that aggregates prices from multiple exchanges."""

    def __init__(self):
        self._fetchers: Dict[str, Any] = {}  # exchange_id -> fetcher
        self._symbol_exchange: Dict[str, str] = {}  # symbol -> exchange_id
        self._tick_history: Dict[str, List[float]] = {}
        self._max_ticks = 200

    def add_exchange(self, exchange_id: str, symbols: List[str]):
        """Register an exchange with its symbols."""
        if exchange_id == "hyperliquid":
            self._fetchers[exchange_id] = HyperliquidFetcher(symbols)
        else:
            self._fetchers[exchange_id] = CCXTDataFetcher(symbols, exchange_id)
        for sym in symbols:
            self._symbol_exchange[sym] = exchange_id

    def fetch_prices(self) -> Dict[str, float]:
        """Fetch prices from all registered exchanges."""
        all_prices = {}
        for exchange_id, fetcher in self._fetchers.items():
            try:
                prices = fetcher.fetch_prices()
                all_prices.update(prices)
            except Exception as exc:
                logger.warning("Exchange %s price fetch failed: %s", exchange_id, exc)
        # Update unified tick history
        for sym, price in all_prices.items():
            if sym not in self._tick_history:
                self._tick_history[sym] = []
            self._tick_history[sym].append(price)
            if len(self._tick_history[sym]) > self._max_ticks:
                self._tick_history[sym] = self._tick_history[sym][-self._max_ticks:]
        return all_prices

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> pd.DataFrame:
        """Fetch OHLCV from the correct exchange for a symbol."""
        exchange_id = self._symbol_exchange.get(symbol)
        if exchange_id and exchange_id in self._fetchers:
            return self._fetchers[exchange_id].fetch_ohlcv(symbol, timeframe, limit)
        return pd.DataFrame()

    def get_ticks(self, symbol: str, count: int = 10) -> List[float]:
        return self._tick_history.get(symbol, [])[-count:]


# ── Technical Signals (adapted for commodity price ranges) ──────────────


class CommodityTechnicalSignals:
    """Technical analysis signals adapted for commodity price ranges."""

    def __init__(self, fetcher: MultiExchangeFetcher):
        self.fetcher = fetcher

    def fetch_indicators(self, symbols: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
        targets = symbols or self.fetcher.symbols
        indicators = {}

        for sym in targets:
            try:
                df = self.fetcher.fetch_ohlcv(sym, "1h", 100)
                if len(df) < 30:
                    continue

                closes = df["close"].values
                highs = df["high"].values
                lows = df["low"].values
                volumes = df["volume"].values
                current = float(closes[-1])

                ma10 = float(sum(closes[-10:]) / 10)
                ma20 = float(sum(closes[-20:]) / 20)
                ma50 = float(sum(closes[-50:]) / 50) if len(closes) >= 50 else current

                deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
                recent = deltas[-14:]
                gains = sum(d for d in recent if d > 0)
                losses = sum(-d for d in recent if d < 0)
                rsi = 100 - (100 / (1 + gains / losses)) if losses > 0 else (100 if gains > 0 else 50)

                mean20 = float(sum(closes[-20:]) / 20)
                std20 = (sum((c - mean20) ** 2 for c in closes[-20:]) / 20) ** 0.5
                boll_lo = mean20 - 2 * std20
                boll_hi = mean20 + 2 * std20

                vol_mean = float(sum(volumes[-20:]) / 20) if len(volumes) >= 20 else 1.0
                vol_ratio = float(volumes[-1] / vol_mean) if vol_mean > 0 else 1.0

                atr = float(sum(highs[-14:]) / 14 - sum(lows[-14:]) / 14)

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
            except Exception as exc:
                logger.warning("Indicator fetch failed for %s: %s", sym, exc)

        return indicators

    def generate_signals(self, indicators: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        signals = {}
        for sym, ind in indicators.items():
            price = ind["price"]
            ma20 = ind["ma20"]
            rsi = ind["rsi"]
            boll_lo = ind["boll_lo"]
            boll_hi = ind["boll_hi"]

            score = 0
            reasons = []

            # RSI Mean Reversion
            if rsi < 30:
                score += 30
                reasons.append(f"RSI oversold ({rsi:.0f})")
            elif rsi > 70:
                score -= 30
                reasons.append(f"RSI overbought ({rsi:.0f})")
            elif rsi < 45:
                score += 10
                reasons.append(f"RSI leaning bearish ({rsi:.0f})")
            elif rsi > 55:
                score -= 10
                reasons.append(f"RSI leaning bullish ({rsi:.0f})")

            # Bollinger Band confirmation
            if price < boll_lo:
                score += 15
                reasons.append("below Bollinger lower")
            elif price > boll_hi:
                score -= 15
                reasons.append("above Bollinger upper")

            # MA20 momentum
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


# ── Lightweight Paper Engine (for $10 wallet) ──────────────────────────


class MicroPaperEngine:
    """Paper trading engine scaled for small accounts ($10)."""

    def __init__(self, initial_capital: float, leverage: float = 3.0, commission_rate: float = 0.0005):
        self.capital = initial_capital
        self.leverage = leverage
        self.commission_rate = commission_rate
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.trade_log: List[Dict[str, Any]] = []
        self.equity_history: List[Dict[str, Any]] = []

    def open_position(
        self, symbol: str, direction: int, size_usd: float, price: float, sl: float, tp: float,
    ) -> Optional[Dict[str, Any]]:
        notional = size_usd
        margin = notional / self.leverage
        commission = notional * self.commission_rate
        if margin + commission > self.capital * 0.95:
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

    def get_equity(self, prices: Dict[str, float]) -> float:
        equity = self.capital
        for sym, pos in self.positions.items():
            equity += pos["margin"]
            if sym in prices:
                equity += (prices[sym] - pos["entry"]) * pos["size"] * pos["direction"]
        return equity

    def get_summary(self, prices: Dict[str, float]) -> Dict[str, Any]:
        realized = sum(t.get("pnl", 0) for t in self.trade_log if t["action"] == "CLOSE")
        total_trades = len([t for t in self.trade_log if t["action"] == "CLOSE"])
        wins = len([t for t in self.trade_log if t["action"] == "CLOSE" and t.get("pnl", 0) > 0])
        equity = self.get_equity(prices)

        return {
            "equity": round(equity, 2),
            "capital": round(self.capital, 2),
            "realized_pnl": round(realized, 2),
            "total_pnl": round(realized, 2),
            "total_trades": total_trades,
            "wins": wins,
            "win_rate": round(wins / total_trades * 100, 1) if total_trades else 0,
            "open_positions": len(self.positions),
            "positions": {
                sym: {
                    "dir": "S" if p["direction"] == -1 else "L",
                    "size": p["size"],
                    "entry": p["entry"],
                }
                for sym, p in self.positions.items()
            },
        }


# ── Main Orchestrator ──────────────────────────────────────────────────


class ForexCommodityOrchestrator:
    """Multi-agent orchestrator for commodity paper trading across exchanges."""

    def __init__(self, config: Dict[str, Any]):
        self.symbols = config["symbols"]
        self.tick_interval = config.get("tick_interval_seconds", 60)
        self.initial_cash = config.get("initial_cash", 10.0)
        self.leverage = config.get("leverage", 3.0)
        self.daily_profit_target = config.get("daily_profit_target", 10.0)
        self.run_dir = config.get("run_dir", "./paper_runs/forex_commodity")
        self.max_positions = config.get("max_positions", 3)
        self.position_pct = config.get("position_pct", 0.35)

        # Multi-exchange fetcher
        self.fetcher = MultiExchangeFetcher()
        exchange_configs = config.get("exchange_configs", {})
        for exchange_id, symbols in exchange_configs.items():
            self.fetcher.add_exchange(exchange_id, symbols)
            logger.info("Registered exchange: %s -> %s", exchange_id, symbols)

        self.signals = CommodityTechnicalSignals(self.fetcher)
        self.engine = MicroPaperEngine(
            initial_capital=self.initial_cash,
            leverage=self.leverage,
        )

        # State
        self._running = False
        self._tick_count = 0
        self._start_equity = self.initial_cash
        self._daily_start_equity = self.initial_cash

        Path(self.run_dir).mkdir(parents=True, exist_ok=True)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        logger.info(
            "MultiExchangeOrchestrator: %s | %ds ticks | $%.2f | %dx | target: $%.2f/day",
            ", ".join(self.symbols),
            self.tick_interval,
            self.initial_cash,
            self.leverage,
            self.daily_profit_target,
        )

    def _signal_handler(self, signum, frame):
        logger.info("Shutdown signal received")
        self._running = False

    def run(self):
        self._running = True
        self._daily_start_equity = self.engine.get_equity(self.fetcher.fetch_prices() or {})

        logger.info("=" * 60)
        logger.info("MULTI-EXCHANGE COMMODITY & STOCK PAPER TRADER")
        logger.info("Symbols: %s", self.symbols)
        logger.info("Capital: $%.2f | Leverage: %dx", self.initial_cash, self.leverage)
        logger.info("Daily profit target: $%.2f", self.daily_profit_target)
        logger.info("=" * 60)

        while self._running:
            try:
                self._tick()
            except Exception as exc:
                logger.error("Tick failed: %s", exc, exc_info=True)

            if self._running:
                time.sleep(self.tick_interval)

        self._print_final_summary()

    def _tick(self):
        self._tick_count += 1

        # 1. Fetch prices
        prices = self.fetcher.fetch_prices()
        if not prices:
            return

        # 2. Compute indicators + signals
        indicators = self.signals.fetch_indicators(self.symbols)
        entry_signals = self.signals.generate_signals(indicators)

        # 3. Check exits for open positions
        for sym in list(self.engine.positions.keys()):
            if sym not in prices:
                continue
            pos = self.engine.positions[sym]
            price = prices[sym]
            direction = pos["direction"]
            entry = pos["entry"]

            if direction > 0:
                pnl = (price - entry) * pos["size"]
            else:
                pnl = (entry - price) * pos["size"]

            # Take profit at $0.05+ (micro account)
            if pnl >= 0.05:
                self.engine.close_position(sym, price, "take_profit")
                logger.info("TP: %s @ %.4f pnl=$%.4f", sym, price, pnl)
                continue

            # Hard stop at -$0.50
            if pnl <= -0.50:
                self.engine.close_position(sym, price, "hard_stop")
                logger.info("STOP: %s @ %.4f pnl=$%.4f", sym, price, pnl)
                continue

            # Trailing stop: if was up $0.10+ and dropped back
            if pnl < 0 and entry != price:
                # Check signal reversal
                sig = entry_signals.get(sym, {})
                if sig.get("action") == "FLAT" or sig.get("confidence", 0) < 40:
                    if pnl > -0.05:
                        self.engine.close_position(sym, price, "signal_weak")
                        logger.info("EXIT: %s @ %.4f pnl=$%.4f (weak signal)", sym, price, pnl)
                        continue

        # 4. Check daily target
        equity = self.engine.get_equity(prices)
        daily_pnl = equity - self._daily_start_equity
        if daily_pnl >= self.daily_profit_target:
            logger.info("DAILY TARGET HIT: $%.2f >= $%.2f", daily_pnl, self.daily_profit_target)

        # 5. Open new positions based on signals
        open_count = len(self.engine.positions)
        if open_count < self.max_positions:
            for sym, sig in entry_signals.items():
                if open_count >= self.max_positions:
                    break
                if sym in self.engine.positions:
                    continue
                if sig["action"] == "FLAT":
                    continue
                if sig["confidence"] < 50:
                    continue

                price = prices.get(sym)
                if not price:
                    continue

                # Position sizing: 40% of equity per position, min $1
                size_usd = max(1.0, equity * self.position_pct)
                if size_usd > equity * 0.9:
                    size_usd = equity * 0.9

                direction = 1 if sig["action"] == "LONG" else -1

                # Commodity-appropriate SL/TP (% based)
                sl_pct = 0.005  # 0.5% SL
                tp_pct = 0.008  # 0.8% TP

                sl = price * (1 - sl_pct) if direction == 1 else price * (1 + sl_pct)
                tp = price * (1 + tp_pct) if direction == 1 else price * (1 - tp_pct)

                trade = self.engine.open_position(sym, direction, size_usd, price, sl, tp)
                if trade:
                    open_count += 1
                    logger.info(
                        "ENTRY: %s %s $%.2f @ %.4f | conf=%d | %s",
                        sym, sig["action"], size_usd, price, sig["confidence"],
                        ", ".join(sig["reasons"]),
                    )

        # 6. Log + print summary
        summary = self.engine.get_summary(prices)
        self._log_tick(summary, indicators, entry_signals)

        if self._tick_count % 5 == 0:
            self._print_tick_summary(summary, daily_pnl)

    def _log_tick(self, summary, indicators, signals):
        record = {
            "tick": self._tick_count,
            "time": datetime.now(timezone.utc).isoformat(),
            **summary,
            "indicators": indicators,
            "signals": {s: {"action": sig["action"], "confidence": sig["confidence"], "reasons": sig["reasons"]} for s, sig in signals.items()},
        }
        with open(Path(self.run_dir) / "equity.jsonl", "a") as f:
            f.write(json.dumps(record, default=str) + "\n")

        # Save state
        state = {
            "tick": self._tick_count,
            "equity": summary["equity"],
            "total_pnl": summary["total_pnl"],
            "positions": summary["positions"],
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(Path(self.run_dir) / "state.json", "w") as f:
            json.dump(state, f, indent=2)

    def _print_tick_summary(self, summary, daily_pnl):
        logger.info(
            "Tick %d | Equity: $%.2f | Daily PnL: $%.2f | Trades: %d | Wins: %d (%.1f%%) | Open: %d",
            self._tick_count, summary["equity"], daily_pnl,
            summary["total_trades"], summary["wins"], summary["win_rate"],
            summary["open_positions"],
        )
        for sym, pos in summary.get("positions", {}).items():
            logger.info("  %s [%s] size=%.6f entry=$%.4f", sym, pos["dir"], pos["size"], pos["entry"])

    def _print_final_summary(self):
        prices = self.fetcher.fetch_prices()
        summary = self.engine.get_summary(prices)
        logger.info("=" * 60)
        logger.info("FINAL SUMMARY — %d ticks", self._tick_count)
        logger.info("Equity: $%.2f (started: $%.2f)", summary["equity"], self.initial_cash)
        logger.info("Total PnL: $%.2f", summary["total_pnl"])
        logger.info("Total trades: %d | Win rate: %.1f%%", summary["total_trades"], summary["win_rate"])
        logger.info("=" * 60)

        # Save final trade log
        with open(Path(self.run_dir) / "trades.jsonl", "w") as f:
            for t in self.engine.trade_log:
                f.write(json.dumps(t, default=str) + "\n")


# ── CLI Entry Point ────────────────────────────────────────────────────


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("./paper_runs/forex_commodity/trader.log", mode="a"),
        ],
    )

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    # Default: all commodity/stock perps across exchanges
    default_binance = ["XAU/USDT:USDT", "XAG/USDT:USDT", "NATGAS/USDT:USDT"]
    default_hyperliquid = ["PAXG", "SPX"]

    all_symbols = default_binance + default_hyperliquid

    config = {
        "symbols": all_symbols,
        "exchange_configs": {
            "binance": default_binance,
            "hyperliquid": default_hyperliquid,
        },
        "tick_interval_seconds": int(os.environ.get("TICK_INTERVAL", "60")),
        "initial_cash": float(os.environ.get("INITIAL_CASH", "10")),
        "leverage": float(os.environ.get("LEVERAGE", "3")),
        "daily_profit_target": float(os.environ.get("DAILY_TARGET", "10")),
        "max_positions": 5,
        "position_pct": 0.25,
        "run_dir": os.environ.get("RUN_DIR", "./paper_runs/forex_commodity"),
    }

    # Allow env override for symbols
    symbols_env = os.environ.get("SYMBOLS")
    if symbols_env:
        user_symbols = [s.strip() for s in symbols_env.split(",")]
        config["symbols"] = user_symbols
        # Auto-detect exchange for each symbol
        binance_syms = [s for s in user_symbols if "/" in s and ":USDT" in s]
        hl_syms = [s for s in user_symbols if "/" not in s or ":USDT" not in s]
        config["exchange_configs"] = {}
        if binance_syms:
            config["exchange_configs"]["binance"] = binance_syms
        if hl_syms:
            config["exchange_configs"]["hyperliquid"] = hl_syms

    orchestrator = ForexCommodityOrchestrator(config)
    orchestrator.run()


if __name__ == "__main__":
    main()
