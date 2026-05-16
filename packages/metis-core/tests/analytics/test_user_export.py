"""Tests for `AnalyticsStore.user_export` / `forget_user` (analytics-api.md §4.10).

Mirrors the SQL-projection style of `test_store.py`: seed events with known
`user_id` / `team_id` stamps directly via the conftest helper, then assert
on the streaming JSONL output and the forget-flow row count.
"""

from __future__ import annotations

import inspect
import json
from datetime import timedelta

import pytest
from metis_core.analytics import AnalyticsStore, TimeWindow
from metis_core.redaction import PseudonymizingRedactor, pseudonym_for


@pytest.fixture
def window(now):
    return TimeWindow(start=now - timedelta(days=1), end=now + timedelta(days=1))


# ---- export: basic correctness --------------------------------------------


def test_user_export_returns_only_subject_user_events(seeded_db, now):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.10",
        user_id="usr_alice",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-haiku-4-5",
        provider="anthropic",
        cost_usd="0.02",
        user_id="usr_bob",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.20",
        user_id="usr_alice",
    )
    with AnalyticsStore(db_path) as store:
        lines = list(store.user_export("usr_alice"))
    assert len(lines) == 2
    for chunk in lines:
        assert chunk.endswith(b"\n")
        obj = json.loads(chunk.rstrip(b"\n"))
        assert obj["payload"]["user_id"] == "usr_alice"


def test_user_export_includes_turn_completed_events(seeded_db, now):
    """`turn.completed` also carries `user_id` per multi-user.md §4.4 —
    the export must pick it up too, not just `llm.call_completed`."""
    db_path, seeder = seeded_db
    seeder.insert_event(
        event_type="turn.completed",
        timestamp=now,
        session_id="sess_a",
        turn_id="turn_a",
        actor="agent",
        payload={
            "stop_reason": "end_turn",
            "llm_call_count": 1,
            "tool_call_count": 0,
            "total_input_tokens": 100,
            "total_output_tokens": 10,
            "total_cost_usd": 0.05,
            "wall_time_seconds": 1.0,
            "user_id": "usr_alice",
            "team_id": "team_eng",
        },
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.05",
        user_id="usr_alice",
        turn_id="turn_a",
    )
    with AnalyticsStore(db_path) as store:
        lines = list(store.user_export("usr_alice"))
    types = sorted(json.loads(line)["type"] for line in lines)
    assert types == ["llm.call_completed", "turn.completed"]


def test_user_export_empty_for_unknown_user(seeded_db, now):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.10",
        user_id="usr_alice",
    )
    with AnalyticsStore(db_path) as store:
        lines = list(store.user_export("usr_does_not_exist"))
    assert lines == []


def test_user_export_window_filter(seeded_db, now):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.10",
        user_id="usr_alice",
    )
    seeder.insert_llm_call_completed(
        timestamp=now - timedelta(days=2),
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.10",
        user_id="usr_alice",
    )
    window = TimeWindow(start=now - timedelta(hours=12), end=now + timedelta(hours=12))
    with AnalyticsStore(db_path) as store:
        in_window = list(store.user_export("usr_alice", window=window))
        all_time = list(store.user_export("usr_alice"))
    assert len(in_window) == 1
    assert len(all_time) == 2


def test_user_export_deterministic_ordering(seeded_db, now):
    """Two consecutive exports of the same window produce byte-identical output."""
    db_path, seeder = seeded_db
    for _ in range(5):
        seeder.insert_llm_call_completed(
            timestamp=now,
            model="anthropic:claude-sonnet-4-6",
            provider="anthropic",
            cost_usd="0.01",
            user_id="usr_alice",
        )
    with AnalyticsStore(db_path) as store:
        first = b"".join(store.user_export("usr_alice"))
        second = b"".join(store.user_export("usr_alice"))
    assert first == second


def test_user_export_streams_without_buffering(seeded_db, now):
    """A 10k-event export doesn't materialize the full list in memory.

    The structural check is that the method is a generator, not a
    list-returning call. We also consume only the first item and confirm
    the rest are still pending — that's only possible with lazy fetch.
    """
    db_path, seeder = seeded_db
    n = 10_000
    for _ in range(n):
        seeder.insert_llm_call_completed(
            timestamp=now,
            model="anthropic:claude-sonnet-4-6",
            provider="anthropic",
            cost_usd="0.0001",
            user_id="usr_alice",
        )
    with AnalyticsStore(db_path) as store:
        gen = store.user_export("usr_alice")
        assert inspect.isgenerator(gen)
        first = next(gen)
        assert first.endswith(b"\n")
        remaining = sum(1 for _ in gen)
    assert remaining == n - 1


def test_user_event_count_helper(seeded_db, now):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now, model="m", provider="p", cost_usd="0.01", user_id="usr_a"
    )
    seeder.insert_llm_call_completed(
        timestamp=now, model="m", provider="p", cost_usd="0.01", user_id="usr_b"
    )
    seeder.insert_llm_call_completed(
        timestamp=now, model="m", provider="p", cost_usd="0.01", user_id="usr_a"
    )
    with AnalyticsStore(db_path) as store:
        assert store.user_event_count("usr_a") == 2
        assert store.user_event_count("usr_b") == 1
        assert store.user_event_count("usr_unknown") == 0


# ---- forget: pseudonymization + idempotence -------------------------------


def test_pseudonym_for_is_deterministic_and_distinct():
    assert pseudonym_for("usr_alice") == pseudonym_for("usr_alice")
    assert pseudonym_for("usr_alice") != pseudonym_for("usr_bob")
    assert pseudonym_for("usr_alice").startswith("redacted_")


def test_forget_user_pseudonymizes_then_export_is_empty(seeded_db, now):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now, model="m", provider="p", cost_usd="0.10", user_id="usr_alice"
    )
    seeder.insert_llm_call_completed(
        timestamp=now, model="m", provider="p", cost_usd="0.20", user_id="usr_alice"
    )
    seeder.insert_llm_call_completed(
        timestamp=now, model="m", provider="p", cost_usd="0.05", user_id="usr_bob"
    )

    redactor = PseudonymizingRedactor(db_path)
    with AnalyticsStore(db_path) as store:
        assert store.user_event_count("usr_alice") == 2
        rows = store.forget_user("usr_alice", redactor=redactor)
        assert rows == 2
        assert list(store.user_export("usr_alice")) == []
        assert store.user_event_count("usr_bob") == 1
        assert store.user_event_count(pseudonym_for("usr_alice")) == 2


def test_forget_user_is_idempotent(seeded_db, now):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now, model="m", provider="p", cost_usd="0.10", user_id="usr_alice"
    )
    redactor = PseudonymizingRedactor(db_path)
    with AnalyticsStore(db_path) as store:
        first = store.forget_user("usr_alice", redactor=redactor)
        second = store.forget_user("usr_alice", redactor=redactor)
    assert first == 1
    assert second == 0


def test_forget_unknown_user_returns_zero(seeded_db):
    db_path, _seeder = seeded_db
    redactor = PseudonymizingRedactor(db_path)
    with AnalyticsStore(db_path) as store:
        rows = store.forget_user("usr_nobody", redactor=redactor)
    assert rows == 0
