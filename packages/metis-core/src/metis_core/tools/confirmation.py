"""Confirmation policy and pluggable handler.

See tool-dispatcher.md §5.2 and §5.3. Confirmation modes per side-effect
class are configured here; the actual prompt-the-user flow is delegated to a
ConfirmationHandler. v1 ships AutoAllowHandler (auto-approves everything);
later layers will plug in a streaming-aware handler that emits
`tool.confirmation_requested` events and waits for an HTTP response.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from metis_core.canonical.tools import SideEffects

logger = logging.getLogger(__name__)


class ConfirmationMode(StrEnum):
    AUTO = "auto"  # execute without prompting
    PROMPT = "prompt"  # request user confirmation
    DENY = "deny"  # never execute


class ConfirmationDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class ConfirmationRequest:
    tool_use_id: str
    tool_name: str
    side_effects: SideEffects
    input_summary: str
    projected_modifications: list[str] | None = None
    command_summary: str | None = None


@dataclass
class ConfirmationPolicy:
    """Per-class confirmation modes with per-tool overrides.

    Defaults match tool-dispatcher.md §5.2: NONE/READ auto, WRITE/EXECUTE/
    NETWORK prompt. Users can lower the bar via per_tool or trusted_workspaces.
    """

    by_class: dict[SideEffects, ConfirmationMode] = field(
        default_factory=lambda: {
            SideEffects.NONE: ConfirmationMode.AUTO,
            SideEffects.READ: ConfirmationMode.AUTO,
            SideEffects.WRITE: ConfirmationMode.PROMPT,
            SideEffects.EXECUTE: ConfirmationMode.PROMPT,
            SideEffects.NETWORK: ConfirmationMode.PROMPT,
        }
    )
    per_tool: dict[str, ConfirmationMode] = field(default_factory=dict)

    def mode_for(self, tool_name: str, side_effects: SideEffects) -> ConfirmationMode:
        if tool_name in self.per_tool:
            return self.per_tool[tool_name]
        return self.by_class.get(side_effects, ConfirmationMode.PROMPT)


DEFAULT_POLICY = ConfirmationPolicy()


class ConfirmationHandler(Protocol):
    """Pluggable handler for `prompt` mode. The dispatcher calls request()
    and awaits the decision."""

    async def request(self, req: ConfirmationRequest) -> ConfirmationDecision: ...


class AutoAllowHandler:
    """Default handler that auto-approves everything.

    Useful for Phase 1 single-user development before the streaming-aware
    handler is wired up. NOT appropriate for multi-user or untrusted-tool
    scenarios.
    """

    async def request(self, req: ConfirmationRequest) -> ConfirmationDecision:
        logger.debug("auto-allow: %s (%s)", req.tool_name, req.side_effects.value)
        return ConfirmationDecision.ALLOW


class AutoDenyHandler:
    """Handler that denies everything. Useful in tests."""

    async def request(self, req: ConfirmationRequest) -> ConfirmationDecision:
        return ConfirmationDecision.DENY
