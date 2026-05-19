"""Per-message override parsing.

See routing-engine.md §9.2. A message starting with `@<alias>` followed by
whitespace is a per-message override; `\\@foo` opts out (the backslash is
stripped and no override applies).
"""

from __future__ import annotations

from dataclasses import dataclass

from metis.core.routing.registry import ModelRegistry


@dataclass(frozen=True)
class OverrideParseResult:
    """Result of parsing a user message for an @alias prefix."""

    cleaned_text: str  # message with the @alias token (and trailing whitespace) removed
    raw_alias: str | None  # the alias token without `@` if present, else None
    resolved_model: str | None  # canonical model id if the alias resolved, else None
    had_override_attempt: bool  # True iff message started with `@` (escape excluded)
    body_missing: bool = False  # True iff `@<alias>` had no whitespace + body after it

    @property
    def is_unknown_alias(self) -> bool:
        return self.had_override_attempt and not self.body_missing and self.resolved_model is None


def parse_per_message_override(message_text: str, registry: ModelRegistry) -> OverrideParseResult:
    """Parse a per-message @alias override from the start of a message.

    Returns the cleaned text plus the resolution outcome. Callers check
    `is_unknown_alias` to surface an error before starting the turn, and
    `body_missing` to reject bare `@<alias>` with no trailing whitespace +
    body (spec §9.2: "the override syntax must be at the start of the
    message and followed by whitespace").
    """
    if message_text.startswith("\\@"):
        return OverrideParseResult(
            cleaned_text=message_text[1:],
            raw_alias=None,
            resolved_model=None,
            had_override_attempt=False,
        )
    if not message_text.startswith("@"):
        return OverrideParseResult(
            cleaned_text=message_text,
            raw_alias=None,
            resolved_model=None,
            had_override_attempt=False,
        )

    # Split off the @<alias> token plus the rest. Per spec, the token must be
    # followed by whitespace; an inline `@foo` not at start was already
    # excluded above.
    parts = message_text.split(maxsplit=1)
    head = parts[0]
    alias_token = head[1:]
    if not alias_token:
        return OverrideParseResult(
            cleaned_text=message_text,
            raw_alias=None,
            resolved_model=None,
            had_override_attempt=False,
        )
    # Spec §9.2: `@<alias>` must be followed by whitespace. A bare `@haiku`
    # (no trailing whitespace + body) does not satisfy the syntax — flag it
    # so the caller can reject the turn with a clear error.
    if len(parts) < 2:
        return OverrideParseResult(
            cleaned_text=message_text,
            raw_alias=alias_token,
            resolved_model=None,
            had_override_attempt=True,
            body_missing=True,
        )
    rest = parts[1]
    resolved = registry.resolve_alias(alias_token)
    return OverrideParseResult(
        cleaned_text=rest,
        raw_alias=alias_token,
        resolved_model=resolved,
        had_override_attempt=True,
    )
