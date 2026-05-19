"""Per-event export-time redactor.

See `docs/specs/redaction.md §6`. Composes with the existing
`Sensitivity` enum tag on every event but does not gate on it — the
redaction *mode* decides what happens, not the sensitivity tag (see
spec §7.6 "Sensitivity tag is informational, not gating").

The redactor is stateless across events except for the
`AGGREGATE_ONLY` mode's accumulator. Two redactors with the same mode
and salt applied to the same input produce byte-identical output.
"""

from __future__ import annotations

import hashlib
from typing import Any

from metis.core.events.envelope import Event
from metis.core.redaction.aggregator import AggregateAccumulator
from metis.core.redaction.modes import (
    ENVELOPE_PSEUDONYM_FIELDS,
    PAYLOAD_PSEUDONYM_FIELDS,
    PRIVATE_TEXT_FIELDS,
    PSEUDONYM_PREFIX,
    REDACTED_SENTINEL,
    SIGNALS_EXTRA_TEXT_KEYS,
    PseudonymTag,
    RedactionMode,
)


def pseudonymize_value(value: str, tag: PseudonymTag | None = None, salt: bytes = b"") -> str:
    """Deterministic SHA-256 → 12-hex pseudonym.

    Matches the format shipped by 12a-2 (`redaction.default.pseudonym_for`):
    `f"redacted_{sha256(value + salt).hexdigest()[:12]}"`. With `salt=b""`
    the hash is content-addressable and matches the GDPR-forget pseudonym
    byte-for-byte. Optional `tag` is appended for human-readable grep
    (still a single, stable function of the inputs).

    Already-pseudonymized values (those starting with the
    `redacted_` prefix) pass through unchanged — the idempotence
    invariant (redaction.md §7.2) requires that a second redaction is a
    no-op on the identity fields.
    """
    if value.startswith(PSEUDONYM_PREFIX):
        return value
    digest = hashlib.sha256(value.encode("utf-8") + salt).hexdigest()[:12]
    if tag is None:
        return f"{PSEUDONYM_PREFIX}{digest}"
    return f"{PSEUDONYM_PREFIX}{tag.value}_{digest}"


def _redact_text(value: Any) -> Any:
    """Replace free-text with the sentinel; idempotent on already-redacted."""
    if value is None or value == REDACTED_SENTINEL:
        return value
    if isinstance(value, list):
        return [REDACTED_SENTINEL for _ in value]
    return REDACTED_SENTINEL


class EventRedactor:
    """Mode-driven per-event redactor.

    See `docs/specs/redaction.md`. Constructed once per export with one
    mode; redact each event in turn. For `AGGREGATE_ONLY` mode, call
    `finalize()` after the last event to get the rolled-up dict.

    `strip_user_controlled` is an additional knob for operators who
    want a stricter contract: when True, USER_CONTROLLED-tier text
    fields (rationales, skill bodies) are also replaced with the
    redaction sentinel. Off by default — see redaction.md §3.3.
    """

    def __init__(
        self,
        mode: RedactionMode,
        *,
        salt: bytes = b"",
        strip_user_controlled: bool = False,
    ) -> None:
        self._mode = mode
        self._salt = salt
        self._strip_user_controlled = strip_user_controlled
        self._aggregator: AggregateAccumulator | None = (
            AggregateAccumulator() if mode == RedactionMode.AGGREGATE_ONLY else None
        )

    @property
    def mode(self) -> RedactionMode:
        return self._mode

    def redact(self, event: Event) -> Event | None:
        """Return the redacted event, or `None` if this event was aggregated.

        Idempotent: `redact(redact(event))` produces the same output as
        a single `redact(event)` call (modulo the aggregate accumulator).

        `Event` is `msgspec.Struct(frozen=True)`; the input is never
        mutated. New envelope fields are passed via `msgspec.structs.replace`.
        """
        if self._mode == RedactionMode.PASSTHROUGH:
            return event
        if self._mode == RedactionMode.AGGREGATE_ONLY:
            assert self._aggregator is not None
            self._aggregator.absorb(event)
            return None
        # PSEUDONYMIZE and REDACT_PRIVATE both pseudonymize identity fields.
        # REDACT_PRIVATE additionally scrubs PRIVATE-tier text fields.
        new_session_id = self._pseudonymize_envelope(event.session_id, "session_id")
        new_turn_id = (
            self._pseudonymize_envelope(event.turn_id, "turn_id")
            if event.turn_id is not None
            else None
        )
        new_payload = self._redact_payload(event.type, dict(event.payload))
        # Use structs.replace to preserve every other field (id, timestamp,
        # actor, type, sensitivity, parent_event_id) and the frozen contract.
        import msgspec.structs

        return msgspec.structs.replace(
            event,
            session_id=new_session_id,
            turn_id=new_turn_id,
            payload=new_payload,
        )

    def finalize(self) -> dict | None:
        """Return the aggregate rollup (AGGREGATE_ONLY) or None otherwise."""
        if self._aggregator is None:
            return None
        return self._aggregator.finalize()

    # ---- internals -----------------------------------------------------

    def _pseudonymize_envelope(self, value: str, field_name: str) -> str:
        tag = ENVELOPE_PSEUDONYM_FIELDS.get(field_name)
        return pseudonymize_value(value, tag=tag, salt=self._salt)

    def _redact_payload(self, event_type: str, payload: dict) -> dict:
        """Apply the per-event-type rules to a payload dict."""
        # Step 1: pseudonymize identity fields under both PSEUDONYMIZE and
        # REDACT_PRIVATE.
        pseudonym_fields = PAYLOAD_PSEUDONYM_FIELDS.get(event_type, {})
        for field, tag in pseudonym_fields.items():
            value = payload.get(field)
            if isinstance(value, str) and value:
                payload[field] = pseudonymize_value(value, tag=tag, salt=self._salt)
        # Step 2: under REDACT_PRIVATE, strip PRIVATE-tier text fields.
        if self._mode == RedactionMode.REDACT_PRIVATE:
            text_fields = PRIVATE_TEXT_FIELDS.get(event_type, ())
            for field in text_fields:
                if field in payload and payload[field] is not None:
                    payload[field] = _redact_text(payload[field])
            # signals_extra has text keys nested inside it (turn.completed).
            if event_type == "turn.completed":
                signals = payload.get("signals_extra")
                if isinstance(signals, dict):
                    new_signals = dict(signals)
                    for key in SIGNALS_EXTRA_TEXT_KEYS:
                        if key in new_signals and new_signals[key] is not None:
                            new_signals[key] = _redact_text(new_signals[key])
                    payload["signals_extra"] = new_signals
        return payload
