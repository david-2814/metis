"""TurnContext and RoutingDecision.

TurnContext is the input to RoutingEngine.decide(). RoutingDecision is the
output: chosen model plus the full chain trace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from metis.core.events.payloads import PolicyEvaluation


@dataclass
class TurnContext:
    """Inputs the routing engine needs to decide a turn's model."""

    session_id: str
    turn_id: str

    # Content fingerprint
    estimated_input_tokens: int
    has_images: bool = False
    has_tool_definitions: bool = False
    has_system_prompt: bool = False
    has_tool_calls_in_history: bool = False
    requires_structured_output: bool = False

    # User-set policy signals (in priority order: highest → lowest)
    per_message_override: str | None = None  # resolved canonical model id
    session_active_model: str | None = None  # MANUAL_STICKY
    workspace_default_model: str | None = None
    global_default_model: str | None = None

    # Predicate evaluation inputs (routing-engine §5.3.1 snapshot points)
    user_message_text: str = ""  # the new USER message of the current turn
    workspace_path: str = ""  # session's absolute workspace path
    timezone: str | None = None  # IANA zone name for time_of_day_between
    now_override: datetime | None = None  # for deterministic time-based tests

    # For tracing
    parent_event_id: str | None = None  # typically the turn.started event id


@dataclass(frozen=True)
class RoutingDecision:
    """Output of RoutingEngine.decide()."""

    chosen_model: str
    winner_index: int
    elapsed_ms: float
    chain: list[PolicyEvaluation] = field(default_factory=list)
