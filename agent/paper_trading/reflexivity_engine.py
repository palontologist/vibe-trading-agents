"""Soros Reflexivity Engine: Five feedback loops + reversal detection.

Markets don't just reflect reality — they change it. This engine models
five reflexive feedback loops and detects when consensus becomes fragile.

Five loops:
  1. Price → Fundamentals: Stock drops trigger credit downgrades, talent flight
  2. P&L → Behaviour: Fund drawdown → forced selling cascade
  3. Narrative → Flows: Analyst convergence → retail follows → contrarian reversal
  4. Market → Policy: Equity drawdown → central bank signals easing
  5. Reflexive Reversal Detection: 5+ rounds in one direction = extreme

Maximum consensus = maximum fragility.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ReflexiveLoop:
    """A single reflexive feedback loop."""

    loop_type: str  # price_fundamentals, pnl_behavior, narrative_flows, market_policy
    direction: str  # positive (reinforcing) or negative (counteracting)
    rounds: int  # Consecutive rounds in same direction
    strength: float  # 0-1, how strong the feedback is
    assets_affected: List[str]
    description: str
    is_extreme: bool  # True if rounds >= 5 (reversal risk)
    reversal_probability: float  # 0-1


@dataclass
class ReflexivitySignal:
    """A signal from the reflexivity engine."""

    timestamp: str
    loops_active: int
    loops_extreme: int
    reversal_risks: List[Dict[str, Any]]
    crowded_trades: List[Dict[str, Any]]
    regime_fragility: float  # 0-1, higher = more fragile
    recommended_action: str  # "reduce_exposure", "normal", "increase_conviction"


class ReflexivityEngine:
    """Model reflexive feedback loops and detect crowded trades.

    Tracks five types of feedback loops across market participants. When
    a loop runs for 5+ consecutive rounds in one direction, it's flagged
    as a reflexive extreme with high reversal probability.

    Usage:
        engine = ReflexivityEngine(state_dir="./paper_runs")
        engine.assess_price_fundamentals("BTC-USDT", -0.18, "sharp_drawdown")
        engine.assess_pnl_behavior("macro_fund", -0.12, "drawdown")
        engine.assess_narrative("bullish_BTC", convergence=0.85, rounds=4)
        signal = engine.evaluate()
    """

    REVERSAL_THRESHOLD = 5  # Rounds before flagging as extreme
    EXTREME_THRESHOLD = 8  # Rounds for critical warning

    def __init__(self, state_dir: str = "./paper_runs"):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "reflexivity_state.json"

        # Active feedback loops
        self._loops: Dict[str, Dict[str, Any]] = {}
        self._narrative_tracker: Dict[str, Dict[str, Any]] = {}
        self._price_history: Dict[str, List[float]] = {}
        self._fund_pnl: Dict[str, List[float]] = {}
        self._history: List[ReflexivitySignal] = []

        self._load_state()

    def _load_state(self):
        """Load persisted state."""
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                self._loops = data.get("loops", {})
                self._narrative_tracker = data.get("narrative_tracker", {})
                self._price_history = data.get("price_history", {})
                self._fund_pnl = data.get("fund_pnl", {})
            except Exception as e:
                logger.warning("Failed to load reflexivity state: %s", e)

    def _save_state(self):
        """Persist state."""
        data = {
            "loops": self._loops,
            "narrative_tracker": self._narrative_tracker,
            "price_history": {k: v[-50:] for k, v in self._price_history.items()},
            "fund_pnl": {k: v[-50:] for k, v in self._fund_pnl.items()},
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        self.state_file.write_text(json.dumps(data, indent=2, default=str))

    # --- Loop 1: Price → Fundamentals ---

    def assess_price_fundamentals(
        self, symbol: str, price_change_pct: float, context: str = ""
    ) -> Optional[ReflexiveLoop]:
        """Track price → fundamentals feedback loop.

        Price drops >15% trigger credit downgrade risk, talent flight,
        customer renegotiations. Rises >20% trigger cheap capital, talent
        attraction, customer confidence.

        Args:
            symbol: Trading pair.
            price_change_pct: Price change as decimal (e.g., -0.15 for -15%).
            context: Additional context about the move.

        Returns:
            ReflexiveLoop if threshold breached, None otherwise.
        """
        self._price_history.setdefault(symbol, []).append(price_change_pct)
        history = self._price_history[symbol][-20:]

        # Check for sustained directional moves
        cumulative = sum(history)
        rounds = 0
        direction = "rising" if cumulative > 0 else "falling"

        for change in history:
            if direction == "falling" and change < 0:
                rounds += 1
            elif direction == "rising" and change > 0:
                rounds += 1
            else:
                break

        strength = min(1.0, abs(cumulative) / 0.5)  # 50% cumulative = max strength
        is_extreme = rounds >= self.REVERSAL_THRESHOLD

        if abs(price_change_pct) < 0.05 and not is_extreme:
            return None

        loop_id = f"price_fundamentals_{symbol}"
        is_extreme = rounds >= self.REVERSAL_THRESHOLD
        reversal_prob = min(0.8, 0.1 + rounds * 0.08) if is_extreme else 0.05

        loop = ReflexiveLoop(
            loop_type="price_fundamentals",
            direction=direction,
            rounds=rounds,
            strength=strength,
            assets_affected=[symbol],
            description=f"{symbol}: {direction} {abs(cumulative) * 100:.1f}% cumulative "
            f"over {rounds} rounds. {'REVERSAL RISK' if is_extreme else 'Monitoring'} "
            f"({context})",
            is_extreme=is_extreme,
            reversal_probability=reversal_prob,
        )
        self._loops[loop_id] = asdict(loop)
        return loop

    # --- Loop 2: P&L → Behaviour ---

    def assess_pnl_behavior(
        self, fund_id: str, cumulative_pnl_pct: float, context: str = ""
    ) -> Optional[ReflexiveLoop]:
        """Track P&L → behavior feedback loop.

        Fund drawdown >10% → forced selling cascade. Gains >15% → increased
        position sizes and concentrated bets.

        Args:
            fund_id: Fund or strategy identifier.
            cumulative_pnl_pct: Cumulative PnL as decimal.
            context: Additional context.

        Returns:
            ReflexiveLoop if threshold breached, None otherwise.
        """
        self._fund_pnl.setdefault(fund_id, []).append(cumulative_pnl_pct)

        if abs(cumulative_pnl_pct) < 0.05:
            return None

        direction = "drawdown" if cumulative_pnl_pct < 0 else "gains"
        rounds = len(
            [
                p
                for p in self._fund_pnl[fund_id][-10:]
                if (p < 0 and cumulative_pnl_pct < 0) or (p > 0 and cumulative_pnl_pct > 0)
            ]
        )

        strength = min(1.0, abs(cumulative_pnl_pct) / 0.20)
        is_extreme = abs(cumulative_pnl_pct) > 0.10 and rounds >= 3
        reversal_prob = 0.3 if is_extreme else 0.05

        behavior_note = ""
        if cumulative_pnl_pct < -0.10:
            behavior_note = "Forced selling cascade likely"
        elif cumulative_pnl_pct < -0.05:
            behavior_note = "Risk reduction expected"
        elif cumulative_pnl_pct > 0.15:
            behavior_note = "Increased position sizes, concentrated bets"
        elif cumulative_pnl_pct > 0.05:
            behavior_note = "Confidence increasing"

        loop_id = f"pnl_behavior_{fund_id}"
        loop = ReflexiveLoop(
            loop_type="pnl_behavior",
            direction=direction,
            rounds=rounds,
            strength=strength,
            assets_affected=[],
            description=f"{fund_id}: {direction} {abs(cumulative_pnl_pct) * 100:.1f}%. {behavior_note} ({context})",
            is_extreme=is_extreme,
            reversal_probability=reversal_prob,
        )
        self._loops[loop_id] = asdict(loop)
        return loop

    # --- Loop 3: Narrative → Flows ---

    def assess_narrative(self, narrative: str, convergence: float, rounds: int = 1) -> Optional[ReflexiveLoop]:
        """Track narrative → flows feedback loop.

        3+ analysts converge on thesis → retail flow follows. After 5 rounds
        of consensus, contrarian reversals emerge.

        Args:
            narrative: Narrative identifier (e.g., "bullish_BTC", "AI_bubble").
            convergence: 0-1, how much agreement there is.
            rounds: Consecutive rounds of this narrative.

        Returns:
            ReflexiveLoop if narrative is becoming crowded.
        """
        tracker = self._narrative_tracker.get(
            narrative,
            {
                "convergence": 0.0,
                "rounds": 0,
                "peak_convergence": 0.0,
            },
        )

        tracker["convergence"] = convergence
        tracker["rounds"] = max(tracker["rounds"], rounds)
        tracker["peak_convergence"] = max(tracker["peak_convergence"], convergence)
        tracker["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._narrative_tracker[narrative] = tracker

        if convergence < 0.5:
            return None

        is_extreme = rounds >= self.REVERSAL_THRESHOLD and convergence > 0.7
        reversal_prob = min(0.6, 0.1 + rounds * 0.06 + convergence * 0.2)

        loop_id = f"narrative_{narrative}"
        loop = ReflexiveLoop(
            loop_type="narrative_flows",
            direction="positive" if "bull" in narrative.lower() else "negative",
            rounds=rounds,
            strength=convergence,
            assets_affected=[],
            description=f"Narrative '{narrative}': convergence={convergence:.0%}, "
            f"{rounds} rounds. "
            f"{'CROWDED TRADE - REVERSAL RISK' if is_extreme else 'Building consensus'}",
            is_extreme=is_extreme,
            reversal_probability=reversal_prob if is_extreme else 0.05,
        )
        self._loops[loop_id] = asdict(loop)
        return loop

    # --- Loop 4: Market → Policy ---

    def assess_market_policy(
        self, equity_drawdown_pct: float, asset_class: str = "equities"
    ) -> Optional[ReflexiveLoop]:
        """Track market → policy feedback loop.

        Equity drawdown >15% → central bank signals easing. Oil >$130 →
        strategic reserve releases.

        Args:
            equity_drawdown_pct: Market drawdown as decimal.
            asset_class: Asset class being monitored.

        Returns:
            ReflexiveLoop if policy response likely.
        """
        if abs(equity_drawdown_pct) < 0.05:
            return None

        is_extreme = abs(equity_drawdown_pct) > 0.15
        rounds = max(1, int(abs(equity_drawdown_pct) * 20))

        policy_response = ""
        if equity_drawdown_pct < -0.15:
            policy_response = "Central bank easing likely"
        elif equity_drawdown_pct < -0.10:
            policy_response = "Central bank monitoring closely"
        elif equity_drawdown_pct > 0.10:
            policy_response = "Tightening risk if overheating"

        loop_id = f"market_policy_{asset_class}"
        loop = ReflexiveLoop(
            loop_type="market_policy",
            direction="negative" if equity_drawdown_pct < 0 else "positive",
            rounds=rounds,
            strength=min(1.0, abs(equity_drawdown_pct) / 0.20),
            assets_affected=[],
            description=f"{asset_class}: drawdown={equity_drawdown_pct * 100:.1f}%. {policy_response}",
            is_extreme=is_extreme,
            reversal_probability=0.2 if is_extreme else 0.05,
        )
        self._loops[loop_id] = asdict(loop)
        return loop

    # --- Loop 5: Reflexive Reversal Detection ---

    def evaluate(self) -> ReflexivitySignal:
        """Evaluate all active loops and generate a reflexivity signal.

        Returns:
            ReflexivitySignal with reversal risks, crowded trades, fragility.
        """
        loops = list(self._loops.values())
        active = [l for l in loops if l.get("rounds", 0) >= 2]
        extreme = [l for l in active if l.get("is_extreme", False)]

        # Identify crowded trades (narrative loops with high convergence)
        crowded = [l for l in loops if l.get("loop_type") == "narrative_flows" and l.get("strength", 0) > 0.7]

        # Identify reversal risks (any extreme loop)
        reversal_risks = [
            {
                "type": l.get("loop_type"),
                "description": l.get("description", ""),
                "reversal_probability": l.get("reversal_probability", 0),
                "rounds": l.get("rounds", 0),
            }
            for l in extreme
        ]

        # Calculate regime fragility
        if not active:
            fragility = 0.0
        else:
            avg_reversal_prob = sum(l.get("reversal_probability", 0) for l in extreme) / max(len(extreme), 1)
            fragility = min(1.0, avg_reversal_prob * (1 + len(extreme) * 0.2))

        # Determine recommended action
        if fragility > 0.5:
            action = "reduce_exposure"
        elif fragility < 0.15 and len(active) < 2:
            action = "increase_conviction"
        else:
            action = "normal"

        signal = ReflexivitySignal(
            timestamp=datetime.now(timezone.utc).isoformat(),
            loops_active=len(active),
            loops_extreme=len(extreme),
            reversal_risks=reversal_risks,
            crowded_trades=[
                {
                    "narrative": l.get("description", "")[:100],
                    "strength": l.get("strength", 0),
                    "reversal_probability": l.get("reversal_probability", 0),
                }
                for l in crowded
            ],
            regime_fragility=round(fragility, 4),
            recommended_action=action,
        )

        self._history.append(signal)
        if len(self._history) > 200:
            self._history = self._history[-200:]

        self._save_state()

        if extreme:
            logger.warning(
                "Reflexivity: %d extreme loops detected, fragility=%.0f%%, action=%s",
                len(extreme),
                fragility * 100,
                action,
            )

        return signal

    def get_active_loops(self) -> List[Dict[str, Any]]:
        """Get all active feedback loops."""
        return [l for l in self._loops.values() if l.get("rounds", 0) >= 2]

    def get_crowded_trades(self) -> List[Dict[str, Any]]:
        """Get all crowded trades (high-convergence narratives)."""
        return [{"narrative": k, **v} for k, v in self._narrative_tracker.items() if v.get("convergence", 0) > 0.7]

    def get_fragility_history(self, last_n: int = 20) -> List[Dict[str, Any]]:
        """Get recent fragility readings."""
        return [
            {
                "timestamp": s.timestamp,
                "fragility": s.regime_fragility,
                "loops_extreme": s.loops_extreme,
                "action": s.recommended_action,
            }
            for s in self._history[-last_n:]
        ]

    def reset_loop(self, loop_id: str):
        """Reset a specific loop (e.g., after a reversal occurs)."""
        if loop_id in self._loops:
            loop = self._loops[loop_id]
            loop["rounds"] = 0
            loop["is_extreme"] = False
            loop["reversal_probability"] = 0.05
            logger.info("Reset reflexivity loop: %s", loop_id)
