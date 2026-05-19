"""Per-session skill activation registry (context-assembler.md v3 §5.2).

Tracks which skills have been activated in a session and enforces the
v3 §5.2.4 budget caps. Two populations:

- **Pre-activated** (`load_reason="always"`): bodies inlined into the
  stable system prefix by the v2 §5.1 padding rule. Free — they're
  already paid for by the cached prefix. Do not count against the
  explicit-activation budget. Re-calling `skill_load` for one returns a
  pointer (no body, no event) per §5.2.2.
- **Explicitly activated** (`load_reason="on_demand"`): bodies returned
  by `skill_load` and lodged in the message history as `tool_result`
  blocks. Bounded by `MAX_EXPLICIT_ACTIVATIONS_PER_SESSION` (count) and
  `HARD_CAP_CUMULATIVE_ACTIVATION_TOKENS` (cumulative tokens).
  Re-calling `skill_load` for one is a no-op (returns pointer, doesn't
  re-inject the body or increment the budget) per §5.2.7 question 4.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# Budget defaults per context-assembler.md v3 §5.2.4. These are
# module-level constants; per-workspace overrides via
# `<workspace>/.metis/skills/config.yaml` are deferred to a later
# revision per §5.2.4 "Configuration".
MAX_EXPLICIT_ACTIVATIONS_PER_SESSION = 3
WARN_CUMULATIVE_ACTIVATION_TOKENS = 10_000
HARD_CAP_CUMULATIVE_ACTIVATION_TOKENS = 30_000


@dataclass
class SkillActivationRegistry:
    """Per-session state for §5.2 activation bookkeeping."""

    # Skills inlined into the stable system prefix by v2 §5.1 padding.
    # Pre-activations don't count against the explicit budget.
    _preloaded: set[str] = field(default_factory=set)
    # Explicit activations, insertion-ordered by name → load_size_tokens.
    # Insertion order is preserved so the error message can list them in
    # the order the agent loaded them.
    _activated: dict[str, int] = field(default_factory=dict)
    # Sticky flag so the WARN log fires exactly once per session.
    _warned: bool = False

    # ---- Pre-activation ----------------------------------------------

    def mark_preloaded(self, name: str) -> None:
        """Record that this skill's body was inlined into the stable
        system prefix as v2 §5.1 padding. Idempotent."""
        self._preloaded.add(name)

    def is_preloaded(self, name: str) -> bool:
        return name in self._preloaded

    @property
    def preloaded_names(self) -> frozenset[str]:
        return frozenset(self._preloaded)

    # ---- Explicit activation -----------------------------------------

    def is_activated(self, name: str) -> bool:
        """True if `skill_load(name)` has already returned the body in
        this session."""
        return name in self._activated

    def record_activation(self, name: str, load_size_tokens: int) -> None:
        """Add an explicit activation to the registry. Caller MUST have
        passed the budget check via `check_can_activate` first."""
        self._activated[name] = load_size_tokens
        if not self._warned and self.cumulative_tokens >= WARN_CUMULATIVE_ACTIVATION_TOKENS:
            self._warned = True
            logger.warning(
                "session has activated %d skills totaling %d tokens "
                "(warn threshold: %d). Continued activation may exhaust "
                "the per-session cap of %d cumulative tokens.",
                self.count,
                self.cumulative_tokens,
                WARN_CUMULATIVE_ACTIVATION_TOKENS,
                HARD_CAP_CUMULATIVE_ACTIVATION_TOKENS,
            )

    @property
    def count(self) -> int:
        return len(self._activated)

    @property
    def cumulative_tokens(self) -> int:
        return sum(self._activated.values())

    @property
    def activated_names(self) -> list[str]:
        """Insertion-ordered list of explicitly activated skill names."""
        return list(self._activated.keys())

    # ---- Budget enforcement ------------------------------------------

    def check_can_activate(self, name: str, load_size_tokens: int) -> None:
        """Raise `SkillBudgetExceededError` if activating `name` with the
        given body size would exceed the v3 §5.2.4 caps.

        Re-activating an already-loaded skill is a no-op and never
        raises — callers should test `is_activated` first and short-circuit.
        Pre-activated skills also never reach this check — callers test
        `is_preloaded` first.
        """
        # Adding a new explicit activation increments count by 1 and
        # adds `load_size_tokens` to the cumulative total.
        prospective_count = self.count + 1
        prospective_tokens = self.cumulative_tokens + load_size_tokens
        if prospective_count > MAX_EXPLICIT_ACTIVATIONS_PER_SESSION:
            raise SkillBudgetExceededError(
                f"activation budget exhausted: {self.count} skills already "
                f"activated this session (limit "
                f"{MAX_EXPLICIT_ACTIVATIONS_PER_SESSION}). Already loaded: "
                f"{self.activated_names}. To free budget, start a fresh "
                f"session or summarize and discard previously loaded "
                f"skill bodies."
            )
        if prospective_tokens > HARD_CAP_CUMULATIVE_ACTIVATION_TOKENS:
            raise SkillBudgetExceededError(
                f"activation token cap exhausted: loading {name!r} would "
                f"bring cumulative activation tokens to {prospective_tokens} "
                f"(cap {HARD_CAP_CUMULATIVE_ACTIVATION_TOKENS}). Already "
                f"loaded: {self.activated_names}."
            )


class SkillBudgetExceededError(Exception):
    """Raised when activating one more skill would exceed v3 §5.2.4
    caps. Wrapped into `ToolExecutionError` by `SkillLoadTool` so it
    surfaces as a normal `tool.failed` event per §5.2.6."""


__all__ = [
    "HARD_CAP_CUMULATIVE_ACTIVATION_TOKENS",
    "MAX_EXPLICIT_ACTIVATIONS_PER_SESSION",
    "WARN_CUMULATIVE_ACTIVATION_TOKENS",
    "SkillActivationRegistry",
    "SkillBudgetExceededError",
]
