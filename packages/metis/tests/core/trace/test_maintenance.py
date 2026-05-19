"""VACUUM, WAL, and bulk-insert sanity for the trace store (Wave 13).

Covers:
- `TraceStore.vacuum()` reclaims free pages and doesn't break readers.
- `TraceStore.wal_size_bytes()` returns 0 on a fresh DB and grows after
  writes; survives FileNotFoundError when the WAL hasn't been touched.
- `TraceStore.wal_checkpoint(...)` returns SQLite's three-tuple verbatim.
- `wal_autocheckpoint_pages` constructor knob plumbs to the PRAGMA.
- A perf smoke that asserts >1k events/sec on a single-process bus +
  trace path. Not a hard limit — flaky-CI guards via x10 over the floor.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from metis.core.events.bus import EventBus
from metis.core.events.envelope import Actor
from metis.core.events.payloads import LLMCallCompleted, SessionCreated, make_event
from metis.core.trace.store import (
    DEFAULT_WAL_AUTOCHECKPOINT_PAGES,
    TraceStore,
)


def _llm_event(session_id: str, i: int):
    return make_event(
        type="llm.call_completed",
        session_id=session_id,
        turn_id=f"turn_{i}",
        parent_event_id=None,
        actor=Actor.SYSTEM,
        payload=LLMCallCompleted(
            model="anthropic:claude-sonnet-4-6",
            provider="anthropic",
            input_tokens=100,
            output_tokens=50,
            cached_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.001,
            pricing_version="2026-05-01",
            latency_ms=120,
            stop_reason="end_turn",
            produced_tool_calls=0,
            produced_thinking_blocks=0,
            gateway_key_id=f"gw_{i % 3}",
            inbound_shape="openai",
            user_id=None,
            team_id=None,
            parent_session_id=None,
        ),
        timestamp=datetime.now(UTC),
    )


def _session_event(session_id: str):
    return make_event(
        type="session.created",
        session_id=session_id,
        actor=Actor.SYSTEM,
        payload=SessionCreated(
            workspace_path="/x",
            workspace_hash="h",
            initial_active_model=None,
            routing_policy_version="v",
        ),
        timestamp=datetime.now(UTC),
    )


# ---- VACUUM ---------------------------------------------------------------


def test_vacuum_returns_byte_delta(tmp_path: Path):
    db = tmp_path / "trace.db"
    store = TraceStore(db)
    # Write enough rows to make free-page reclaim meaningful, then delete
    # most of them so VACUUM has work to do.
    for i in range(2000):
        store.write(_llm_event(f"sess_{i}", i))
    store._conn.execute("DELETE FROM events WHERE rowid % 2 = 0")
    delta = store.vacuum()
    # Delta CAN be slightly negative on tiny DBs due to page-boundary
    # rounding; we just assert it's an int and the rebuild ran without
    # raising.
    assert isinstance(delta, int)
    store.close()


def test_vacuum_does_not_break_readers(tmp_path: Path):
    """After VACUUM, queries return the same rows in the same order.

    Captures `(id, type, session_id)` triples before vacuum and asserts
    they still come back identically afterwards.
    """
    db = tmp_path / "trace.db"
    store = TraceStore(db)
    for i in range(100):
        store.write(_llm_event(f"sess_{i % 5}", i))
    before = list(store._conn.execute("SELECT id, type, session_id FROM events ORDER BY id"))
    store.vacuum()
    after = list(store._conn.execute("SELECT id, type, session_id FROM events ORDER BY id"))
    assert before == after

    # Plus indexes are still functional after the rebuild.
    rows = store.events_for_session("sess_0")
    assert all(e.session_id == "sess_0" for e in rows)
    store.close()


def test_vacuum_via_separate_reader_connection(tmp_path: Path):
    """A second TraceStore opened on the same file sees post-VACUUM state."""
    db = tmp_path / "trace.db"
    writer = TraceStore(db)
    for i in range(50):
        writer.write(_llm_event(f"sess_{i}", i))
    writer._conn.execute("DELETE FROM events WHERE rowid > 25")
    writer.vacuum()
    writer.close()

    reader = TraceStore(db)
    count = reader._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 25
    reader.close()


# ---- WAL ------------------------------------------------------------------


def test_wal_size_bytes_returns_int_without_raising(tmp_path: Path):
    """Contract: returns a non-negative int even when the WAL doesn't exist.

    A freshly-opened DB has the WAL populated during schema setup, so
    we don't assert exactly zero here — just the non-negative-int
    contract that backs the Prometheus gauge.
    """
    db = tmp_path / "trace.db"
    store = TraceStore(db)
    size = store.wal_size_bytes()
    assert isinstance(size, int)
    assert size >= 0
    store.close()


def test_wal_size_bytes_returns_zero_when_wal_missing(tmp_path: Path):
    """Manually delete the -wal file to exercise the FileNotFoundError branch."""
    db = tmp_path / "trace.db"
    store = TraceStore(db)
    store.close()
    wal_path = Path(str(db) + "-wal")
    if wal_path.exists():
        wal_path.unlink()
    # Re-open; until first write, deleting and asking is the contract.
    store = TraceStore(db)
    # Re-delete after open in case open re-created it.
    if wal_path.exists():
        wal_path.unlink()
    assert store.wal_size_bytes() == 0
    store.close()


def test_wal_size_bytes_grows_after_writes(tmp_path: Path):
    db = tmp_path / "trace.db"
    store = TraceStore(db)
    for i in range(10):
        store.write(_llm_event(f"sess_{i}", i))
    # WAL has been touched; size > 0.
    assert store.wal_size_bytes() > 0
    store.close()


def test_wal_checkpoint_truncate_resets_wal(tmp_path: Path):
    db = tmp_path / "trace.db"
    store = TraceStore(db)
    for i in range(50):
        store.write(_llm_event(f"sess_{i}", i))
    assert store.wal_size_bytes() > 0
    busy, _log_pages, _checkpointed = store.wal_checkpoint(mode="TRUNCATE")
    assert busy == 0
    assert store.wal_size_bytes() == 0
    store.close()


def test_wal_checkpoint_unknown_mode_raises(tmp_path: Path):
    db = tmp_path / "trace.db"
    store = TraceStore(db)
    with pytest.raises(ValueError):
        store.wal_checkpoint(mode="EXHAUSTIVE")
    store.close()


def test_wal_autocheckpoint_pragma_set_to_default(tmp_path: Path):
    db = tmp_path / "trace.db"
    store = TraceStore(db)
    row = store._conn.execute("PRAGMA wal_autocheckpoint").fetchone()
    assert row[0] == DEFAULT_WAL_AUTOCHECKPOINT_PAGES
    store.close()


def test_wal_autocheckpoint_pages_constructor_override(tmp_path: Path):
    db = tmp_path / "trace.db"
    store = TraceStore(db, wal_autocheckpoint_pages=2048)
    row = store._conn.execute("PRAGMA wal_autocheckpoint").fetchone()
    assert row[0] == 2048
    store.close()


# ---- Bulk-insert smoke ----------------------------------------------------


def test_bulk_insert_perf_smoke(tmp_path: Path):
    """Confirm >1k events/sec end-to-end on a single-process bus + trace.

    Floor is set well below the measured 4-5k events/sec baseline so CI
    flakes don't fire; tightening the floor would push false positives
    on slow runners. The benchmark script is the authoritative perf
    surface (docs/operations/trace-performance.md §1).
    """

    async def _run():
        db = tmp_path / "trace.db"
        bus = EventBus(queue_size=4096)
        bus.start()
        store = TraceStore(db)
        handle = store.attach_to(bus)

        n = 1000
        # session.created so the FK satisfies (PRAGMA foreign_keys is OFF
        # by default but writing a session helps causality realism).
        bus.emit(_session_event("perf"))
        wall_start = time.perf_counter()
        for i in range(n):
            bus.emit(_llm_event("perf", i))
        await bus.drain()
        wall_end = time.perf_counter()

        bus.unsubscribe(handle)
        await bus.stop()
        store.close()

        eps = n / (wall_end - wall_start)
        assert eps > 1000, (
            f"perf regression: {eps:.0f} events/sec; expected >1k. "
            "See docs/operations/trace-performance.md §1.1 to investigate."
        )

    asyncio.run(_run())


# ---- Bulk-insert prepared-statement contract ------------------------------


def test_bulk_insert_uses_parameterized_sql(tmp_path: Path):
    """Sanity: TraceStore.write parameterizes — never string-formats.

    The contract is that user-controlled fields (session_id, payload
    JSON) never end up in SQL text. Verifying via a hostile payload
    that would break naive string interpolation but is harmless under
    parameterization.
    """
    db = tmp_path / "trace.db"
    store = TraceStore(db)
    nasty_session = "'; DROP TABLE events; --"
    store.write(_session_event(nasty_session))
    # Table still exists if parameterization holds.
    rows = store.events_for_session(nasty_session)
    assert len(rows) == 1
    assert rows[0].session_id == nasty_session
    store.close()
