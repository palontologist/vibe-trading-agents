"""Multi-signal confluence strategy for aggressive growth.

Signal Stack:
  1. Sentiment Contrarian - Buy extreme fear, sell extreme greed
  2. Momentum - MA crossover + RSI confirmation
  3. Breakout - Bollinger squeeze + volume surge
  4. Rotation - Weakest performer exit, high-beta entry
  5. Factor Alpha - Cross-sectional momentum/volatility ranking

Each signal outputs a score in [-1.0, 1.0] and a confidence [0, 100].
Final signal = weighted average of all active signals.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ── Asset Universe ──────────────────────────────────────────────────────

CRYPTO_ASSETS = ["BTC", "ETH", "SOL", "WIF", "JUP", "RENDER", "DOGE", "SUI", "LINK", "NEAR", "AAVE", "FET"]
FOREX_ASSETS = ["MXN", "BRL", "ZAR", "NZD", "HUF", "MYR", "PLN"]
COMMODITY_ASSETS = ["XAU", "XAG", "NATGAS"]

# High-beta assets for rotation targets (ranked by historical beta)
HIGH_BETA_CRYPTO = ["SOL", "WIF", "JUP", "DOGE", "SUI", "RENDER"]
HIGH_BETA_FOREX = ["MXN", "BRL", "ZAR"]

# Assets with known high correlation (for diversification)
CORRELATION_GROUPS = {
    "btc_group": ["BTC", "ETH"],
    "alt_l1": ["SOL", "NEAR", "SUI"],
    "defi": ["AAVE", "LINK", "RENDER"],
    "meme": ["DOGE", "WIF", "JUP"],
    "commodity": ["XAU", "XAG"],
}


@dataclass
class SignalOutput:
    """Single signal result."""
    name: str
    direction: float  # -1.0 to 1.0
    confidence: int   # 0 to 100
    reason: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConfluenceSignal:
    """Combined multi-signal output."""
    symbol: str
    direction: float  # -1.0 to 1.0 (weighted average)
    confidence: int   # 0 to 100
    signals: List[SignalOutput] = field(default_factory=list)
    action: str = "FLAT"  # LONG, SHORT, FLAT, CLOSE
    size_pct: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    reason: str = ""


class GrowthStrategy:
    """Multi-signal confluence engine for $10 -> $10K growth.

    Combines 5 signal types with dynamic weighting based on market regime.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.min_confidence = self.config.get("min_confidence", 40)
        self.min_confluence = self.config.get("min_confluence", 0.20)
        self.lookback = self.config.get("lookback", 50)

        # Signal weights (sum to 1.0)
        self.weights = self.config.get("signal_weights", {
            "sentiment": 0.25,
            "momentum": 0.25,
            "breakout": 0.20,
            "rotation": 0.15,
            "factor": 0.15,
        })

        # Tracking for rotation signal
        self._position_pnl: Dict[str, float] = {}
        self._position_age: Dict[str, float] = {}  # seconds held
        self._fear_greed_history: List[int] = []

    def update_fear_greed(self, fg_value: int) -> None:
        """Update Fear & Greed index (0=Extreme Fear, 100=Extreme Greed)."""
        self._fear_greed_history.append(fg_value)
        if len(self._fear_greed_history) > 100:
            self._fear_greed_history = self._fear_greed_history[-100:]

    def update_position_tracking(self, symbol: str, pnl: float, age_seconds: float) -> None:
        """Update position PnL and age for rotation signal."""
        self._position_pnl[symbol] = pnl
        self._position_age[symbol] = age_seconds

    def generate_signals(
        self,
        prices: Dict[str, float],
        historical_data: Dict[str, pd.DataFrame],
        open_positions: Dict[str, Any],
        equity: float,
        day_progress: float = 0.0,
    ) -> List[ConfluenceSignal]:
        """Generate confluence signals for all assets.

        Args:
            prices: Current prices {symbol: price}
            historical_data: {symbol: DataFrame with open/high/low/close/volume}
            open_positions: {symbol: position_dict}
            equity: Current portfolio equity
            day_progress: 0.0 to 1.0, progress through trading day

        Returns:
            List of ConfluenceSignal for each actionable asset
        """
        signals = []

        all_assets = list(prices.keys())
        for symbol in all_assets:
            if symbol not in historical_data:
                continue

            df = historical_data[symbol]
            if len(df) < self.lookback:
                continue

            # Compute all signal types
            signal_outputs = []

            # 1. Sentiment Contrarian
            if self._fear_greed_history:
                s = self._sentiment_signal(symbol, prices[symbol])
                signal_outputs.append(s)

            # 2. Momentum
            s = self._momentum_signal(symbol, prices[symbol], df)
            signal_outputs.append(s)

            # 3. Breakout
            s = self._breakout_signal(symbol, prices[symbol], df)
            signal_outputs.append(s)

            # 4. Rotation (only if we have open positions)
            if open_positions:
                s = self._rotation_signal(symbol, open_positions)
                if s:
                    signal_outputs.append(s)

            # 5. Factor Alpha (cross-sectional ranking)
            s = self._factor_signal(symbol, df)
            signal_outputs.append(s)

            # Combine signals
            confluence = self._combine_signals(symbol, signal_outputs, equity, day_progress)
            if confluence:
                signals.append(confluence)

        return signals

    def _sentiment_signal(self, symbol: str, price: float) -> SignalOutput:
        """Fear & Greed contrarian signal.

        Extreme Fear (<25) -> contrarian LONG
        Extreme Greed (>75) -> contrarian SHORT
        """
        fg = self._fear_greed_history[-1] if self._fear_greed_history else 50
        fg_trend = 0
        if len(self._fear_greed_history) >= 3:
            fg_trend = self._fear_greed_history[-1] - self._fear_greed_history[-3]

        if fg < 20:
            direction = 0.8
            confidence = 85
            reason = f"F&G={fg} Extreme Fear contrarian LONG"
        elif fg < 30:
            direction = 0.5
            confidence = 70
            reason = f"F&G={fg} Fear zone contrarian LONG"
        elif fg > 80:
            direction = -0.8
            confidence = 80
            reason = f"F&G={fg} Extreme Greed contrarian SHORT"
        elif fg > 70:
            direction = -0.5
            confidence = 65
            reason = f"F&G={fg} Greed zone contrarian SHORT"
        else:
            direction = 0.0
            confidence = 30
            reason = f"F&G={fg} neutral"

        # Boost confidence if F&G is moving toward extreme
        if fg < 30 and fg_trend < -5:
            confidence = min(95, confidence + 10)
            reason += " (improving fear)"
        elif fg > 70 and fg_trend > 5:
            confidence = min(95, confidence + 10)
            reason += " (increasing greed)"

        return SignalOutput(
            name="sentiment",
            direction=direction,
            confidence=confidence,
            reason=reason,
            metadata={"fear_greed": fg, "trend": fg_trend},
        )

    def _momentum_signal(self, symbol: str, price: float, df: pd.DataFrame) -> SignalOutput:
        """MA crossover + RSI confirmation."""
        close = df["close"].values
        if len(close) < 50:
            return SignalOutput("momentum", 0.0, 20, "Insufficient data")

        # RSI(14)
        rsi = self._compute_rsi(close, 14)
        current_rsi = rsi[-1] if not np.isnan(rsi[-1]) else 50

        # MA crossover
        ma10 = np.mean(close[-10:])
        ma20 = np.mean(close[-20:])
        ma50 = np.mean(close[-50:])

        # Score
        score = 0.0
        reasons = []

        # RSI signals
        if current_rsi < 30:
            score += 0.4
            reasons.append(f"RSI={current_rsi:.0f} oversold")
        elif current_rsi < 40:
            score += 0.2
            reasons.append(f"RSI={current_rsi:.0f} low")
        elif current_rsi > 70:
            score -= 0.4
            reasons.append(f"RSI={current_rsi:.0f} overbought")
        elif current_rsi > 60:
            score -= 0.2
            reasons.append(f"RSI={current_rsi:.0f} high")

        # MA alignment
        if price > ma10 > ma20 > ma50:
            score += 0.4
            reasons.append("MA bullish alignment")
        elif price < ma10 < ma20 < ma50:
            score -= 0.4
            reasons.append("MA bearish alignment")
        elif ma10 > ma20:
            score += 0.15
            reasons.append("MA10>MA20")
        elif ma10 < ma20:
            score -= 0.15
            reasons.append("MA10<MA20")

        # Price momentum
        pct_5 = (price - close[-5]) / close[-5] if close[-5] > 0 else 0
        pct_20 = (price - close[-20]) / close[-20] if close[-20] > 0 else 0

        if pct_5 > 0.03:
            score += 0.2
            reasons.append(f"+{pct_5*100:.1f}% 5-bar momentum")
        elif pct_5 < -0.03:
            score -= 0.2
            reasons.append(f"{pct_5*100:.1f}% 5-bar momentum")

        direction = np.clip(score, -1.0, 1.0)
        confidence = min(90, int(50 + abs(score) * 50))

        return SignalOutput(
            name="momentum",
            direction=direction,
            confidence=confidence,
            reason="; ".join(reasons) if reasons else "neutral",
            metadata={"rsi": current_rsi, "ma10": ma10, "ma20": ma20, "pct_5": pct_5},
        )

    def _breakout_signal(self, symbol: str, price: float, df: pd.DataFrame) -> SignalOutput:
        """Bollinger squeeze + volume surge detection."""
        close = df["close"].values
        volume = df["volume"].values if "volume" in df.columns else np.ones_like(close)

        if len(close) < 20:
            return SignalOutput("breakout", 0.0, 20, "Insufficient data")

        # Bollinger Bands
        ma20 = np.mean(close[-20:])
        std20 = np.std(close[-20:])
        upper = ma20 + 2 * std20
        lower = ma20 - 2 * std20

        # Bandwidth (squeeze indicator)
        bandwidth = (upper - lower) / ma20 if ma20 > 0 else 0
        # Simple historical average bandwidth
        if len(close) > 40:
            hist_bw = []
            for i in range(20, min(len(close), 60)):
                m = np.mean(close[i-20:i])
                s = np.std(close[i-20:i])
                if m > 0:
                    hist_bw.append((m + 2*s - (m - 2*s)) / m)
            avg_bandwidth = np.mean(hist_bw) if hist_bw else bandwidth
        else:
            avg_bandwidth = bandwidth
        squeeze = bandwidth < avg_bandwidth * 0.7 if avg_bandwidth > 0 else False

        # Volume surge
        vol_avg = np.mean(volume[-20:]) if len(volume) >= 20 else np.mean(volume)
        vol_ratio = volume[-1] / vol_avg if vol_avg > 0 else 1.0
        volume_surge = vol_ratio > 1.5

        score = 0.0
        reasons = []

        if price > upper and volume_surge:
            score = 0.7
            reasons.append(f"Breakout above upper BB, vol={vol_ratio:.1f}x")
        elif price < lower and volume_surge:
            score = -0.7
            reasons.append(f"Breakdown below lower BB, vol={vol_ratio:.1f}x")
        elif squeeze and volume_surge:
            # Squeeze breakout - direction from recent momentum
            recent = (close[-1] - close[-5]) / close[-5] if close[-5] > 0 else 0
            score = 0.5 if recent > 0 else -0.5
            reasons.append(f"Squeeze breakout, vol={vol_ratio:.1f}x, direction={'up' if score > 0 else 'down'}")
        elif squeeze:
            score = 0.0
            reasons.append("BB squeeze forming (waiting for breakout)")
        else:
            score = 0.0
            reasons.append("No breakout pattern")

        direction = np.clip(score, -1.0, 1.0)
        confidence = min(85, int(40 + abs(score) * 50))

        return SignalOutput(
            name="breakout",
            direction=direction,
            confidence=confidence,
            reason="; ".join(reasons),
            metadata={"bandwidth": bandwidth, "squeeze": squeeze, "vol_ratio": vol_ratio},
        )

    def _rotation_signal(self, symbol: str, open_positions: Dict[str, Any]) -> Optional[SignalOutput]:
        """Rotation: sell weakest performers, buy high-beta.

        Logic from 赛博六壬交易员:
        - Close positions that are oldest and weakest (lowest PnL)
        - Rotate into high-beta assets not currently held
        """
        if symbol not in open_positions:
            # Entry signal: if this is a high-beta asset not held, consider entry
            is_high_beta = symbol in HIGH_BETA_CRYPTO or symbol in HIGH_BETA_FOREX
            if not is_high_beta:
                return None

            # Check if we have space for rotation entry
            held_symbols = set(open_positions.keys())
            if len(held_symbols) < 3:
                return None

            # Find weakest held position
            weakest = min(
                held_symbols,
                key=lambda s: self._position_pnl.get(s, 0),
            )
            weakest_pnl = self._position_pnl.get(weakest, 0)

            # Only rotate if new asset looks better
            if weakest_pnl < -0.005:  # -0.5% loss
                return SignalOutput(
                    name="rotation",
                    direction=0.5,
                    confidence=65,
                    reason=f"Rotation entry: {symbol} replaces weakest {weakest} (PnL={weakest_pnl*100:.2f}%)",
                    metadata={"replace": weakest, "replace_pnl": weakest_pnl},
                )
            return None

        # Exit signal: if this is the weakest position, exit
        held_symbols = set(open_positions.keys())
        if len(held_symbols) <= 1:
            return None

        weakest = min(held_symbols, key=lambda s: self._position_pnl.get(s, 0))
        if symbol == weakest:
            pnl = self._position_pnl.get(symbol, 0)
            age_h = self._position_age.get(symbol, 0) / 3600

            # Force exit if > 6 hours or losing
            if age_h > 6:
                return SignalOutput(
                    name="rotation",
                    direction=-1.0 if open_positions[symbol].get("side") == "LONG" else 1.0,
                    confidence=80,
                    reason=f"6h forced close: {symbol} (age={age_h:.1f}h, PnL={pnl*100:.2f}%)",
                    metadata={"forced_exit": True, "age_hours": age_h},
                )
            elif pnl < -0.02:  # -2% loss
                return SignalOutput(
                    name="rotation",
                    direction=-1.0 if open_positions[symbol].get("side") == "LONG" else 1.0,
                    confidence=75,
                    reason=f"Weakest rotation exit: {symbol} (PnL={pnl*100:.2f}%)",
                    metadata={"weakest_exit": True},
                )

        return None

    def _factor_signal(self, symbol: str, df: pd.DataFrame) -> SignalOutput:
        """Cross-sectional factor ranking (momentum + volatility).

        Inspired by Alpha Zoo framework: rank assets by recent momentum
        and inverse volatility for a combined factor score.
        """
        close = df["close"].values
        if len(close) < 20:
            return SignalOutput("factor", 0.0, 20, "Insufficient data")

        # Momentum factor: 10-day return
        mom_10 = (close[-1] - close[-10]) / close[-10] if close[-10] > 0 else 0

        # Volatility factor: 10-day realized vol (inverse = lower vol is better)
        returns = np.diff(close[-11:]) / close[-11:-1]
        vol_10 = np.std(returns) if len(returns) > 1 else 0.01
        inv_vol = 1.0 / vol_10 if vol_10 > 0 else 0

        # Combined factor score
        score = np.clip(mom_10 * 5 + inv_vol * 0.1, -1.0, 1.0)
        confidence = min(75, int(40 + abs(score) * 40))

        reason = f"Factor: mom={mom_10*100:.1f}%, vol={vol_10*100:.1f}%"
        direction = float(score)

        return SignalOutput(
            name="factor",
            direction=direction,
            confidence=confidence,
            reason=reason,
            metadata={"momentum_10d": mom_10, "vol_10d": vol_10},
        )

    def _combine_signals(
        self,
        symbol: str,
        signals: List[SignalOutput],
        equity: float,
        day_progress: float,
    ) -> Optional[ConfluenceSignal]:
        """Combine all signals with dynamic weighting."""
        if not signals:
            return None

        weighted_dir = 0.0
        total_weight = 0.0
        total_confidence = 0
        reasons = []

        for s in signals:
            w = self.weights.get(s.name, 0.1)
            weighted_dir += s.direction * w * (s.confidence / 100)
            total_weight += w
            total_confidence += s.confidence
            if abs(s.direction) > 0.1:
                reasons.append(f"[{s.name}] {s.reason}")

        if total_weight == 0:
            return None

        direction = weighted_dir / total_weight
        avg_confidence = total_confidence // len(signals)

        # Determine action
        action = "FLAT"
        size_pct = 0.0

        if abs(direction) >= self.min_confluence and avg_confidence >= self.min_confidence:
            if direction > 0:
                action = "LONG"
            else:
                action = "SHORT"
            # Position sizing scales with confidence
            size_pct = min(0.85, 0.50 + (avg_confidence - 50) / 80)

        # Dynamic sizing based on day progress (compound more as we approach target)
        if day_progress > 0.5:
            size_pct = min(0.90, size_pct * 1.3)
        if day_progress > 0.7:
            size_pct = min(0.95, size_pct * 1.5)

        # Set stops
        stop_loss = 0.03 if action != "FLAT" else 0.0  # 3% hard stop
        take_profit = 0.05 if action != "FLAT" else 0.0  # 5% take profit

        return ConfluenceSignal(
            symbol=symbol,
            direction=float(np.clip(direction, -1.0, 1.0)),
            confidence=avg_confidence,
            signals=signals,
            action=action,
            size_pct=size_pct,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reason="; ".join(reasons) if reasons else "no clear signal",
        )

    @staticmethod
    def _compute_rsi(prices: np.ndarray, period: int = 14) -> np.ndarray:
        """Compute RSI indicator."""
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        avg_gain = np.full_like(prices, np.nan)
        avg_loss = np.full_like(prices, np.nan)

        if len(gains) < period:
            return avg_gain

        avg_gain[period] = np.mean(gains[:period])
        avg_loss[period] = np.mean(losses[:period])

        for i in range(period + 1, len(prices)):
            avg_gain[i] = (avg_gain[i-1] * (period - 1) + gains[i-1]) / period
            avg_loss[i] = (avg_loss[i-1] * (period - 1) + losses[i-1]) / period

        rs = np.where(avg_loss > 0, avg_gain / avg_loss, 100)
        rsi = 100 - (100 / (1 + rs))
        rsi[:period] = np.nan
        return rsi
