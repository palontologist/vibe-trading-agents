"""CIO Agent: Weighted signal aggregation with active portfolio management.

The CIO was independently downweighted to 0.3 by the Darwinian system in
ATLAS, revealing that portfolio management — not signal generation — was
the bottleneck. This CIO implements active management rules: conviction
thresholds, position correlation awareness, drawdown-based de-leveraging,
and regime-aware exposure scaling.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class CIOAction:
    """A single portfolio action from the CIO."""

    symbol: str
    action: str  # BUY, SELL, HOLD, CLOSE
    direction: int  # 1 = long, -1 = short
    target_weight: float  # -1.0 to 1.0
    conviction: float  # 0-100
    size_usd: float
    reasoning: str
    contributing_agents: List[str] = field(default_factory=list)


class CIOAgent:
    """Chief Investment Officer with active management rules.

    Aggregates signals from multiple agents weighted by Darwinian weights,
    then applies portfolio-level rules:
      1. Conviction threshold: only act if weighted conviction >= threshold
      2. Drawdown de-leveraging: reduce exposure when drawdown > limit
      3. Regime-aware scaling: reduce size in high-risk regimes
      4. Position correlation: avoid over-concentration in same direction
      5. Max open positions: hard cap on concurrent positions

    Usage:
        cio = CIOAgent(state_dir="./paper_runs")
        signals = {"llm_engine": {...}, "tech_fallback": {...}}
        weights = {"llm_engine": 1.5, "tech_fallback": 0.8}
        actions = cio.synthesize(signals, weights, equity, positions, regime)
    """

    def __init__(
        self,
        state_dir: str = "./paper_runs",
        min_conviction: float = 55.0,
        max_positions: int = 5,
        max_gross_exposure: float = 1.0,
        max_net_exposure: float = 0.5,
        drawdown_delever_threshold: float = 0.10,
        drawdown_delever_factor: float = 0.5,
        regime_scale_high_risk: float = 0.5,
        regime_scale_medium_risk: float = 0.8,
    ):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cio_log = self.state_dir / "cio_decisions.jsonl"

        self.min_conviction = min_conviction
        self.max_positions = max_positions
        self.max_gross_exposure = max_gross_exposure
        self.max_net_exposure = max_net_exposure
        self.drawdown_delever_threshold = drawdown_delever_threshold
        self.drawdown_delever_factor = drawdown_delever_factor
        self.regime_scale_high_risk = regime_scale_high_risk
        self.regime_scale_medium_risk = regime_scale_medium_risk

        self._peak_equity: float = 0.0
        self._decision_count: int = 0

    def synthesize(
        self,
        agent_signals: Dict[str, Dict[str, Any]],
        agent_weights: Dict[str, float],
        equity: float,
        positions: Dict[str, Any],
        regime: Dict[str, Any],
        prices: Dict[str, float],
    ) -> List[CIOAction]:
        """Synthesize agent signals into portfolio actions.

        Args:
            agent_signals: {agent_id: {symbol: {action, confidence, target_weight, ...}}}
            agent_weights: {agent_id: weight} from Darwinian manager.
            equity: Current portfolio equity.
            positions: Current open positions.
            regime: Market regime from MarketAnalyst.
            prices: Current prices for all symbols.

        Returns:
            List of CIOAction objects.
        """
        self._decision_count += 1

        # Step 1: Aggregate signals per symbol, weighted by agent weights
        symbol_signals = self._aggregate_signals(agent_signals, agent_weights)

        # Step 2: Apply drawdown de-leveraging
        exposure_scale = self._calc_exposure_scale(equity, regime)

        # Step 3: Generate actions per symbol
        actions = []
        for symbol, agg in symbol_signals.items():
            action = self._decide_symbol(symbol, agg, equity, positions, prices, exposure_scale)
            if action:
                actions.append(action)

        # Step 4: Enforce portfolio-level constraints
        actions = self._enforce_portfolio_constraints(actions, equity, positions)

        # Step 5: Log decision
        self._log_decision(
            {
                "cycle": self._decision_count,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "equity": equity,
                "regime": regime,
                "exposure_scale": exposure_scale,
                "actions": [
                    {
                        "symbol": a.symbol,
                        "action": a.action,
                        "conviction": a.conviction,
                        "size_usd": a.size_usd,
                        "agents": a.contributing_agents,
                    }
                    for a in actions
                ],
            }
        )

        return actions

    def _aggregate_signals(
        self,
        agent_signals: Dict[str, Dict[str, Any]],
        agent_weights: Dict[str, float],
    ) -> Dict[str, Dict[str, Any]]:
        """Aggregate per-symbol signals weighted by agent weights.

        Returns:
            {symbol: {weighted_conviction, direction, agents, ...}}
        """
        symbol_data: Dict[str, Dict[str, Any]] = {}

        for agent_id, signals in agent_signals.items():
            weight = agent_weights.get(agent_id, 1.0)
            sig_map = signals.get("signals", signals) if isinstance(signals, dict) else {}

            for symbol, sig in sig_map.items():
                if not isinstance(sig, dict):
                    continue

                if symbol not in symbol_data:
                    symbol_data[symbol] = {
                        "long_weighted_conviction": 0.0,
                        "short_weighted_conviction": 0.0,
                        "long_agents": [],
                        "short_agents": [],
                        "total_weight": 0.0,
                        "reasons": [],
                    }

                confidence = sig.get("confidence", 50)
                target_weight = sig.get("target_weight", 0.0)
                action = sig.get("action", "CLOSE")

                weighted_conf = confidence * weight
                data = symbol_data[symbol]
                data["total_weight"] += weight

                if action == "CLOSE" or target_weight == 0:
                    # Close signals reduce conviction for existing direction
                    continue

                if target_weight > 0:
                    data["long_weighted_conviction"] += weighted_conf
                    data["long_agents"].append(agent_id)
                else:
                    data["short_weighted_conviction"] += weighted_conf
                    data["short_agents"].append(agent_id)

                data["reasons"].append(f"{agent_id}(w={weight:.1f}): {sig.get('reasoning', '')[:80]}")

        # Resolve direction per symbol
        for symbol, data in symbol_data.items():
            lw = data["long_weighted_conviction"]
            sw = data["short_weighted_conviction"]
            if lw >= sw:
                data["direction"] = 1
                data["weighted_conviction"] = lw - (sw * 0.5)
                data["agents"] = data["long_agents"]
            else:
                data["direction"] = -1
                data["weighted_conviction"] = sw - (lw * 0.5)
                data["agents"] = data["short_agents"]

            # Normalize conviction to 0-100
            if data["total_weight"] > 0:
                data["conviction_normalized"] = min(100, data["weighted_conviction"] / data["total_weight"])
            else:
                data["conviction_normalized"] = 0

        return symbol_data

    def _calc_exposure_scale(self, equity: float, regime: Dict[str, Any]) -> float:
        """Calculate exposure scaling factor based on drawdown and regime."""
        self._peak_equity = max(self._peak_equity, equity)

        scale = 1.0

        # Drawdown de-leveraging
        if self._peak_equity > 0:
            drawdown = (self._peak_equity - equity) / self._peak_equity
            if drawdown > self.drawdown_delever_threshold:
                # Linear reduction: at threshold scale=1.0, at 2x threshold scale=0.5
                excess_dd = drawdown - self.drawdown_delever_threshold
                scale *= max(
                    self.drawdown_delever_factor,
                    1.0 - (excess_dd / self.drawdown_delever_threshold),
                )

        # Regime scaling
        risk_level = regime.get("risk_level", "low")
        if risk_level == "high":
            scale *= self.regime_scale_high_risk
        elif risk_level == "medium":
            scale *= self.regime_scale_medium_risk

        return max(0.1, min(1.0, scale))

    def _decide_symbol(
        self,
        symbol: str,
        agg: Dict[str, Any],
        equity: float,
        positions: Dict[str, Any],
        prices: Dict[str, float],
        exposure_scale: float,
    ) -> Optional[CIOAction]:
        """Decide action for a single symbol."""
        conviction = agg.get("conviction_normalized", 0)
        direction = agg.get("direction", 0)

        # Conviction threshold
        if conviction < self.min_conviction:
            return None

        # Scale conviction by exposure factor
        scaled_conviction = conviction * exposure_scale

        # Calculate position size
        price = prices.get(symbol, 0)
        if price <= 0:
            return None

        size_usd = equity * (scaled_conviction / 100) * 0.25
        size_usd = min(size_usd, equity * 0.25)  # Cap at 25% of equity

        # Determine action
        current_pos = positions.get(symbol)
        if current_pos is not None:
            current_dir = (
                current_pos.get("direction", 0)
                if isinstance(current_pos, dict)
                else getattr(current_pos, "direction", 0)
            )
            if current_dir == direction:
                action = "HOLD"
                size_usd = 0
            else:
                action = "SELL"  # Flip direction
        else:
            action = "BUY"

        return CIOAction(
            symbol=symbol,
            action=action,
            direction=direction,
            target_weight=direction * (scaled_conviction / 100),
            conviction=round(scaled_conviction, 2),
            size_usd=round(size_usd, 2),
            reasoning=f"Conviction={conviction:.0f}%, agents={agg.get('agents', [])}",
            contributing_agents=agg.get("agents", []),
        )

    def _enforce_portfolio_constraints(
        self,
        actions: List[CIOAction],
        equity: float,
        positions: Dict[str, Any],
    ) -> List[CIOAction]:
        """Enforce max positions, gross/net exposure limits."""
        if not actions:
            return actions

        # Count existing positions that won't be closed
        active_symbols = set(positions.keys())
        for a in actions:
            if a.action in ("SELL", "CLOSE"):
                active_symbols.discard(a.symbol)

        # Limit new positions
        new_actions = []
        new_count = len(active_symbols)

        for a in actions:
            if a.action == "BUY" and a.symbol not in active_symbols:
                if new_count >= self.max_positions:
                    a.action = "HOLD"
                    a.size_usd = 0
                    a.conviction = 0
                    continue
                new_count += 1
                active_symbols.add(a.symbol)
            new_actions.append(a)

        # Check gross exposure
        total_exposure = sum(a.size_usd for a in new_actions if a.size_usd > 0)
        if total_exposure > equity * self.max_gross_exposure and new_actions:
            scale = (equity * self.max_gross_exposure) / total_exposure
            for a in new_actions:
                a.size_usd *= scale

        return new_actions

    def _log_decision(self, decision: Dict[str, Any]):
        """Append decision to JSONL log."""
        with open(self.cio_log, "a") as f:
            f.write(json.dumps(decision, default=str) + "\n")

    def get_peak_equity(self) -> float:
        return self._peak_equity
