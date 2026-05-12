"""Per-message override parsing.

See routing-engine.md §9.2. A message starting with `@<alias>` followed by
whitespace is a per-message override; `\\@foo` opts out (the backslash is
stripped and no override applies).
"""

from __future__ import annotations

from dataclasses import dataclass

from metis.routing.registry import ModelRegistry


@dataclass(frozen=True)
class OverrideParseResult:
    """Result of parsing a user message for an @alias prefix."""

    cleaned_text: str  # message with the @alias token (and trailing whitespace) removed
    raw_alias: str | None  # the alias token without `@` if present, else None
    resolved_model: str | None  # canonical model id if the alias resolved, else None
    had_override_attempt: bool  # True iff message started with `@` (escape excluded)

    @property
    def is_unknown_alias(self) -> bool:
        return self.had_override_attempt and self.resolved_model is None


def parse_per_message_override(message_text: str, registry: ModelRegistry) -> OverrideParseResult:
    """Parse a per-message @alias override from the start of a message.

    Returns the cleaned text plus the resolution outcome. Callers check
    `is_unknown_alias` to surface an error before starting the turn.
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
    rest = parts[1] if len(parts) > 1 else ""
    alias_token = head[1:]
    if not alias_token:
        return OverrideParseResult(
            cleaned_text=message_text,
            raw_alias=None,
            resolved_model=None,
            had_override_attempt=False,
        )
    resolved = registry.resolve_alias(alias_token)
    return OverrideParseResult(
        cleaned_text=rest,
        raw_alias=alias_token,
        resolved_model=resolved,
        had_override_attempt=True,
    )
