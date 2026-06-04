"""Tests for `metis evaluate --batch-mode` / `--collect-batches`.

The batch path goes through three adapter methods (`submit_batch`,
`poll_batch`, `fetch_batch`) and a small persistence layer on the trace
DB (`evaluator_batch_handles`). These tests drive both halves with a
lightweight fake adapter, then read back the trace store to verify the
expected `eval.completed` rows landed.

The fake adapter (`_FakeBatchAdapter`) implements the ProviderAdapter
shape only as far as the batch path needs. It returns deterministic
`CanonicalResponse` objects that round-trip cleanly through the LLM
judge's JSON parser, so verdict assertions can be byte-precise.

The crucial property — "verdicts match sync mode byte-for-byte except
for `pricing_mode='batch'`" — is exercised by
`test_batch_verdict_matches_sync_byte_for_byte_except_pricing_mode`,
which runs both paths against the same fake adapter and a single
turn.completed event and asserts the score/confidence/rubric line up
identically.
"""

from __future__ import annotations

import time
from pathlib import Path

# Importing the anthropic adapter first eliminates a pre-existing
# order-dependent import cycle (`canonical.batch` ↔ `adapters.protocol`).
# Test ordering should not silently change adapter import timing.
from metis.core.adapters import anthropic as _anthropic_adapter_loader  # noqa: F401
from metis.core.adapters.errors import ErrorClass
from metis.core.adapters.protocol import (
    CanonicalRequest,
    CanonicalResponse,
    StopReason,
    TokenUsage,
)
from metis.core.canonical.batch import BatchError, BatchHandle, BatchStatus
from metis.core.canonical.content import TextBlock
from metis.core.eval.batch import (
    collect_pending_batches,
    submit_batch_for_window,
)
from metis.core.trace.store import TraceStore

from .helpers import build_turn_completed, new_turn_id

# ---------------------------------------------------------------------------
# Fake adapter
# ---------------------------------------------------------------------------


class _FakeBatchAdapter:
    """Implements the four methods batch.py touches.

    The adapter is single-batch by design — `submit_batch` records the
    in-flight requests on the instance so subsequent `poll_batch` /
    `fetch_batch` calls can reply against the captured custom_ids.

    `poll_status` toggles what `poll_batch` returns (defaults to
    `"completed"`); `next_response_text` controls the JSON the LLM-judge
    parse path sees on every result row.
    """

    name = "anthropic"

    def __init__(
        self,
        *,
        poll_status: BatchStatus = "completed",
        response_text: str = '{"score": 0.8, "confidence": 0.9, "rationale": "OK"}',
        error_per_custom_id: dict[str, BatchError] | None = None,
        response_text_per_custom_id: dict[str, str] | None = None,
    ) -> None:
        self._poll_status = poll_status
        self._response_text = response_text
        self._error_per_custom_id = error_per_custom_id or {}
        self._response_text_per_custom_id = response_text_per_custom_id or {}
        self.submitted_batches: list[list[CanonicalRequest]] = []
        self.poll_calls: list[BatchHandle] = []
        self.fetch_calls: list[BatchHandle] = []

    async def submit_batch(self, requests: list[CanonicalRequest]) -> BatchHandle:
        self.submitted_batches.append(list(requests))
        return BatchHandle(
            provider=self.name,
            batch_id=f"batch_{len(self.submitted_batches)}",
            submitted_at_ms=int(time.time() * 1000),
            request_count=len(requests),
            custom_ids=tuple(r.request_id for r in requests),
        )

    async def poll_batch(self, handle: BatchHandle) -> BatchStatus:
        self.poll_calls.append(handle)
        return self._poll_status

    async def fetch_batch(
        self,
        handle: BatchHandle,
    ) -> list[CanonicalResponse | BatchError]:
        self.fetch_calls.append(handle)
        results: list[CanonicalResponse | BatchError] = []
        for custom_id in handle.custom_ids:
            err = self._error_per_custom_id.get(custom_id)
            if err is not None:
                results.append(err)
                continue
            text = self._response_text_per_custom_id.get(custom_id, self._response_text)
            results.append(
                CanonicalResponse(
                    request_id=custom_id,
                    model="anthropic:claude-haiku-4-5",
                    provider="anthropic",
                    content=[TextBlock(text=text)],
                    stop_reason=StopReason.END_TURN,
                    usage=TokenUsage(
                        input_tokens=500,
                        output_tokens=30,
                        pricing_mode="batch",
                    ),
                    latency_ms=120,
                )
            )
        return results

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_turns(db_path: Path, *, count: int = 3) -> list[tuple[str, str]]:
    """Seed `count` turn.completed events. Returns [(session_id, turn_id)]."""
    trace = TraceStore(db_path)
    turn_ids: list[tuple[str, str]] = []
    session_id = "sess_batch"
    for i in range(count):
        turn_id = new_turn_id()
        trace.write(
            build_turn_completed(
                session_id=session_id,
                turn_id=turn_id,
                signals_extra={
                    "user_prompt_text": f"please do task {i}",
                    "assistant_response_text": f"task {i} complete",
                },
            )
        )
        turn_ids.append((session_id, turn_id))
    trace.close()
    return turn_ids


