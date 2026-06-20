"""Per-agent signal performance tracking with Sharpe-based scoring.

Tracks each signal's outcome against actual price movement, calculates
rolling Sharpe ratio per agent, and maintains a persistent scorecard
that feeds into Darwinian weight adjustment.

The agent prompts are the weights being optimized. Sharpe ratio is the
loss function.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SignalRecord:
    """A single signal with its outcome."""

    agent_id: str
    symbol: str
    direction: int  # 1 = long, -1 = short
    conviction: float  # 0-100
    entry_price: float
    entry_time: str
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    actual_return: Optional[float] = None
    weighted_return: Optional[float] = None
    is_hit: Optional[bool] = None
    reasoning: str = ""


class AgentScorecard:
    """Track per-agent signal performance and calculate rolling Sharpe.

    Every signal from every agent is scored against the actual forward
    return. The rolling Sharpe of conviction-weighted returns becomes
    the agent's performance metric.

    Usage:
        scorecard = AgentScorecard(state_dir="./paper_runs")
        scorecard.log_signal("llm_engine", "BTC-USDT", 1, 85, 65000, ...)
        scorecard.score_outcome("BTC-USDT", 66000)
        metrics = scorecard.get_agent_metrics("llm_engine")
    """

    MIN_WEIGHT = 0.3
    MAX_WEIGHT = 2.5
    DEFAULT_WEIGHT = 1.0
    LOOKBACK_CYCLES = 30  # Rolling window for Sharpe calculation

    def __init__(self, state_dir: str = "./paper_runs"):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.scorecard_file = self.state_dir / "agent_scorecard.json"
        self.signals_file = self.state_dir / "agent_signals.jsonl"
        self._signals: List[SignalRecord] = []
        self._agent_weights: Dict[str, float] = {}
        self._load_state()

    def _load_state(self):
        """Load persisted scorecard state."""
        if self.scorecard_file.exists():
            try:
                data = json.loads(self.scorecard_file.read_text())
                self._agent_weights = data.get("agent_weights", {})
                logger.info("Loaded scorecard: %d agents tracked", len(self._agent_weights))
            except Exception as e:
                logger.warning("Failed to load scorecard: %s", e)

        if self.signals_file.exists():
            try:
                for line in self.signals_file.read_text().strip().split("\n"):
                    if line.strip():
                        rec = SignalRecord(**json.loads(line))
                        self._signals.append(rec)
                logger.info("Loaded %d historical signals", len(self._signals))
            except Exception as e:
                logger.warning("Failed to load signals: %s", e)

    def _save_state(self):
        """Persist scorecard state."""
        data = {
            "agent_weights": self._agent_weights,
            "total_signals": len(self._signals),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        self.scorecard_file.write_text(json.dumps(data, indent=2))

    def register_agent(self, agent_id: str, weight: float = DEFAULT_WEIGHT):
        """Register an agent with initial weight."""
        self._agent_weights[agent_id] = max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weight))
        self._save_state()

    def log_signal(
        self,
        agent_id: str,
        symbol: str,
        direction: int,
        conviction: float,
        entry_price: float,
        reasoning: str = "",
    ) -> None:
        """Log a new signal for tracking.

        Args:
            agent_id: Which agent generated this signal.
            symbol: Trading pair.
            direction: 1 for long, -1 for short.
            conviction: 0-100 confidence level.
            entry_price: Price at signal time.
            reasoning: Agent's reasoning text.
        """
        if agent_id not in self._agent_weights:
            self.register_agent(agent_id)

        record = SignalRecord(
            agent_id=agent_id,
            symbol=symbol,
            direction=direction,
            conviction=conviction,
            entry_price=entry_price,
            entry_time=datetime.now(timezone.utc).isoformat(),
            reasoning=reasoning[:200],
        )
        self._signals.append(record)

        with open(self.signals_file, "a") as f:
            f.write(json.dumps(asdict(record)) + "\n")

        # Keep last 1000 signals in memory
        if len(self._signals) > 1000:
            self._signals = self._signals[-1000:]

    def score_outcome(self, symbol: str, exit_price: float) -> List[Dict[str, Any]]:
        """Score all open signals for a symbol against the exit price.

        Args:
            symbol: Trading pair that was resolved.
            exit_price: Price at resolution.

        Returns:
            List of scored records.
        """
        scored = []
        now = datetime.now(timezone.utc).isoformat()

        for rec in self._signals:
            if rec.symbol == symbol and rec.exit_price is None:
                actual_return = (exit_price - rec.entry_price) / rec.entry_price
                conviction_factor = rec.conviction / 100.0

                if rec.direction == 1:
                    weighted_return = conviction_factor * actual_return
                    is_hit = actual_return > 0
                else:
                    weighted_return = conviction_factor * (-actual_return)
                    is_hit = actual_return < 0

                rec.exit_price = exit_price
                rec.exit_time = now
                rec.actual_return = actual_return
                rec.weighted_return = weighted_return
                rec.is_hit = is_hit

                scored.append(asdict(rec))

        self._save_state()
        return scored

    def get_agent_signals(self, agent_id: str, lookback: Optional[int] = None) -> List[SignalRecord]:
        """Get scored signals for an agent within the rolling window."""
        lookback = lookback or self.LOOKBACK_CYCLES
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback * 2)
        cutoff_str = cutoff.isoformat()

        return [
            s
            for s in self._signals
            if s.agent_id == agent_id and s.exit_price is not None and s.exit_time and s.exit_time >= cutoff_str
        ]

    def calculate_agent_sharpe(self, agent_id: str, lookback: Optional[int] = None) -> Dict[str, float]:
        """Calculate rolling Sharpe ratio for an agent.

        Sharpe = mean(conviction-weighted returns) / std(conviction-weighted returns) * sqrt(252)

        Args:
            agent_id: Agent to score.
            lookback: Number of cycles to look back.

        Returns:
            Dict with sharpe, hit_rate, n_signals, mean_return, std_return.
        """
        signals = self.get_agent_signals(agent_id, lookback)

        if len(signals) < 2:
            return {
                "sharpe": 0.0,
                "hit_rate": 0.5,
                "n_signals": len(signals),
                "mean_return": 0.0,
                "std_return": 0.0,
            }

        returns = [s.weighted_return for s in signals if s.weighted_return is not None]
        if not returns:
            return {
                "sharpe": 0.0,
                "hit_rate": 0.5,
                "n_signals": len(signals),
                "mean_return": 0.0,
                "std_return": 0.0,
            }

        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
        std_ret = math.sqrt(variance) if variance > 0 else 0.0001

        # Annualized Sharpe (assuming crypto 365 bars/year for 1H data)
        sharpe = (mean_ret / std_ret) * math.sqrt(365) if std_ret > 0.0001 else 0.0

        hits = sum(1 for s in signals if s.is_hit)
        hit_rate = hits / len(signals)

        return {
            "sharpe": round(sharpe, 4),
            "hit_rate": round(hit_rate, 4),
            "n_signals": len(signals),
            "mean_return": round(mean_ret, 6),
            "std_return": round(std_ret, 6),
        }

    def get_all_agent_metrics(self) -> Dict[str, Dict[str, float]]:
        """Get Sharpe metrics for all registered agents."""
        metrics = {}
        for agent_id in self._agent_weights:
            metrics[agent_id] = self.calculate_agent_sharpe(agent_id)
        return metrics

    def get_worst_agent(self, min_signals: int = 3) -> Optional[str]:
        """Identify the worst-performing agent by Sharpe ratio.

        Args:
            min_signals: Minimum number of scored signals to consider.

        Returns:
            Agent ID with lowest Sharpe, or None if no eligible agents.
        """
        worst_id = None
        worst_sharpe = float("inf")

        for agent_id in self._agent_weights:
            metrics = self.calculate_agent_sharpe(agent_id)
            if metrics["n_signals"] >= min_signals:
                if metrics["sharpe"] < worst_sharpe:
                    worst_sharpe = metrics["sharpe"]
                    worst_id = agent_id

        return worst_id

    def get_agent_weight(self, agent_id: str) -> float:
        """Get current Darwinian weight for an agent."""
        return self._agent_weights.get(agent_id, self.DEFAULT_WEIGHT)

    def get_all_weights(self) -> Dict[str, float]:
        """Get all agent weights."""
        return dict(self._agent_weights)

    def get_scorecard_summary(self) -> Dict[str, Any]:
        """Get full scorecard summary for reporting."""
        metrics = self.get_all_agent_metrics()
        return {
            "agent_weights": self._agent_weights,
            "agent_metrics": metrics,
            "total_signals": len(self._signals),
            "scored_signals": sum(1 for s in self._signals if s.exit_price is not None),
            "agents": list(self._agent_weights.keys()),
        }
