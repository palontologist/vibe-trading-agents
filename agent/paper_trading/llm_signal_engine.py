"""LLM-driven signal engine.

Replaces static signal_fn with a dynamic LLM call each cycle.
Feeds OHLCV data, current positions, and portfolio state to the LLM,
then parses structured signal output.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

SIGNAL_SYSTEM_PROMPT = """\
You are an expert crypto scalping specialist focused on MEAN REVERSION setups. \
Your edge: identifying when price has stretched too far from fair value and is \
likely to snap back.

Core principle: Crypto overextends on every move. When RSI hits extremes and price \
breaks Bollinger bands, the move is exhausted and will reverse. Your job is to \
catch these reversals early.

Rules:
- Return ONLY a JSON object, no explanation outside it.
- Each symbol gets a signal with action, confidence (0-100), and target_weight (-1.0 to 1.0).
- action: "LONG", "SHORT", or "CLOSE"
- target_weight: positive for long, negative for short, 0 to close
- confidence: 0-100, only trade if >= {min_confidence}
- risk_score: 0-100, lower is safer, only trade if <= {max_risk_score}
- Include brief reasoning per symbol — be specific about the pattern you see

MEAN REVERSION PATTERNS TO LOOK FOR:
1. OVERSOLD BOUNCE: RSI < 30 + price below Bollinger lower + long lower wick → LONG
2. OVERBOUGHT REVERSAL: RSI > 70 + price above Bollinger upper + long upper wick → SHORT
3. EXTREME VOLUME CLIMAX: Volume spike 3x+ average + price at extreme → fade it
4. REJECTION WICKS: Candle wicks 2x+ body size at support/resistance → trade reversal
5. RANGE BOUNCE: Price hits range boundary with weakening momentum → fade to middle

AVOID:
- Don't chase breakouts (they fail more than they succeed)
- Don't trade with strong momentum (wait for exhaustion)
- If market is in strong directional move with no wicks, say CLOSE

PRICE PREDICTIONS (REQUIRED for every symbol):
- predicted_price_1h: Your best estimate of the price in 1 hour
- predicted_price_4h: Your best estimate of the price in 4 hours
- predicted_price_24h: Your best estimate of the price in 24 hours
- These predictions are tracked and scored against actual prices to measure your accuracy
- Your recent prediction accuracy is shown below — use it to calibrate your confidence

BE AGGRESSIVE on clear mean-reversion setups. If RSI is extreme and price is at \
a Bollinger band, that's your signal. If the market is in the middle of range, \
say CLOSE.
"""

SIGNAL_USER_PROMPT = """\
Current time: {time}

Portfolio state:
  Total equity: ${equity:.2f}
  Open positions: {positions}

Market data for {num_symbols} symbols (last {bars} 1H bars each):

{data_summary}

Recent price action (last 10 candles OHLC for each symbol):
{recent_candles}

{prediction_history}