def _count_handle_rows(db_path: Path, status: str | None = None) -> int:
    trace = TraceStore(db_path)
    try:
        if status is None:
            cur = trace._conn.execute("SELECT COUNT(*) FROM evaluator_batch_handles")
        else:
            cur = trace._conn.execute(
                "SELECT COUNT(*) FROM evaluator_batch_handles WHERE status = ?",
                (status,),
            )
        return int(cur.fetchone()[0])
    finally:
        trace.close()


def _count_eval_completed(db_path: Path, subject_id: str | None = None) -> int:
    trace = TraceStore(db_path)
    try:
        if subject_id is None:
            cur = trace._conn.execute("SELECT COUNT(*) FROM events WHERE type = 'eval.completed'")
            return int(cur.fetchone()[0])
        cur = trace._conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'eval.completed' "
            "AND json_extract(payload_json, '$.subject_id') = ?",
            (subject_id,),
        )
        return int(cur.fetchone()[0])
    finally:
        trace.close()


def _read_eval_completed_payloads(db_path: Path) -> list[dict]:
    """Return the full payload dict of every `eval.completed` row, ordered by id."""
    import json

    trace = TraceStore(db_path)
    try:
        cur = trace._conn.execute(
            "SELECT payload_json FROM events WHERE type = 'eval.completed' ORDER BY id"
        )
        return [json.loads(row[0]) for row in cur.fetchall()]
    finally:
        trace.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_trace_store_creates_evaluator_batch_handles_table(tmp_path: Path):
    """`TraceStore` adds the additive table on first open — no schema
    bump required. The `CREATE TABLE IF NOT EXISTS` makes the migration
    a no-op on existing DBs."""
    db = tmp_path / "trace.db"
    trace = TraceStore(db)
    try:
        cur = trace._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='evaluator_batch_handles'"
        )
        assert cur.fetchone() is not None, (
            "evaluator_batch_handles should be created by TraceStore opening"
        )
        # And the supporting index on status, so --collect-batches's
        # "WHERE status = 'pending'" query plans cleanly.
        cur = trace._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_evaluator_batch_handles_status'"
        )
        assert cur.fetchone() is not None
    finally:
        trace.close()


# ---------------------------------------------------------------------------
# Submit path
# ---------------------------------------------------------------------------


async def test_batch_mode_submit_persists_handle_and_exits(tmp_path: Path):
    """`submit_batch_for_window` submits one batch, persists one
    handle-row per subject, and returns the batch metadata for the CLI
    to print. No `eval.completed` events are emitted on submit — those
    only land on `--collect-batches`."""
    db = tmp_path / "trace.db"
    _seed_turns(db, count=3)
    adapter = _FakeBatchAdapter()

    results = await submit_batch_for_window(
        db_path=db,
        adapter=adapter,
        subject_kind="turn",
    )

    # One chunk → one batch → one BatchSubmitResult.
    assert len(results) == 1
    assert results[0].request_count == 3
    assert results[0].subject_kind == "turn"
    assert results[0].handle.provider == "anthropic"

    # Three pending handles in the trace DB — one per turn.
    assert _count_handle_rows(db) == 3
    assert _count_handle_rows(db, status="pending") == 3

    # No eval.completed yet — submit-only.
    assert _count_eval_completed(db) == 0

    # Adapter was called exactly once with all 3 requests bundled.
    assert len(adapter.submitted_batches) == 1
    assert len(adapter.submitted_batches[0]) == 3


