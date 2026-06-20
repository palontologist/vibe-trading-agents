"""Autoresearch Engine: Prompt self-modification based on agent performance.

Inspired by Karpathy's autoresearch. The agent prompts are the weights
being optimized. Each cycle is one training iteration.

Process:
  1. Identify worst-performing agent (lowest Sharpe)
  2. Generate ONE targeted prompt modification via LLM
  3. Test for N cycles
  4. If Sharpe improved: keep (commit)
  5. If Sharpe worsened: revert
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ModificationRecord:
    """Record of a prompt modification experiment."""

    mod_id: str
    agent_id: str
    timestamp: str
    modification: str
    old_prompt: str
    new_prompt: str
    pre_sharpe: float
    post_sharpe: Optional[float] = None
    status: str = "testing"  # testing, kept, reverted
    test_cycles: int = 0
    required_cycles: int = 5


class AutoresearchEngine:
    """Self-improving agent prompts via performance feedback.

    The agent prompts are the weights being optimized. Sharpe ratio is
    the loss function. No GPU needed.

    Usage:
        engine = AutoresearchEngine(state_dir="./paper_runs", prompt_dir="./prompts")
        engine.register_agent("llm_engine", "You are a trading signal engine...")
        worst = engine.identify_worst_agent(scorecard)
        mod = engine.generate_modification(worst, scorecard)
        engine.apply_modification(mod)
        ... run for test cycles ...
        engine.evaluate_modification(mod.mod_id, new_sharpe)
    """

    TEST_CYCLES = 5  # Cycles to test a modification before evaluating

    def __init__(
        self,
        state_dir: str = "./paper_runs",
        prompt_dir: str = "./prompts",
        llm_factory=None,
    ):
        self.state_dir = Path(state_dir)
        self.prompt_dir = Path(prompt_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.prompt_dir.mkdir(parents=True, exist_ok=True)

        self.log_file = self.state_dir / "autoresearch_log.jsonl"
        self._prompts: Dict[str, str] = {}
        self._modifications: Dict[str, ModificationRecord] = {}
        self._llm_factory = llm_factory
        self._load_state()

    def _load_state(self):
        """Load persisted autoresearch state."""
        state_file = self.state_dir / "autoresearch_state.json"
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text())
                self._prompts = data.get("prompts", {})
                for mod_data in data.get("modifications", []):
                    mod = ModificationRecord(**mod_data)
                    self._modifications[mod.mod_id] = mod
                logger.info(
                    "Loaded autoresearch state: %d prompts, %d modifications",
                    len(self._prompts),
                    len(self._modifications),
                )
            except Exception as e:
                logger.warning("Failed to load autoresearch state: %s", e)

    def _save_state(self):
        """Persist autoresearch state."""
        data = {
            "prompts": self._prompts,
            "modifications": [asdict(m) for m in self._modifications.values()],
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        state_file = self.state_dir / "autoresearch_state.json"
        state_file.write_text(json.dumps(data, indent=2))

    def register_agent(self, agent_id: str, prompt: str):
        """Register an agent with its current prompt."""
        self._prompts[agent_id] = prompt
        # Save prompt to file
        prompt_file = self.prompt_dir / f"{agent_id}.txt"
        prompt_file.write_text(prompt)
        self._save_state()
        logger.info("Registered agent prompt: %s (%d bytes)", agent_id, len(prompt))

    def get_prompt(self, agent_id: str) -> Optional[str]:
        """Get current prompt for an agent."""
        return self._prompts.get(agent_id)

    def identify_worst_agent(
        self,
        agent_metrics: Dict[str, Dict[str, float]],
        min_signals: int = 3,
        cooldown_cycles: int = 5,
    ) -> Optional[str]:
        """Identify the worst-performing agent eligible for modification.

        Args:
            agent_metrics: {agent_id: {"sharpe": float, "n_signals": int, ...}}
            min_signals: Minimum scored signals to be eligible.
            cooldown_cycles: Minimum cycles between modifications to same agent.

        Returns:
            Agent ID with lowest Sharpe, or None.
        """
        worst_id = None
        worst_sharpe = float("inf")

        for agent_id, metrics in agent_metrics.items():
            if metrics.get("n_signals", 0) < min_signals:
                continue

            # Check cooldown: skip agents currently in testing
            for mod in self._modifications.values():
                if mod.agent_id == agent_id and mod.status == "testing":
                    break
            else:
                # No active test for this agent
                sharpe = metrics.get("sharpe", 0)
                if sharpe < worst_sharpe:
                    worst_sharpe = sharpe
                    worst_id = agent_id

        return worst_id

    def generate_modification(
        self,
        agent_id: str,
        agent_metrics: Dict[str, Dict[str, float]],
        recent_signals: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[ModificationRecord]:
        """Generate a targeted prompt modification via LLM.

        Args:
            agent_id: Agent to modify.
            agent_metrics: Current metrics for context.
            recent_signals: Recent signal history for failure analysis.

        Returns:
            ModificationRecord if successful, None otherwise.
        """
        prompt = self._prompts.get(agent_id)
        if not prompt:
            logger.warning("No prompt registered for agent: %s", agent_id)
            return None

        metrics = agent_metrics.get(agent_id, {})
        sharpe = metrics.get("sharpe", 0)

        # Build analysis prompt
        analysis = self._build_modification_prompt(agent_id, prompt, sharpe, recent_signals or [])

        # Try LLM-based modification
        new_prompt = self._call_llm_for_modification(analysis)

        if not new_prompt:
            # Fallback: add a conservative rule
            new_prompt = self._fallback_modification(prompt, sharpe)

        if not new_prompt or new_prompt == prompt:
            logger.warning("No modification generated for %s", agent_id)
            return None

        mod = ModificationRecord(
            mod_id=f"mod_{agent_id}_{uuid.uuid4().hex[:8]}",
            agent_id=agent_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            modification=self._describe_change(prompt, new_prompt),
            old_prompt=prompt,
            new_prompt=new_prompt,
            pre_sharpe=sharpe,
            required_cycles=self.TEST_CYCLES,
        )

        self._modifications[mod.mod_id] = mod
        self._log_modification(mod)
        self._save_state()

        logger.info(
            "Generated modification for %s: %s (sharpe: %.2f)",
            agent_id,
            mod.modification[:100],
            sharpe,
        )

        return mod

    def apply_modification(self, mod_id: str) -> bool:
        """Apply a modification, updating the agent's prompt.

        Args:
            mod_id: Modification record ID.

        Returns:
            True if applied successfully.
        """
        mod = self._modifications.get(mod_id)
        if not mod:
            return False

        mod.status = "testing"
        self._prompts[mod.agent_id] = mod.new_prompt

        # Save modified prompt to file
        prompt_file = self.prompt_dir / f"{mod.agent_id}.txt"
        prompt_file.write_text(mod.new_prompt)

        self._save_state()
        logger.info("Applied modification %s to %s", mod_id, mod.agent_id)
        return True

    def advance_test(self, mod_id: str) -> bool:
        """Advance a modification's test cycle counter.

        Args:
            mod_id: Modification record ID.

        Returns:
            True if the test is now complete.
        """
        mod = self._modifications.get(mod_id)
        if not mod or mod.status != "testing":
            return False

        mod.test_cycles += 1
        self._save_state()

        if mod.test_cycles >= mod.required_cycles:
            logger.info(
                "Modification %s test complete (%d/%d cycles)",
                mod_id,
                mod.test_cycles,
                mod.required_cycles,
            )
            return True
        return False

    def evaluate_modification(self, mod_id: str, new_sharpe: float) -> str:
        """Evaluate a completed modification test.

        Args:
            mod_id: Modification record ID.
            new_sharpe: Sharpe ratio after testing period.

        Returns:
            "kept" or "reverted"
        """
        mod = self._modifications.get(mod_id)
        if not mod:
            return "not_found"

        mod.post_sharpe = new_sharpe
        improved = new_sharpe > mod.pre_sharpe

        if improved:
            mod.status = "kept"
            logger.info(
                "Modification %s KEPT: sharpe %.2f -> %.2f",
                mod_id,
                mod.pre_sharpe,
                new_sharpe,
            )
        else:
            mod.status = "reverted"
            # Revert prompt
            self._prompts[mod.agent_id] = mod.old_prompt
            prompt_file = self.prompt_dir / f"{mod.agent_id}.txt"
            prompt_file.write_text(mod.old_prompt)
            logger.info(
                "Modification %s REVERTED: sharpe %.2f -> %.2f",
                mod_id,
                mod.pre_sharpe,
                new_sharpe,
            )

        self._log_modification(mod)
        self._save_state()
        return mod.status

    def get_active_modifications(self) -> List[ModificationRecord]:
        """Get all modifications currently in testing."""
        return [m for m in self._modifications.values() if m.status == "testing"]

    def get_history(self, status: Optional[str] = None, limit: int = 50) -> List[ModificationRecord]:
        """Get modification history, optionally filtered by status."""
        mods = list(self._modifications.values())
        if status:
            mods = [m for m in mods if m.status == status]
        return sorted(mods, key=lambda m: m.timestamp, reverse=True)[:limit]

    def get_stats(self) -> Dict[str, Any]:
        """Get autoresearch statistics."""
        all_mods = list(self._modifications.values())
        completed = [m for m in all_mods if m.status in ("kept", "reverted")]
        kept = [m for m in completed if m.status == "kept"]
        reverted = [m for m in completed if m.status == "reverted"]

        return {
            "total_modifications": len(all_mods),
            "kept": len(kept),
            "reverted": len(reverted),
            "testing": len([m for m in all_mods if m.status == "testing"]),
            "keep_rate": len(kept) / len(completed) if completed else 0,
            "agents_trained": list(self._prompts.keys()),
        }

    # --- Internal helpers ---

    def _build_modification_prompt(
        self,
        agent_id: str,
        current_prompt: str,
        sharpe: float,
        recent_signals: List[Dict[str, Any]],
    ) -> str:
        """Build the prompt for generating a modification."""
        failures = [s for s in recent_signals if s.get("is_hit") is False][:10]

        failure_patterns = ""
        if failures:
            patterns = []
            for f in failures:
                patterns.append(
                    f"- {f.get('symbol', '?')}: {f.get('direction', '?')} "
                    f"conviction={f.get('conviction', '?')} "
                    f"return={f.get('actual_return', '?'):.4f} "
                    f"reason: {f.get('reasoning', '')[:80]}"
                )
            failure_patterns = "\n".join(patterns)

        return f"""You are optimizing a trading agent's prompt. The agent's current Sharpe ratio is {sharpe:.2f} (negative = poor).