Return JSON in this exact format:
{{
  "signals": {{
    "SYMBOL": {{
      "action": "LONG|SHORT|CLOSE",
      "confidence": 0-100,
      "target_weight": -1.0 to 1.0,
      "risk_score": 0-100,
      "reasoning": "brief reason",
      "predicted_price_1h": float (your best estimate of price in 1 hour),
      "predicted_price_4h": float (your best estimate of price in 4 hours),
      "predicted_price_24h": float (your best estimate of price in 24 hours)
    }}
  }},
  "market_regime": "bullish|bearish|sideways|volatile",
  "overall_risk": "low|medium|high"
}}
"""


class PredictionTracker:
    """Tracks LLM price predictions and scores them against actual prices."""

    def __init__(self):
        self._predictions: List[Dict[str, Any]] = []
        self._max_history = 50

    def record(self, symbol: str, current_price: float, predictions: Dict[str, Any]):
        """Record a prediction from the LLM."""
        entry = {
            "symbol": symbol,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "current_price": current_price,
            "predicted_1h": predictions.get("predicted_price_1h"),
            "predicted_4h": predictions.get("predicted_price_4h"),
            "predicted_24h": predictions.get("predicted_price_24h"),
            "actual_1h": None,
            "actual_4h": None,
            "actual_24h": None,
            "error_1h": None,
            "error_4h": None,
            "error_24h": None,
            "hit_1h": None,
            "hit_4h": None,
            "hit_24h": None,
        }
        self._predictions.append(entry)
        if len(self._predictions) > self._max_history:
            self._predictions = self._predictions[-self._max_history :]

    def update_with_prices(self, prices: Dict[str, float]):
        """Check predictions against actual prices. Call every tick."""
        now = datetime.now(timezone.utc)
        for pred in self._predictions:
            sym = pred["symbol"]
            if sym not in prices:
                continue
            actual = prices[sym]
            pred_time = datetime.fromisoformat(pred["timestamp"])
            elapsed_hours = (now - pred_time).total_seconds() / 3600

            if pred.get("predicted_1h") and elapsed_hours >= 1 and pred.get("actual_1h") is None:
                pred["actual_1h"] = actual
                pred["error_1h"] = abs(actual - pred["predicted_1h"]) / pred["predicted_1h"] * 100
                pred["hit_1h"] = pred["predicted_1h"] > pred["current_price"]

            if pred.get("predicted_4h") and elapsed_hours >= 4 and pred.get("actual_4h") is None:
                pred["actual_4h"] = actual
                pred["error_4h"] = abs(actual - pred["predicted_4h"]) / pred["predicted_4h"] * 100
                pred["hit_4h"] = pred["predicted_4h"] > pred["current_price"]

            if pred.get("predicted_24h") and elapsed_hours >= 24 and pred.get("actual_24h") is None:
                pred["actual_24h"] = actual
                pred["error_24h"] = abs(actual - pred["predicted_24h"]) / pred["predicted_24h"] * 100
                pred["hit_24h"] = pred["predicted_24h"] > pred["current_price"]

    def get_accuracy(self, symbol: Optional[str] = None, horizon: str = "1h") -> Optional[float]:
        """Return average error % for a symbol (or all symbols). Lower is better."""
        horizon_key = f"error_{horizon}"
        errors = [
            p[horizon_key]
            for p in self._predictions
            if (symbol is None or p["symbol"] == symbol) and p.get(horizon_key) is not None
        ]
        if not errors:
            return None
        return sum(errors) / len(errors)

    def get_directional_accuracy(self, symbol: Optional[str] = None, horizon: str = "1h") -> Optional[float]:
        """Return % of time the LLM correctly predicted direction (up/down)."""
        horizon_key = f"hit_{horizon}"
        hits = [
            p[horizon_key]
            for p in self._predictions
            if (symbol is None or p["symbol"] == symbol) and p.get(horizon_key) is not None
        ]
        if not hits:
            return None
        return sum(1 for h in hits if h is not None) / len(hits) * 100

    def get_recent_history(
        self,
        symbol: Optional[str] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Return recent predictions for prompt context."""
        preds = self._predictions if symbol is None else [p for p in self._predictions if p["symbol"] == symbol]
        return preds[-limit:]

    def get_summary_text(self) -> str:
        """Return a text summary of prediction accuracy for the LLM prompt."""
        lines = ["Prediction accuracy (lower error = better):"]
        for horizon in ("1h", "4h", "24h"):
            avg = self.get_accuracy(horizon=horizon)
            if avg is not None:
                lines.append(f"  {horizon} avg error: {avg:.2f}%")
            else:
                lines.append(f"  {horizon}: no scored predictions yet")
        return "\n".join(lines)

    def get_confidence_adjustment(self, symbol: Optional[str] = None) -> float:
        """Return a confidence adjustment (-20 to +20) based on recent accuracy.
        If LLM has been accurate, boost confidence. If wrong, reduce it."""
        err = self.get_accuracy(symbol, "1h")
        if err is None:
            return 0
        if err < 1:
            return 15
        if err < 2:
            return 10
        if err < 3:
            return 5
        if err < 5:
            return 0
        if err < 10:
            return -10
        return -20


