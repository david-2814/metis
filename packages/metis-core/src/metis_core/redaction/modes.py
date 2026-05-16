"""Redaction modes + per-field policy table.

See `docs/specs/redaction.md §2`, `§3`. The policy is a declarative
table keyed by event type. The redactor never invents field names —
every entry mirrors the typed payload struct in
`metis_core.events.payloads`.
"""

from __future__ import annotations

from enum import StrEnum

REDACTED_SENTINEL = "[REDACTED]"

# Pseudonym prefix tag (matches 12a-2's `redaction.default.pseudonym_for`).
# Kept here so callers can detect already-pseudonymized values for the
# idempotence invariant (§7.2) without importing the SQL impl.
PSEUDONYM_PREFIX = "redacted_"


class RedactionMode(StrEnum):
    """Mutually exclusive export modes; see redaction.md §2."""

    PASSTHROUGH = "passthrough"
    PSEUDONYMIZE = "pseudonymize"
    REDACT_PRIVATE = "redact_private"
    AGGREGATE_ONLY = "aggregate_only"


class PseudonymTag(StrEnum):
    """Human-readable tag baked into pseudonym values for operator triage.

    Not part of the hash; appended to the prefix for log-grep readability.
    The catalog of tags is closed by design: a redactor that encounters a
    field requiring a new tag will hash without one, never silently invent.
    """

    SESSION = "sess"
    TURN = "turn"
    USER = "user"
    TEAM = "team"
    GATEWAY_KEY = "gkey"
    PARENT_SESSION = "psess"
    WORKSPACE = "wkspc"
    REQUEST = "req"


# Envelope-level fields that get pseudonymized under PSEUDONYMIZE and
# stricter modes. Keyed by the Event attribute name.
ENVELOPE_PSEUDONYM_FIELDS: dict[str, PseudonymTag] = {
    "session_id": PseudonymTag.SESSION,
    "turn_id": PseudonymTag.TURN,
}


# Per-event-type pseudonym fields. Each entry maps the payload's JSON key
# to the pseudonym tag. The matcher is conservative: only fields whose
# values are identity strings (not free-text content) get hashed.
#
# Workspace_hash is already a SHA-256 fingerprint per
# canonical-message-format / multi-user.md — leave it verbatim (hashing
# a hash adds no privacy and breaks grep-by-workspace-hash recipes).
PAYLOAD_PSEUDONYM_FIELDS: dict[str, dict[str, PseudonymTag]] = {
    "session.created": {
        "workspace_path": PseudonymTag.WORKSPACE,
    },
    "turn.completed": {
        "user_id": PseudonymTag.USER,
        "team_id": PseudonymTag.TEAM,
        "parent_session_id": PseudonymTag.PARENT_SESSION,
    },
    "llm.call_started": {
        "request_id": PseudonymTag.REQUEST,
        "parent_session_id": PseudonymTag.PARENT_SESSION,
    },
    "llm.call_completed": {
        "user_id": PseudonymTag.USER,
        "team_id": PseudonymTag.TEAM,
        "gateway_key_id": PseudonymTag.GATEWAY_KEY,
        "parent_session_id": PseudonymTag.PARENT_SESSION,
    },
    "gateway.key_issued": {
        "gateway_key_id": PseudonymTag.GATEWAY_KEY,
        "workspace_path": PseudonymTag.WORKSPACE,
        "user_id": PseudonymTag.USER,
        "team_id": PseudonymTag.TEAM,
    },
    "gateway.key_revoked": {
        "gateway_key_id": PseudonymTag.GATEWAY_KEY,
    },
    "gateway.key_rotated": {
        "old_gateway_key_id": PseudonymTag.GATEWAY_KEY,
        "new_gateway_key_id": PseudonymTag.GATEWAY_KEY,
        "workspace_path": PseudonymTag.WORKSPACE,
        "user_id": PseudonymTag.USER,
        "team_id": PseudonymTag.TEAM,
    },
    "gateway.quota_exceeded": {
        "gateway_key_id": PseudonymTag.GATEWAY_KEY,
        "user_id": PseudonymTag.USER,
        "team_id": PseudonymTag.TEAM,
    },
    "quota.alert": {
        "gateway_key_id": PseudonymTag.GATEWAY_KEY,
        "user_id": PseudonymTag.USER,
        "team_id": PseudonymTag.TEAM,
    },
    "delegate.started": {
        "parent_session_id": PseudonymTag.PARENT_SESSION,
        "worker_session_id": PseudonymTag.SESSION,
    },
    "delegate.completed": {
        "parent_session_id": PseudonymTag.PARENT_SESSION,
        "worker_session_id": PseudonymTag.SESSION,
    },
    "delegate.failed": {
        "parent_session_id": PseudonymTag.PARENT_SESSION,
        "worker_session_id": PseudonymTag.SESSION,
    },
    "analytics.user_exported": {
        "subject_user_id": PseudonymTag.USER,
    },
    "analytics.user_forgotten": {
        "subject_user_id": PseudonymTag.USER,
        # `pseudonym` is already a hash; leave verbatim.
    },
}


# Per-event-type PRIVATE-tier text fields. Replaced with REDACTED_SENTINEL
# under REDACT_PRIVATE. Only fields that may carry user-originated free
# text or full file paths/commands are listed; hash-form fields
# (input_hash, fingerprint_id) are not redacted.
PRIVATE_TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "turn.started": ("user_message_text_redacted",),
    "tool.completed": ("files_modified", "command_executed"),
    "tool.failed": ("error_message",),
    "tool.confirmation_requested": (
        "input_summary",
        "command_summary",
        "projected_modifications",
    ),
    "llm.call_failed": ("error_message_redacted",),
}


# Keys within `turn.completed.signals_extra` that carry user-controlled
# text. Stripped under REDACT_PRIVATE. The signals_extra dict is the
# evaluator's content-penalty channel (see TurnCompleted docstring); user
# prompts and assistant responses ride this field for the LLM judge.
SIGNALS_EXTRA_TEXT_KEYS: tuple[str, ...] = (
    "user_prompt_text",
    "assistant_response_text",
)