Current prompt:
---
{current_prompt}
---

Recent failures (last {len(failures)} losing signals):
{failure_patterns}

Generate ONE targeted modification to the prompt that addresses the identified failure patterns.
The modification should be specific and actionable. Examples:
- "Add momentum filter to prevent high-conviction longs during sector weakness"
- "Require RSI < 70 before any bullish calls"
- "Add DXY threshold check before EM shorts"

Return ONLY the complete revised prompt (not just the diff). Keep the same structure and format.
Make ONE focused change that addresses the most common failure pattern."""

    def _call_llm_for_modification(self, prompt: str) -> Optional[str]:
        """Call LLM to generate prompt modification."""
        if not self._llm_factory:
            try:
                from src.providers.llm import build_llm

                llm = build_llm()
            except Exception:
                return None
        else:
            llm = self._llm_factory()

        try:
            response = llm.invoke(
                [
                    {
                        "role": "system",
                        "content": "You are an expert prompt engineer for quantitative trading agents. Return only the revised prompt, nothing else.",
                    },
                    {"role": "user", "content": prompt},
                ]
            )
            content = response.content if hasattr(response, "content") else str(response)
            # Strip markdown fences if present
            if content.strip().startswith("```"):
                lines = content.strip().split("\n")
                lines = lines[1:]  # Remove opening fence
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                content = "\n".join(lines)
            return content if content.strip() else None
        except Exception as e:
            logger.warning("LLM modification failed: %s", e)
            return None

    def _fallback_modification(self, prompt: str, sharpe: float) -> Optional[str]:
        """Fallback modification when LLM is unavailable."""
        if sharpe < -1.0:
            addition = "\n\n## Conservative Override\nWhen Sharpe is below -1.0, require:\n- RSI confirmation before any signal\n- Volume > 1.5x average for conviction > 70\n- Reduce conviction by 20% during high volatility (ATR > 2x average)"
        elif sharpe < -0.5:
            addition = "\n\n## Risk Filter\nBefore generating any signal:\n- Check if RSI is in extreme territory (>80 or <20)\n- If extreme, reduce conviction by 30%\n- Never generate high-conviction signals (>80) without volume confirmation"
        else:
            addition = "\n\n## Signal Quality\n- Require at least 2 confirming indicators before high-conviction signals\n- Reduce conviction for signals that contradict the broader market regime"

        return prompt + addition

    def _describe_change(self, old: str, new: str) -> str:
        """Describe the difference between old and new prompts."""
        old_lines = set(old.split("\n"))
        new_lines = set(new.split("\n"))
        added = new_lines - old_lines
        if added:
            return "\n".join(sorted(added)[:3])[:200]
        return "Prompt modified"

    def _log_modification(self, mod: ModificationRecord):
        """Append modification to JSONL log."""
        with open(self.log_file, "a") as f:
            f.write(json.dumps(asdict(mod), default=str) + "\n")