class LLMSignalEngine:
    """Generate trading signals via LLM on each cycle."""

    def __init__(
        self,
        min_confidence: int = 70,
        max_risk_score: int = 50,
        model_name: Optional[str] = None,
        llm_factory: Any = None,
        max_bars: int = 50,
        prediction_tracker: Any = None,
    ):
        self.min_confidence = min_confidence
        self.max_risk_score = max_risk_score
        self.model_name = model_name
        self._llm_factory = llm_factory
        self.max_bars = max_bars
        self._llm: Any = None
        self._history: List[Dict[str, Any]] = []
        self.prediction_tracker = prediction_tracker
        self._cached_result: Dict[str, Any] = {}

    def _get_llm(self) -> Any:
        if self._llm is None:
            if self._llm_factory:
                self._llm = self._llm_factory()
            else:
                from src.providers.llm import build_llm

                self._llm = build_llm(model_name=self.model_name)
        return self._llm

    def _fallback_tech_signals(
        self,
        data_map: Dict[str, pd.DataFrame],
    ) -> Dict[str, Any]:
        """Fallback technical analysis signals when LLM is unavailable.

        Uses MA crossovers, RSI, and volume for signal generation.
        """
        signals = {}
        for symbol, df in data_map.items():
            if len(df) < 20:
                continue

            close = df["close"]
            current = close.iloc[-1]
            ma10 = close.tail(10).mean()
            ma20 = close.tail(20).mean()
            rsi = self._calc_rsi(close, 14)
            volume = df["volume"].tail(20)
            vol_ratio = volume.iloc[-1] / volume.mean() if volume.mean() > 0 else 1.0

            score = 0
            reasons = []

            # MA crossover
            if current > ma10 > ma20:
                score += 20
                reasons.append("bullish MA alignment")
            elif current < ma10 < ma20:
                score -= 20
                reasons.append("bearish MA alignment")
            if current > ma20 and close.iloc[-2] <= ma20:
                score += 15
                reasons.append("MA20 crossover up")
            elif current < ma20 and close.iloc[-2] >= ma20:
                score -= 15
                reasons.append("MA20 crossover down")

            # RSI
            if rsi < 30:
                score += 25
                reasons.append(f"RSI oversold ({rsi:.0f})")
            elif rsi > 70:
                score -= 25
                reasons.append(f"RSI overbought ({rsi:.0f})")
            elif rsi < 40:
                score += 10
                reasons.append(f"RSI bearish ({rsi:.0f})")
            elif rsi > 60:
                score -= 10
                reasons.append(f"RSI bullish ({rsi:.0f})")

            # Volume
            if vol_ratio > 2.0 and score > 0:
                score += 10
                reasons.append("high volume confirmation")
            elif vol_ratio > 2.0 and score < 0:
                score -= 10
                reasons.append("high volume confirmation")

            if abs(score) < 20:
                signals[symbol] = {
                    "action": "CLOSE",
                    "confidence": 50,
                    "target_weight": 0.0,
                    "risk_score": 50,
                    "reasoning": f"weak signal ({', '.join(reasons)})",
                }
            else:
                confidence = min(90, 50 + abs(score))
                risk_score = max(10, 50 - abs(score))
                weight = min(0.5, abs(score) / 100) * (1 if score > 0 else -1)
                action = "LONG" if score > 0 else "SHORT"
                signals[symbol] = {
                    "action": action,
                    "confidence": confidence,
                    "target_weight": round(weight, 3),
                    "risk_score": risk_score,
                    "reasoning": f"score={score} ({', '.join(reasons)})",
                }

        return {
            "signals": signals,
            "market_regime": "technical_fallback",
            "overall_risk": "medium",
        }

    def generate_signals(
        self,
        data_map: Dict[str, pd.DataFrame],
        equity: float,
        positions: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Query LLM for trading signals.

        Args:
            data_map: symbol -> OHLCV DataFrame
            equity: current portfolio equity in USD
            positions: current open positions

        Returns:
            Parsed signal dict with signals, market_regime, overall_risk.
        """
        if not data_map:
            return {"signals": {}, "market_regime": "unknown", "overall_risk": "unknown"}

        data_summary = self._summarize_data(data_map)
        position_summary = self._summarize_positions(positions)
        recent_candles = self._summarize_recent_candles(data_map)

        system_prompt = SIGNAL_SYSTEM_PROMPT.format(
            min_confidence=self.min_confidence,
            max_risk_score=self.max_risk_score,
        )

        user_prompt = SIGNAL_USER_PROMPT.format(
            time=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            equity=equity,
            positions=position_summary,
            num_symbols=len(data_map),
            bars=min(self.max_bars, max(len(df) for df in data_map.values()) if data_map else 0),
            data_summary=data_summary,
            recent_candles=recent_candles,
            prediction_history=self.prediction_tracker.get_summary_text()
            if self.prediction_tracker
            else "No prediction history yet.",
        )

        try:
            llm = self._get_llm()
            response = llm.invoke(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
            )

            content = response.content if hasattr(response, "content") else str(response)
            parsed = self._parse_response(content)

            if parsed:
                self._cached_result = parsed
                self._history.append(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "signals": parsed.get("signals", {}),
                    }
                )
                logger.info(
                    "LLM signals: %s | regime: %s | risk: %s",
                    {k: v.get("action") for k, v in parsed.get("signals", {}).items()},
                    parsed.get("market_regime"),
                    parsed.get("overall_risk"),
                )
                return parsed
            else:
                logger.warning("Failed to parse LLM response, returning no signals")
                self._cached_result = {"signals": {}, "market_regime": "unknown", "overall_risk": "unknown"}
                return self._cached_result

        except Exception as exc:
            logger.warning("LLM signal generation failed: %s — falling back to technical analysis", exc)
            fallback = self._fallback_tech_signals(data_map)
            self._cached_result = fallback
            return fallback

    def _summarize_data(self, data_map: Dict[str, pd.DataFrame]) -> str:
        """Summarize OHLCV data for LLM prompt."""
        lines = []
        for symbol, df in data_map.items():
            recent = df.tail(self.max_bars)
            if len(recent) < 5:
                continue

            close = recent["close"]
            current = close.iloc[-1]
            ma10 = close.tail(10).mean() if len(close) >= 10 else current
            ma20 = close.tail(20).mean() if len(close) >= 20 else current
            high = recent["high"].max()
            low = recent["low"].min()
            volume_avg = recent["volume"].mean() if "volume" in recent.columns else 0
            volume_now = recent["volume"].iloc[-1] if "volume" in recent.columns else 0

            rsi = self._calc_rsi(close, 14)
            boll_upper = close.mean() + close.std() * 2 if len(close) >= 5 else current
            boll_lower = close.mean() - close.std() * 2 if len(close) >= 5 else current

            change_1h = (current - close.iloc[0]) / close.iloc[0] * 100 if len(close) > 1 else 0

            lines.append(
                f"  {symbol}:\n"
                f"    Price: ${current:.4f} | Change: {change_1h:+.2f}%\n"
                f"    MA10: ${ma10:.4f} | MA20: ${ma20:.4f}\n"
                f"    Range: ${low:.4f} - ${high:.4f}\n"
                f"    RSI(14): {rsi:.1f}\n"
                f"    Bollinger: ${boll_lower:.4f} - ${boll_upper:.4f}\n"
                f"    Volume: {volume_now:.0f} (avg {volume_avg:.0f})"
            )
        return "\n".join(lines) if lines else "No data available"

    def _summarize_recent_candles(self, data_map: Dict[str, pd.DataFrame]) -> str:
        """Summarize last 10 OHLC candles for LLM price action analysis."""
        lines = []
        for symbol, df in data_map.items():
            recent = df.tail(10)
            if len(recent) < 3:
                continue
            parts = []
            for _, row in recent.iterrows():
                o = float(row["open"])
                h = float(row["high"])
                l = float(row["low"])
                c = float(row["close"])
                v = float(row.get("volume", 0))
                body = c - o
                direction = "+" if body > 0 else "-"
                parts.append(f"{direction}O:{o:.1f} H:{h:.1f} L:{l:.1f} C:{c:.1f} V:{v:.0f}")
            lines.append(f"  {symbol}: {' | '.join(parts)}")
        return "\n".join(lines) if lines else "No recent candles"

    @staticmethod
    def _calc_rsi(series: pd.Series, period: int = 14) -> float:
        """Calculate RSI for the last value."""
        if len(series) < period + 1:
            return 50.0
        delta = series.diff()
        gain = delta.where(delta > 0, 0).tail(period).mean()
        loss = (-delta.where(delta < 0, 0)).tail(period).mean()
        if loss == 0:
            return 100.0
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _summarize_positions(positions: Dict[str, Any]) -> str:
        if not positions:
            return "none"
        parts = []
        for sym, pos in positions.items():
            if isinstance(pos, dict):
                d = pos.get("direction", 0)
                s = pos.get("size", 0)
                e = pos.get("entry", 0)
            else:
                d = getattr(pos, "direction", 0)
                s = getattr(pos, "size", 0)
                e = getattr(pos, "entry_price", 0)
            direction = "LONG" if d > 0 else "SHORT"
            parts.append(f"{sym}: {direction} {s:.4f} @ ${e:.2f}")
        return ", ".join(parts)

    @staticmethod
    def _parse_response(text: str) -> Optional[Dict[str, Any]]:
        """Extract JSON from LLM response."""
        text = text.strip()
        if text.startswith("```"):
            fence_end = text.find("```", 3)
            if fence_end > 0:
                text = text[3:fence_end].strip()
            if text.startswith("json"):
                text = text[4:].strip()

        depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        start = -1
        return None

    def get_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        return self._history[-limit:]