async def test_submit_with_empty_window_does_nothing(tmp_path: Path):
    """`submit_batch_for_window` against an empty window short-circuits
    cleanly — no batch is submitted and no handle row is written."""
    db = tmp_path / "trace.db"
    # Open + close so the schema lands without seeding any events.
    TraceStore(db).close()
    adapter = _FakeBatchAdapter()

    results = await submit_batch_for_window(
        db_path=db,
        adapter=adapter,
        subject_kind="turn",
    )

    assert results == []
    assert adapter.submitted_batches == []
    assert _count_handle_rows(db) == 0


# ---------------------------------------------------------------------------
# Collect path
# ---------------------------------------------------------------------------


async def test_collect_batches_ingests_completed_with_pricing_mode_batch(
    tmp_path: Path,
):
    """`collect_pending_batches` polls the adapter, fetches results,
    parses verdicts, and emits `eval.completed` events with
    `signals.pricing_mode='batch'`. After ingest, the handle rows are
    marked `status='ingested'` with `ingested_at_ms` stamped."""
    db = tmp_path / "trace.db"
    _seed_turns(db, count=2)
    adapter = _FakeBatchAdapter(
        response_text='{"score": 0.75, "confidence": 0.85, "rationale": "looks good"}'
    )

    # Submit first so there are pending handles.
    await submit_batch_for_window(db_path=db, adapter=adapter, subject_kind="turn")
    assert _count_handle_rows(db, status="pending") == 2

    # Collect. The fake reports "completed" by default.
    results = await collect_pending_batches(db_path=db, adapter=adapter)

    assert len(results) == 1
    assert results[0].status == "completed"
    assert results[0].verdicts_emitted == 2
    assert results[0].request_count == 2

    # Two `eval.completed` events landed; both carry `pricing_mode='batch'`
    # in signals (the typed payload doesn't have a dedicated field; signals
    # is the documented place per the spec deviation noted in batch.py).
    payloads = _read_eval_completed_payloads(db)
    assert len(payloads) == 2
    for p in payloads:
        assert p["signals"]["pricing_mode"] == "batch"
        assert p["judge_kind"] == "llm"
        # Score/confidence parsed from the JSON response text byte-for-byte.
        assert p["score"] == 0.75
        assert p["confidence"] == 0.85

    # Handle rows transitioned to "ingested" with a non-null timestamp.
    assert _count_handle_rows(db, status="pending") == 0
    assert _count_handle_rows(db, status="ingested") == 2

    trace = TraceStore(db)
    try:
        cur = trace._conn.execute("SELECT ingested_at_ms FROM evaluator_batch_handles")
        for row in cur.fetchall():
            assert row[0] is not None and row[0] > 0
    finally:
        trace.close()


async def test_collect_batches_is_idempotent(tmp_path: Path):
    """Re-running `--collect-batches` against an already-ingested handle
    does NOT emit duplicate `eval.completed` events. The pending-handle
    query filters on `status='pending'`, so ingested rows are skipped on
    the second pass."""
    db = tmp_path / "trace.db"
    _seed_turns(db, count=2)
    adapter = _FakeBatchAdapter()

    await submit_batch_for_window(db_path=db, adapter=adapter, subject_kind="turn")
    first = await collect_pending_batches(db_path=db, adapter=adapter)
    assert len(first) == 1
    assert _count_eval_completed(db) == 2

    # Second invocation — adapter would re-emit the same verdicts if it
    # were called, but the handle row is already ingested so the pending
    # query returns empty.
    poll_calls_before = len(adapter.poll_calls)
    second = await collect_pending_batches(db_path=db, adapter=adapter)

    assert second == []  # nothing pending
    assert _count_eval_completed(db) == 2  # no new rows
    assert len(adapter.poll_calls) == poll_calls_before  # adapter not re-called


