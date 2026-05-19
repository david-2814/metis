"""Equality and hashing semantics for MessageMetadata.

canonical-message-format.md §6.5: `provider_raw` is not part of equality
comparisons or hashing.
"""

from __future__ import annotations

from decimal import Decimal

from metis.core.canonical.messages import (
    MessageMetadata,
    MessageStatus,
    Usage,
)


def test_provider_raw_does_not_affect_equality():
    a = MessageMetadata(model="m", provider="p", provider_raw={"x": 1})
    b = MessageMetadata(model="m", provider="p", provider_raw={"x": 2})
    assert a == b


def test_provider_raw_does_not_affect_hash():
    a = MessageMetadata(model="m", provider="p", provider_raw={"x": 1})
    b = MessageMetadata(model="m", provider="p", provider_raw={"x": 2})
    # Both hash without error and produce identical hashes.
    assert hash(a) == hash(b)


def test_metadata_with_provider_raw_is_hashable():
    """The dict in provider_raw must not poison hashability."""
    md = MessageMetadata(model="m", provider="p", provider_raw={"nested": {"k": "v"}})
    hash(md)  # must not raise


def test_provider_raw_none_vs_set_compare_equal():
    a = MessageMetadata(model="m", provider="p")
    b = MessageMetadata(model="m", provider="p", provider_raw={"x": 1})
    assert a == b
    assert hash(a) == hash(b)


def test_model_difference_breaks_equality():
    a = MessageMetadata(model="m1", provider="p")
    b = MessageMetadata(model="m2", provider="p")
    assert a != b
    assert hash(a) != hash(b)


def test_provider_difference_breaks_equality():
    a = MessageMetadata(model="m", provider="p1")
    b = MessageMetadata(model="m", provider="p2")
    assert a != b
    assert hash(a) != hash(b)


def test_usage_difference_breaks_equality():
    u1 = Usage(
        input_tokens=10,
        output_tokens=5,
        cost_usd=Decimal("0.001"),
        pricing_version="v1",
        latency_ms=100,
    )
    u2 = Usage(
        input_tokens=20,
        output_tokens=5,
        cost_usd=Decimal("0.002"),
        pricing_version="v1",
        latency_ms=100,
    )
    a = MessageMetadata(model="m", provider="p", usage=u1)
    b = MessageMetadata(model="m", provider="p", usage=u2)
    assert a != b
    assert hash(a) != hash(b)


def test_status_difference_breaks_equality():
    a = MessageMetadata(model="m", provider="p", status=MessageStatus.COMPLETE)
    b = MessageMetadata(model="m", provider="p", status=MessageStatus.ERROR)
    assert a != b
    assert hash(a) != hash(b)


def test_parent_tool_use_id_difference_breaks_equality():
    a = MessageMetadata(parent_tool_use_id="tool_1")
    b = MessageMetadata(parent_tool_use_id="tool_2")
    assert a != b
    assert hash(a) != hash(b)


def test_equality_is_reflexive_and_symmetric():
    a = MessageMetadata(model="m", provider="p", provider_raw={"x": 1})
    b = MessageMetadata(model="m", provider="p", provider_raw={"y": 2})
    assert a == a
    assert a == b
    assert b == a


def test_equality_with_non_metadata_returns_not_implemented():
    md = MessageMetadata(model="m")
    assert (md == "not metadata") is False
    assert (md == 42) is False


def test_can_be_used_in_set():
    """Hashable means usable as a dict key / set member — exercise that."""
    a = MessageMetadata(model="m", provider="p", provider_raw={"x": 1})
    b = MessageMetadata(model="m", provider="p", provider_raw={"x": 2})
    s = {a, b}
    assert len(s) == 1


def test_user_id_default_is_none():
    md = MessageMetadata(model="m", provider="p")
    assert md.user_id is None
    assert md.team_id is None


def test_user_id_difference_breaks_equality():
    a = MessageMetadata(model="m", provider="p", user_id="usr_alice")
    b = MessageMetadata(model="m", provider="p", user_id="usr_bob")
    assert a != b
    assert hash(a) != hash(b)


def test_team_id_difference_breaks_equality():
    a = MessageMetadata(model="m", provider="p", team_id="team_eng")
    b = MessageMetadata(model="m", provider="p", team_id="team_marketing")
    assert a != b
    assert hash(a) != hash(b)


def test_user_team_none_vs_set_break_equality():
    a = MessageMetadata(model="m", provider="p")
    b = MessageMetadata(model="m", provider="p", user_id="usr_alice")
    assert a != b
    assert hash(a) != hash(b)
