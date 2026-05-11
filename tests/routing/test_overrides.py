"""Tests for per-message @alias override parsing."""

from __future__ import annotations

from metis.routing.overrides import parse_per_message_override
from metis.routing.registry import ModelRegistry


def test_no_override_when_no_at_prefix(registry: ModelRegistry):
    result = parse_per_message_override("hello there", registry)
    assert result.had_override_attempt is False
    assert result.cleaned_text == "hello there"
    assert result.resolved_model is None


def test_resolves_alias(registry: ModelRegistry):
    result = parse_per_message_override("@haiku what time is it", registry)
    assert result.had_override_attempt is True
    assert result.raw_alias == "haiku"
    assert result.resolved_model == "anthropic:claude-haiku-4-5"
    assert result.cleaned_text == "what time is it"


def test_resolves_canonical_id(registry: ModelRegistry):
    result = parse_per_message_override("@anthropic:claude-opus-4-7 do the deep thing", registry)
    assert result.resolved_model == "anthropic:claude-opus-4-7"


def test_unknown_alias_flagged(registry: ModelRegistry):
    result = parse_per_message_override("@nope do something", registry)
    assert result.had_override_attempt is True
    assert result.raw_alias == "nope"
    assert result.resolved_model is None
    assert result.is_unknown_alias is True


def test_backslash_escape_no_override(registry: ModelRegistry):
    result = parse_per_message_override("\\@haiku is a model", registry)
    assert result.had_override_attempt is False
    assert result.cleaned_text == "@haiku is a model"
    assert result.resolved_model is None


def test_at_followed_by_whitespace_only(registry: ModelRegistry):
    """`@ ` is not a valid override."""
    result = parse_per_message_override("@ what is this", registry)
    assert result.had_override_attempt is False


def test_inline_at_not_override(registry: ModelRegistry):
    """Per spec §9.2, override must be at the START of the message."""
    result = parse_per_message_override("ping @haiku tomorrow", registry)
    assert result.had_override_attempt is False
    assert result.resolved_model is None
    assert result.cleaned_text == "ping @haiku tomorrow"


def test_override_with_no_remaining_text(registry: ModelRegistry):
    result = parse_per_message_override("@haiku", registry)
    assert result.resolved_model == "anthropic:claude-haiku-4-5"
    assert result.cleaned_text == ""