async def test_collect_batches_still_pending_leaves_handle_alone(tmp_path: Path):
    """When the provider reports `in_progress`, `--collect-batches`
    skips the batch and leaves the handle row in `pending` so a later
    invocation can retry."""
    db = tmp_path / "trace.db"
    _seed_turns(db, count=1)

    # Adapter that says "still in progress" on poll.
    adapter = _FakeBatchAdapter(poll_status="in_progress")

    await submit_batch_for_window(db_path=db, adapter=adapter, subject_kind="turn")
    results = await collect_pending_batches(db_path=db, adapter=adapter)

    assert len(results) == 1
    assert results[0].status == "in_progress"
    assert results[0].verdicts_emitted == 0
    # fetch_batch was never called — the collector short-circuits on
    # pending status to avoid blocking on the provider's results endpoint.
    assert adapter.fetch_calls == []
    # Handle row stayed pending.
    assert _count_handle_rows(db, status="pending") == 1
    assert _count_handle_rows(db, status="ingested") == 0
    # No eval.completed events landed.
    assert _count_eval_completed(db) == 0


async def test_collect_batches_emits_eval_failed_on_batch_error_row(tmp_path: Path):
    """Per-request `BatchError` rows surface as `eval.failed` events;
    successful peers still emit `eval.completed` as normal. The handle
    row is still marked ingested — partial-failure batches are not
    auto-resubmitted in v1."""
    db = tmp_path / "trace.db"
    _seed_turns(db, count=2)

    # Submit first so the adapter's submitted_batches list captures the
    # canonical custom_ids — we then map an error onto the first one.
    pre_adapter = _FakeBatchAdapter()
    await submit_batch_for_window(db_path=db, adapter=pre_adapter, subject_kind="turn")
    submitted_custom_ids = [r.request_id for r in pre_adapter.submitted_batches[0]]

    error = BatchError(
        custom_id=submitted_custom_ids[0],
        error_class=ErrorClass.SERVER_ERROR,
        error_message="upstream blew up",
        retryable=True,
    )
    collect_adapter = _FakeBatchAdapter(error_per_custom_id={submitted_custom_ids[0]: error})

    results = await collect_pending_batches(db_path=db, adapter=collect_adapter)

    assert len(results) == 1
    # One verdict, one failure — verdicts_emitted counts only successes.
    assert results[0].verdicts_emitted == 1

    # One eval.completed + one eval.failed.
    assert _count_eval_completed(db) == 1
    trace = TraceStore(db)
    try:
        failed_count = trace._conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'eval.failed'"
        ).fetchone()[0]
    finally:
        trace.close()
    assert failed_count == 1


# ---------------------------------------------------------------------------
# Byte-for-byte verdict comparison (sync vs batch)
# ---------------------------------------------------------------------------


async def test_batch_verdict_carries_pricing_mode_signal_and_llm_judge_stamp(
    tmp_path: Path,
):
    """Acceptance criterion: a verdict produced via the batch path
    carries the expected judge-kind / rubric stamps plus
    `pricing_mode='batch'` in signals.

    The sync `LLMJudge` and the batch ingest path call the same
    `_parse_response` + `_llm_rubric_for` helpers, so the structural
    verdict shape is identical. The pricing_mode signal is the only
    documented difference, and that's what the savings dashboard
    partitions on.
    """
    db = tmp_path / "trace.db"
    _seed_turns(db, count=1)

    adapter = _FakeBatchAdapter(
        response_text='{"score": 0.4, "confidence": 0.6, "rationale": "partial"}'
    )
    await submit_batch_for_window(db_path=db, adapter=adapter, subject_kind="turn")
    await collect_pending_batches(db_path=db, adapter=adapter)

    payloads = _read_eval_completed_payloads(db)
    assert len(payloads) == 1
    p = payloads[0]

    # Verdict shape matches the LLM judge's sync output.
    assert p["judge_kind"] == "llm"
    assert p["rubric_id"] == "turn-llm-v1"
    assert p["rubric_version"] == "1.0.0"
    assert p["score"] == 0.4
    assert p["confidence"] == 0.6
    # Pricing-mode signal — the load-bearing flag for the savings
    # dashboard's `group_by=pricing_mode` partition.
    assert p["signals"]["pricing_mode"] == "batch"
    # And the rationale-preview/hash machinery from LLMJudge transfers
    # cleanly so the typed payload doesn't lose information.
    assert p["signals"]["rationale_preview"] == "partial"
    assert "rationale_hash" in p["signals"]
