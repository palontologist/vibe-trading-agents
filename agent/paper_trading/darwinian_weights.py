"""Darwinian weight adjustment for agents.

Top-quartile agents get weight * 1.05 daily. Bottom-quartile get * 0.95.
Weights are bounded between 0.3 (floor) and 2.5 (ceiling).

The CIO weights each agent's recommendation proportionally. High-weight
agents (2.5) have 8x the influence of minimum-weight agents (0.3).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class DarwinianWeightManager:
    """Evolve agent weights based on rolling performance.

    Weights are updated daily based on agent Sharpe quartiles:
      - Top quartile: weight *= 1.05 (up to 2.5)
      - Bottom quartile: weight *= 0.95 (down to 0.3)
      - Middle: unchanged

    The CIO then weights each agent's recommendation by these scores.

    Usage:
        manager = DarwinianWeightManager(state_dir="./paper_runs")
        manager.register_agent("llm_engine", 1.0)
        metrics = {"llm_engine": {"sharpe": 1.2}, "tech_fallback": {"sharpe": -0.5}}
        manager.update_weights(metrics)
        weight = manager.get_weight("llm_engine")
    """

    MIN_WEIGHT = 0.3
    MAX_WEIGHT = 2.5
    DEFAULT_WEIGHT = 1.0
    TOP_QUARTILE_MULTIPLIER = 1.05
    BOTTOM_QUARTILE_MULTIPLIER = 0.95

    def __init__(self, state_dir: str = "./paper_runs"):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.weights_file = self.state_dir / "darwinian_weights.json"
        self._weights: Dict[str, float] = {}
        self._history: List[Dict[str, Any]] = []
        self._load_state()

    def _load_state(self):
        """Load persisted weight state."""
        if self.weights_file.exists():
            try:
                data = json.loads(self.weights_file.read_text())
                self._weights = data.get("weights", {})
                self._history = data.get("history", [])
                logger.info("Loaded Darwinian weights: %d agents", len(self._weights))
            except Exception as e:
                logger.warning("Failed to load Darwinian weights: %s", e)

    def _save_state(self):
        """Persist weight state."""
        data = {
            "weights": self._weights,
            "history": self._history[-200:],
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        self.weights_file.write_text(json.dumps(data, indent=2))

    def register_agent(self, agent_id: str, weight: float = DEFAULT_WEIGHT):
        """Register an agent with initial weight."""
        self._weights[agent_id] = max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weight))
        self._save_state()

    def get_weight(self, agent_id: str) -> float:
        """Get current weight for an agent."""
        return self._weights.get(agent_id, self.DEFAULT_WEIGHT)

    def get_all_weights(self) -> Dict[str, float]:
        """Get all agent weights."""
        return dict(self._weights)

    def update_weights(
        self,
        agent_metrics: Dict[str, Dict[str, float]],
        min_signals: int = 3,
    ) -> Dict[str, Dict[str, float]]:
        """Update weights based on agent Sharpe quartiles.

        Args:
            agent_metrics: {agent_id: {"sharpe": float, "n_signals": int, ...}}
            min_signals: Minimum scored signals to be eligible for adjustment.

        Returns:
            Dict of {agent_id: {"old": float, "new": float, "quartile": str}}
        """
        if not agent_metrics:
            return {}

        # Filter to agents with enough signals
        eligible = {aid: m for aid, m in agent_metrics.items() if m.get("n_signals", 0) >= min_signals}

        if len(eligible) < 4:
            # Not enough agents for quartile split, use median split
            if len(eligible) < 2:
                return {}
            sorted_agents = sorted(eligible.items(), key=lambda x: x[1].get("sharpe", 0))
            mid = len(sorted_agents) // 2
            bottom_ids = {a[0] for a in sorted_agents[:mid]}
            top_ids = {a[0] for a in sorted_agents[mid:]}
        else:
            # Quartile split
            sorted_agents = sorted(eligible.items(), key=lambda x: x[1].get("sharpe", 0))
            q = len(sorted_agents) // 4
            bottom_ids = {a[0] for a in sorted_agents[:q]}
            top_ids = {a[0] for a in sorted_agents[3 * q :]}

        updates = {}

        for agent_id in self._weights:
            old_weight = self._weights[agent_id]
            new_weight = old_weight
            quartile = "middle"

            if agent_id in top_ids:
                new_weight = old_weight * self.TOP_QUARTILE_MULTIPLIER
                quartile = "top"
            elif agent_id in bottom_ids:
                new_weight = old_weight * self.BOTTOM_QUARTILE_MULTIPLIER
                quartile = "bottom"

            # Clamp
            new_weight = max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, new_weight))

            if abs(new_weight - old_weight) > 0.001:
                self._weights[agent_id] = new_weight
                updates[agent_id] = {
                    "old": round(old_weight, 4),
                    "new": round(new_weight, 4),
                    "quartile": quartile,
                    "sharpe": eligible.get(agent_id, {}).get("sharpe", 0),
                }
                logger.info(
                    "Weight update: %s %.2f -> %.2f (%s quartile, sharpe=%.2f)",
                    agent_id,
                    old_weight,
                    new_weight,
                    quartile,
                    eligible.get(agent_id, {}).get("sharpe", 0),
                )

        # Record history
        self._history.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "updates": updates,
                "all_weights": dict(self._weights),
            }
        )

        self._save_state()
        return updates

    def get_weighted_signal(self, signals: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Apply Darwinian weights to agent signals.

        Each agent's signal conviction is multiplied by their weight.

        Args:
            signals: {agent_id: {"conviction": float, "direction": int, ...}}

        Returns:
            Signals with "weighted_conviction" field added.
        """
        result = {}
        total_weight = 0.0

        for agent_id, sig in signals.items():
            weight = self.get_weight(agent_id)
            conviction = sig.get("conviction", 50)
            weighted_conviction = conviction * weight
            result[agent_id] = {
                **sig,
                "weight": weight,
                "weighted_conviction": round(weighted_conviction, 2),
            }
            total_weight += weight

        # Normalize weighted convictions by total weight
        if total_weight > 0:
            for agent_id in result:
                result[agent_id]["normalized_conviction"] = round(
                    result[agent_id]["weighted_conviction"] / total_weight * 100, 2
                )

        return result

    def get_leaderboard(self) -> List[Dict[str, Any]]:
        """Get agents sorted by weight (highest first)."""
        return sorted(
            [{"agent_id": aid, "weight": round(w, 4)} for aid, w in self._weights.items()],
            key=lambda x: x["weight"],
            reverse=True,
        )

    def get_weight_summary(self) -> Dict[str, Any]:
        """Get weight distribution summary."""
        if not self._weights:
            return {"agents": 0}

        weights = list(self._weights.values())
        return {
            "agents": len(self._weights),
            "max_weight": max(weights),
            "min_weight": min(weights),
            "avg_weight": round(sum(weights) / len(weights), 4),
            "at_ceiling": sum(1 for w in weights if w >= self.MAX_WEIGHT),
            "at_floor": sum(1 for w in weights if w <= self.MIN_WEIGHT),
            "leaderboard": self.get_leaderboard(),
        }
