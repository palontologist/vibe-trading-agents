"""Cohort-based emergent regime detection.

No explicit regime detector is built. Instead, it emerges from tracking
which cohort (short-trained vs long-trained) gets things right.

When short-window agents outperform -> NOVEL_REGIME
When long-window agents outperform -> HISTORICAL_REGIME
When roughly equal -> MIXED

This is the JANUS meta-layer adapted for Vibe-Trading's signal engines.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class CohortMetrics:
    """Accuracy metrics for a cohort over a rolling window."""

    cohort_id: str
    hit_rate: float
    sharpe: float
    n_signals: int
    mean_return: float
    window_start: str
    window_end: str


class CohortRegimeDetector:
    """Emergent regime detection via cohort performance differentials.

    Maintains multiple signal cohorts (e.g., "short_window" trained on
    recent data, "long_window" trained on historical data). The weight
    differential between cohorts is an emergent regime detector.

    Usage:
        detector = CohortRegimeDetector(state_dir="./paper_runs")
        detector.register_cohort("short_window", lookback_hours=168)   # 7 days
        detector.register_cohort("long_window", lookback_hours=2160)   # 90 days
        detector.score_signal("short_window", "BTC-USDT", 1, 80, 65000)
        detector.score_outcome("BTC-USDT", 66000)
        regime = detector.detect_regime()
        weights = detector.get_cohort_weights()
    """

    REGIME_THRESHOLD = 0.15  # Weight diff threshold for regime signals
    MIN_WEIGHT = 0.2
    MAX_WEIGHT = 0.8

    def __init__(self, state_dir: str = "./paper_runs"):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "cohort_regime.json"
        self.signals_file = self.state_dir / "cohort_signals.jsonl"

        self._cohorts: Dict[str, Dict[str, Any]] = {}
        self._signals: List[Dict[str, Any]] = []
        self._cohort_weights: Dict[str, float] = {}
        self._history: List[Dict[str, Any]] = []
        self._load_state()

    def _load_state(self):
        """Load persisted state."""
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                self._cohorts = data.get("cohorts", {})
                self._cohort_weights = data.get("cohort_weights", {})
                self._history = data.get("history", [])
            except Exception as e:
                logger.warning("Failed to load cohort state: %s", e)

        if self.signals_file.exists():
            try:
                for line in self.signals_file.read_text().strip().split("\n"):
                    if line.strip():
                        self._signals.append(json.loads(line))
                if len(self._signals) > 2000:
                    self._signals = self._signals[-2000:]
            except Exception as e:
                logger.warning("Failed to load cohort signals: %s", e)

    def _save_state(self):
        """Persist state."""
        data = {
            "cohorts": self._cohorts,
            "cohort_weights": self._cohort_weights,
            "history": self._history[-100:],
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        self.state_file.write_text(json.dumps(data, indent=2))

    def register_cohort(self, cohort_id: str, lookback_hours: int = 168):
        """Register a cohort with a specific lookback window.

        Args:
            cohort_id: Unique cohort identifier (e.g., "short_window", "long_window").
            lookback_hours: Rolling window in hours for scoring signals.
        """
        self._cohorts[cohort_id] = {
            "lookback_hours": lookback_hours,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        # Initialize equal weights
        if not self._cohort_weights:
            for cid in self._cohorts:
                self._cohort_weights[cid] = 1.0 / len(self._cohorts)
        self._save_state()
        logger.info("Registered cohort: %s (lookback=%dh)", cohort_id, lookback_hours)

    def score_signal(
        self,
        cohort_id: str,
        symbol: str,
        direction: int,
        conviction: float,
        entry_price: float,
    ):
        """Record a signal from a cohort for later scoring.

        Args:
            cohort_id: Which cohort generated this signal.
            symbol: Trading pair.
            direction: 1 = long, -1 = short.
            conviction: 0-100 confidence.
            entry_price: Price at signal time.
        """
        if cohort_id not in self._cohorts:
            self.register_cohort(cohort_id)

        record = {
            "cohort": cohort_id,
            "symbol": symbol,
            "direction": direction,
            "conviction": conviction,
            "entry_price": entry_price,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "exit_price": None,
            "exit_time": None,
            "is_hit": None,
            "weighted_return": None,
        }
        self._signals.append(record)

        with open(self.signals_file, "a") as f:
            f.write(json.dumps(record) + "\n")

    def score_outcome(self, symbol: str, exit_price: float) -> List[Dict[str, Any]]:
        """Score all open signals for a symbol.

        Args:
            symbol: Trading pair.
            exit_price: Resolution price.

        Returns:
            List of scored records.
        """
        scored = []
        now = datetime.now(timezone.utc).isoformat()

        for rec in self._signals:
            if rec["symbol"] == symbol and rec["exit_price"] is None:
                actual_return = (exit_price - rec["entry_price"]) / rec["entry_price"]
                conviction_factor = rec["conviction"] / 100.0

                if rec["direction"] == 1:
                    weighted_return = conviction_factor * actual_return
                    is_hit = actual_return > 0
                else:
                    weighted_return = conviction_factor * (-actual_return)
                    is_hit = actual_return < 0

                rec["exit_price"] = exit_price
                rec["exit_time"] = now
                rec["is_hit"] = is_hit
                rec["weighted_return"] = weighted_return
                scored.append(rec)

        return scored

    def calculate_cohort_metrics(self, cohort_id: str) -> CohortMetrics:
        """Calculate accuracy metrics for a cohort over its rolling window.

        Args:
            cohort_id: Cohort to score.

        Returns:
            CohortMetrics with hit_rate, sharpe, etc.
        """
        config = self._cohorts.get(cohort_id, {})
        lookback_hours = config.get("lookback_hours", 168)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        cutoff_str = cutoff.isoformat()

        # Filter scored signals for this cohort within window
        cohort_signals = [
            s
            for s in self._signals
            if s["cohort"] == cohort_id
            and s["exit_price"] is not None
            and s["exit_time"]
            and s["exit_time"] >= cutoff_str
        ]

        n = len(cohort_signals)
        if n == 0:
            return CohortMetrics(
                cohort_id=cohort_id,
                hit_rate=0.5,
                sharpe=0.0,
                n_signals=0,
                mean_return=0.0,
                window_start=cutoff_str,
                window_end=datetime.now(timezone.utc).isoformat(),
            )

        # Hit rate
        hits = sum(1 for s in cohort_signals if s["is_hit"])
        hit_rate = hits / n

        # Sharpe of weighted returns
        returns = [s["weighted_return"] for s in cohort_signals if s["weighted_return"] is not None]
        if len(returns) < 2:
            sharpe = 0.0
            mean_ret = returns[0] if returns else 0.0
        else:
            mean_ret = sum(returns) / len(returns)
            variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
            std_ret = math.sqrt(variance) if variance > 0 else 0.0001
            sharpe = (mean_ret / std_ret) * math.sqrt(365) if std_ret > 0.0001 else 0.0

        return CohortMetrics(
            cohort_id=cohort_id,
            hit_rate=round(hit_rate, 4),
            sharpe=round(sharpe, 4),
            n_signals=n,
            mean_return=round(mean_ret, 6),
            window_start=cutoff_str,
            window_end=datetime.now(timezone.utc).isoformat(),
        )

    def update_weights(self) -> Dict[str, float]:
        """Update cohort weights based on recent accuracy.

        Uses softmax with constraints (min 0.2, max 0.8).

        Returns:
            Updated cohort weights.
        """
        raw_scores = {}

        for cohort_id in self._cohorts:
            metrics = self.calculate_cohort_metrics(cohort_id)
            # Combined score: 50% hit rate + 50% normalized Sharpe
            norm_sharpe = max(0, min(1, (metrics.sharpe + 1) / 2))
            raw_scores[cohort_id] = 0.5 * metrics.hit_rate + 0.5 * norm_sharpe

        # Softmax with constraints
        self._cohort_weights = self._softmax_with_constraints(raw_scores)
        self._save_state()

        logger.info("Updated cohort weights: %s", self._cohort_weights)
        return dict(self._cohort_weights)

    def detect_regime(self) -> str:
        """Detect market regime from cohort weight differential.

        Returns:
            "NOVEL_REGIME" - Short-window outperforming (unusual market)
            "HISTORICAL_REGIME" - Long-window outperforming (classical patterns)
            "MIXED" - Both roughly equal
        """
        weights = self._cohort_weights or {}

        # Find short vs long window cohorts
        short_weight = weights.get("short_window", 0.5)
        long_weight = weights.get("long_window", 0.5)

        # If using different cohort names, find min/max lookback
        if "short_window" not in self._cohorts or "long_window" not in self._cohorts:
            if len(self._cohorts) >= 2:
                cohort_lookbacks = [(cid, cfg.get("lookback_hours", 168)) for cid, cfg in self._cohorts.items()]
                cohort_lookbacks.sort(key=lambda x: x[1])
                short_cid = cohort_lookbacks[0][0]
                long_cid = cohort_lookbacks[-1][0]
                short_weight = weights.get(short_cid, 0.5)
                long_weight = weights.get(long_cid, 0.5)
            else:
                return "MIXED"

        weight_diff = short_weight - long_weight

        if weight_diff > self.REGIME_THRESHOLD:
            regime = "NOVEL_REGIME"
        elif weight_diff < -self.REGIME_THRESHOLD:
            regime = "HISTORICAL_REGIME"
        else:
            regime = "MIXED"

        # Record history
        self._history.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "regime": regime,
                "weight_diff": round(weight_diff, 4),
                "weights": {k: round(v, 4) for k, v in weights.items()},
            }
        )
        if len(self._history) > 500:
            self._history = self._history[-500:]
        self._save_state()

        return regime

    def get_cohort_weights(self) -> Dict[str, float]:
        """Get current cohort weights."""
        return dict(self._cohort_weights)

    def get_regime_history(self, days: int = 30) -> List[Dict[str, Any]]:
        """Get regime detection history."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_str = cutoff.isoformat()
        return [h for h in self._history if h["timestamp"] >= cutoff_str]

    def get_summary(self) -> Dict[str, Any]:
        """Get full regime detector summary."""
        metrics = {}
        for cohort_id in self._cohorts:
            m = self.calculate_cohort_metrics(cohort_id)
            metrics[cohort_id] = asdict(m)

        return {
            "cohorts": list(self._cohorts.keys()),
            "cohort_weights": {k: round(v, 4) for k, v in self._cohort_weights.items()},
            "current_regime": self.detect_regime(),
            "cohort_metrics": metrics,
            "total_signals": len(self._signals),
            "scored_signals": sum(1 for s in self._signals if s["exit_price"] is not None),
        }

    def _softmax_with_constraints(self, scores: Dict[str, float]) -> Dict[str, float]:
        """Apply softmax with min/max weight constraints."""
        if not scores:
            return {}

        # Softmax
        max_score = max(scores.values())
        exp_scores = {k: math.exp(v - max_score) for k, v in scores.items()}
        total = sum(exp_scores.values())
        weights = {k: v / total for k, v in exp_scores.items()}

        # Apply floor
        for cohort in weights:
            if weights[cohort] < self.MIN_WEIGHT:
                weights[cohort] = self.MIN_WEIGHT

        # Renormalize
        total = sum(weights.values())
        weights = {k: v / total for k, v in weights.items()}

        # Apply ceiling
        for cohort in weights:
            if weights[cohort] > self.MAX_WEIGHT:
                weights[cohort] = self.MAX_WEIGHT

        # Final renormalize
        total = sum(weights.values())
        weights = {k: v / total for k, v in weights.items()}

        return weights
