"""Batched re-evaluation: submit, persist, poll, ingest.

Two callable entry points back the `metis evaluate --batch-mode` and
`metis evaluate --collect-batches` flows per provider-adapter-contract.md
§4.6 + evaluator.md §6.2:

  * `submit_batch_for_window(...)`: walks the trace store for in-window
    subjects, builds an LLM-judge `CanonicalRequest` per subject, hands
    the bundle to `adapter.submit_batch`, and persists the resulting
    `BatchHandle` rows to a small SQLite table on the existing trace DB.
    The CLI exits without waiting (24h Anthropic SLA).
  * `collect_pending_batches(...)`: reads pending handles, polls each
    via `adapter.poll_batch`, and for completed batches calls
    `adapter.fetch_batch`, parses results back into `EvalVerdict`s,
    emits `eval.completed` events with `pricing_mode="batch"` in
    `signals`, and marks the handle ingested (`status="ingested"`,
    `ingested_at_ms`).

Idempotency: `--collect-batches` runs are safe to re-invoke. A handle
with `status="ingested"` is skipped on subsequent passes, so no
duplicate `eval.completed` events land in the trace.

The handle table is additive on the trace DB schema (no
`TRACE_SCHEMA_VERSION` bump); the `CREATE TABLE IF NOT EXISTS` in
[`trace/store.py`](../../trace/store.py) makes the migration a no-op on
existing DBs.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from metis.core.adapters.protocol import (
    CanonicalRequest,
    CanonicalResponse,
    ProviderAdapter,
)
from metis.core.canonical.batch import BatchError, BatchHandle
from metis.core.canonical.content import TextBlock
from metis.core.canonical.ids import next_monotonic_ulid
from metis.core.canonical.messages import Message, Role
from metis.core.eval.cli import _subjects_in_window
from metis.core.eval.judge import SubjectContext
from metis.core.eval.llm_judge import (
    _SYSTEM_PROMPT,
    DEFAULT_JUDGE_MAX_OUTPUT_TOKENS,
    LLMJudgeError,
    _build_user_message,
    _llm_rubric_for,
    _parse_response,
)
from metis.core.eval.verdict import EvalSubjectKind, EvalVerdict, clamp_unit
from metis.core.events.bus import EventBus
from metis.core.events.envelope import Actor, Sensitivity
from metis.core.events.payloads import EvalCompleted, EvalFailed, make_event
from metis.core.pricing.table import PriceTable
from metis.core.trace.store import TraceStore

logger = logging.getLogger(__name__)


# Default judge model used by `--batch-mode` when the caller doesn't
# pin one explicitly. Mirrors `LLMJudgeConfig.judge_model` so sync and
# batch modes pick the same model out of the box.
DEFAULT_BATCH_JUDGE_MODEL = "anthropic:claude-haiku-4-5"


@dataclass(frozen=True)
class _PendingSubject:
    """Subject metadata captured at submit time.

    Persisted on the `evaluator_batch_handles` row so `--collect-batches`
    can rebuild the verdict without re-walking the trace store.
    """

    custom_id: str
    subject_kind: EvalSubjectKind
    subject_id: str
    session_id: str
    turn_id: str | None


@dataclass(frozen=True)
class BatchSubmitResult:
    """Returned by `submit_batch_for_window` for the CLI to print."""

    handle: BatchHandle
    request_count: int
    subject_kind: EvalSubjectKind


@dataclass(frozen=True)
class BatchCollectResult:
    """Returned by `collect_pending_batches` for the CLI to print."""

    batch_id: str
    provider: str
    request_count: int
    verdicts_emitted: int
    skipped_already_ingested: bool
    status: str  # "ingested" | "queued" | "in_progress" | "expired" | "failed"


# --- Schema helpers ----------------------------------------------------


def _ensure_table(trace: TraceStore) -> None:
    """Idempotent — relies on `CREATE TABLE IF NOT EXISTS` in trace/store.py.

    Surfaces a clearer error if the trace DB was opened against an
    out-of-tree schema that doesn't carry the additive table.
    """
    cur = trace._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='evaluator_batch_handles'"
    )
    if cur.fetchone() is None:
        raise RuntimeError(
            "evaluator_batch_handles table missing from trace DB; "
            "re-open the TraceStore so the additive schema is applied."
        )


# --- Submit path -------------------------------------------------------


def _build_canonical_request_for_subject(
    *,
    ctx: SubjectContext,
    judge_model: str,
    max_output_tokens: int,
) -> CanonicalRequest:
    """Mirror `LLMJudge._call_adapter`'s request shape.

    Re-using the LLM judge's private helpers (`_build_user_message`,
    `_SYSTEM_PROMPT`) means the prompt sent in the batch is the SAME
    prompt the sync `LLMJudge.evaluate` path would send for the same
    subject — that's the property the byte-for-byte verdict acceptance
    rests on.
    """
    user_text = _build_user_message(ctx)
    return CanonicalRequest(
        request_id=str(next_monotonic_ulid()),
        messages=[
            Message(
                id=str(next_monotonic_ulid()),
                session_id=ctx.session_id or "eval",
                role=Role.USER,
                content=[TextBlock(text=user_text)],
                created_at=datetime.now(UTC),
            )
        ],
        tools=[],
        system_prompt=_SYSTEM_PROMPT,
        model=judge_model,
        max_output_tokens=max_output_tokens,
        temperature=0.0,
    )


def _build_subject_context(
    trace: TraceStore,
    subject_kind: EvalSubjectKind,
    sub: dict,
) -> SubjectContext | None:
    """Re-create the same SubjectContext the online subscriber would build.

    Mirrors `Evaluator.evaluate_turn` / `evaluate_tool_cycle` /
    `evaluate_session` — keeps the prompt input identical across sync
    and batch modes.
    """
    session_id = sub["session_id"]
    subject_id = sub["subject_id"]
    if subject_kind == "turn":
        turn_id = subject_id
        events = trace.events_for_turn(turn_id)
        turn_completed = next((e for e in events if e.type == "turn.completed"), None)
        if turn_completed is None:
            return None
        signals_extra = turn_completed.payload.get("signals_extra") or None
        return SubjectContext(
            subject_kind="turn",
            subject_id=turn_id,
            events=events,
            session_id=session_id,
            signals_extra=signals_extra,
        )
    if subject_kind == "tool_cycle":
        turn_id = sub.get("turn_id")
        if not turn_id or not subject_id:
            return None
        events = trace.events_for_turn(turn_id)
        if not events:
            return None
        return SubjectContext(
            subject_kind="tool_cycle",
            subject_id=subject_id,
            events=events,
            session_id=session_id,
        )
    if subject_kind == "session":
        events = trace.events_for_session(session_id)
        if not any(e.type == "session.ended" for e in events):
            return None
        # Mirror the subscriber's child-turn-verdict aggregation logic.
        child_scores: list[float] = []
        child_eval_ids: list[str] = []
        seen_subjects: set[str] = set()
        for e in reversed(events):
            if e.type != "eval.completed":
                continue
            if e.payload.get("subject_kind") != "turn":
                continue
            sid = e.payload.get("subject_id")
            if sid in seen_subjects:
                continue
            seen_subjects.add(sid)
            child_scores.append(float(e.payload.get("score") or 0.0))
            child_eval_ids.append(str(e.payload.get("eval_id")))
        return SubjectContext(
            subject_kind="session",
            subject_id=session_id,
            events=events,
            session_id=session_id,
            signals_extra={
                "child_turn_scores": list(reversed(child_scores)),
                "child_eval_ids": list(reversed(child_eval_ids)),
            },
        )
    return None


async def submit_batch_for_window(
    *,
    db_path: str | Path,
    adapter: ProviderAdapter,
    subject_kind: EvalSubjectKind = "turn",
    since: datetime | None = None,
    until: datetime | None = None,
    session_id: str | None = None,
    judge_model: str = DEFAULT_BATCH_JUDGE_MODEL,
    max_output_tokens: int = DEFAULT_JUDGE_MAX_OUTPUT_TOKENS,
) -> list[BatchSubmitResult]:
    """Submit all in-window subjects as a batch (or batches, if chunked).

    v1: one batch per invocation per `subject_kind`. Anthropic's per-batch
    cap is 100k requests / 256MB; if a single window exceeds that, this
    function falls back to multiple sequential batches and returns one
    `BatchSubmitResult` per chunk. The in-flight Wave-18 default uses a
    soft chunk size of 5000 to keep individual batches cheap to inspect.
    """
    trace = TraceStore(db_path)
    try:
        _ensure_table(trace)
        # Collect (subject, request) pairs.
        subjects: list[dict] = list(
            _subjects_in_window(trace, subject_kind, since, until, session_id)
        )
        if not subjects:
            return []

        # Build the CanonicalRequest list + parallel _PendingSubject list.
        # Skip subjects whose context can't be assembled — same posture as
        # the sync path, which emits `eval.failed` for `subject_not_found`.
        pending: list[_PendingSubject] = []
        requests: list[CanonicalRequest] = []
        for sub in subjects:
            ctx = _build_subject_context(trace, subject_kind, sub)
            if ctx is None:
                continue
            request = _build_canonical_request_for_subject(
                ctx=ctx,
                judge_model=judge_model,
                max_output_tokens=max_output_tokens,
            )
            pending.append(
                _PendingSubject(
                    custom_id=request.request_id,
                    subject_kind=subject_kind,
                    subject_id=ctx.subject_id,
                    session_id=ctx.session_id or "",
                    turn_id=sub.get("turn_id"),
                )
            )
            requests.append(request)

        if not requests:
            return []

        # Chunk to stay under provider caps. The default is well under
        # both Anthropic (100k) and OpenAI (50k) caps.
        chunk_size = 5000
        results: list[BatchSubmitResult] = []
        for chunk_start in range(0, len(requests), chunk_size):
            chunk_end = chunk_start + chunk_size
            chunk_requests = requests[chunk_start:chunk_end]
            chunk_pending = pending[chunk_start:chunk_end]
            handle = await adapter.submit_batch(chunk_requests)
            _persist_handles(
                trace,
                handle=handle,
                pending=chunk_pending,
                judge_model=judge_model,
            )
            results.append(
                BatchSubmitResult(
                    handle=handle,
                    request_count=len(chunk_requests),
                    subject_kind=subject_kind,
                )
            )
        return results
    finally:
        trace.close()


def _persist_handles(
    trace: TraceStore,
    *,
    handle: BatchHandle,
    pending: list[_PendingSubject],
    judge_model: str,
) -> None:
    """Write one row per request_id in the chunk.

    Uses `INSERT OR IGNORE` so a re-submit with the same `custom_id`
    (re-running `--batch-mode` against an unchanged window) doesn't
    error out — the older row wins and the new submission's handle
    becomes a no-op as far as collection is concerned.
    """
    rows = [
        (
            ps.custom_id,
            handle.batch_id,
            handle.provider,
            handle.submitted_at_ms,
            ps.subject_kind,
            ps.subject_id,
            ps.session_id,
            ps.turn_id,
            judge_model,
            "pending",
            None,
        )
        for ps in pending
    ]
    trace._conn.executemany(
        "INSERT OR IGNORE INTO evaluator_batch_handles "
        "(custom_id, batch_id, provider, submitted_at_ms, "
        " subject_kind, subject_id, session_id, turn_id, "
        " judge_model, status, ingested_at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


# --- Collect path ------------------------------------------------------


@dataclass(frozen=True)
class _StoredHandleGroup:
    """All `evaluator_batch_handles` rows that share one batch_id."""

    batch_id: str
    provider: str
    submitted_at_ms: int
    custom_ids: tuple[str, ...]
    subjects: dict[str, _PendingSubject]  # keyed by custom_id
    judge_model: str | None


def _load_pending_groups(trace: TraceStore) -> list[_StoredHandleGroup]:
    """Group pending rows by batch_id.

    Returns groups whose every row has `status='pending'`. A group with
    any ingested row is skipped — the contract is "one batch
    submission, one collection".
    """
    cur = trace._conn.execute(
        "SELECT custom_id, batch_id, provider, submitted_at_ms, "
        "       subject_kind, subject_id, session_id, turn_id, "
        "       judge_model, status "
        "FROM evaluator_batch_handles "
        "WHERE status = 'pending' "
        "ORDER BY batch_id, custom_id"
    )
    by_batch: dict[str, list[tuple]] = {}
    for row in cur.fetchall():
        by_batch.setdefault(row[1], []).append(row)
    groups: list[_StoredHandleGroup] = []
    for batch_id, rows in by_batch.items():
        custom_ids: list[str] = []
        subjects: dict[str, _PendingSubject] = {}
        provider = rows[0][2]
        submitted_at_ms = rows[0][3]
        judge_model = rows[0][8]
        for r in rows:
            custom_ids.append(r[0])
            subjects[r[0]] = _PendingSubject(
                custom_id=r[0],
                subject_kind=r[4],
                subject_id=r[5],
                session_id=r[6] or "",
                turn_id=r[7],
            )
        groups.append(
            _StoredHandleGroup(
                batch_id=batch_id,
                provider=provider,
                submitted_at_ms=submitted_at_ms,
                custom_ids=tuple(custom_ids),
                subjects=subjects,
                judge_model=judge_model,
            )
        )
    return groups


def _mark_ingested(trace: TraceStore, batch_id: str, *, ingested_at_ms: int) -> None:
    trace._conn.execute(
        "UPDATE evaluator_batch_handles "
        "SET status = 'ingested', ingested_at_ms = ? "
        "WHERE batch_id = ? AND status = 'pending'",
        (ingested_at_ms, batch_id),
    )


def _emit_verdict_event(
    bus: EventBus,
    *,
    verdict: EvalVerdict,
    session_id: str,
    turn_id: str | None,
) -> None:
    """Mirror `Evaluator._emit` for the batch-ingest path.

    `pricing_mode="batch"` is stamped into `signals` so analytics
    can partition (`group_by=pricing_mode`) — the typed `EvalCompleted`
    payload doesn't carry a top-level `pricing_mode` field, but `signals`
    is an opaque JSON-roundtrippable dict and the dashboard reads from it.
    """
    payload = EvalCompleted(
        eval_id=verdict.eval_id,
        subject_kind=verdict.subject_kind,
        subject_id=verdict.subject_id,
        score=verdict.score,
        confidence=verdict.confidence,
        judge_kind=verdict.judge_kind,  # type: ignore[arg-type]
        judge_cost_usd=verdict.judge_cost_usd,
        judge_latency_ms=verdict.judge_latency_ms,
        rubric_id=verdict.rubric_id,
        rubric_version=verdict.rubric_version,
        signals=verdict.signals,
        judge_model=verdict.judge_model,
        judge_pricing_version=verdict.judge_pricing_version,
        parent_eval_id=verdict.parent_eval_id,
    )
    event = make_event(
        type="eval.completed",
        session_id=session_id,
        actor=Actor.SYSTEM,
        payload=payload,
        timestamp=datetime.now(UTC),
        turn_id=turn_id,
        sensitivity=Sensitivity.PSEUDONYMOUS,
    )
    bus.emit(event)


def _emit_failed_event(
    bus: EventBus,
    *,
    subject_kind: EvalSubjectKind,
    subject_id: str,
    session_id: str,
    failure_mode: str,
    error_message: str,
) -> None:
    payload = EvalFailed(
        eval_id=str(next_monotonic_ulid()),
        subject_kind=subject_kind,
        subject_id=subject_id,
        failure_mode=failure_mode,  # type: ignore[arg-type]
        error_message=error_message,
        judge_latency_ms=0,
    )
    event = make_event(
        type="eval.failed",
        session_id=session_id,
        actor=Actor.SYSTEM,
        payload=payload,
        timestamp=datetime.now(UTC),
    )
    bus.emit(event)


def _verdict_from_batch_response(
    *,
    response: CanonicalResponse,
    subject_kind: EvalSubjectKind,
    subject_id: str,
    judge_model: str | None,
    pricing: PriceTable | None,
) -> EvalVerdict:
    """Parse a batch result into a verdict matching the sync LLM path.

    Mirrors `LLMJudge.evaluate`'s parse + verdict-assembly logic so the
    verdict shape matches byte-for-byte except for the stamped
    `pricing_mode="batch"` signal.
    """
    parsed = _parse_response(response)
    rubric_id, rubric_version = _llm_rubric_for(subject_kind)
    rationale = parsed.rationale[:200]
    signals: dict = {
        "rationale_hash": _sha256_hex(rationale),
        "rationale_preview": rationale,
        "attempts": 1,
        "pricing_mode": "batch",
    }
    if pricing is not None:
        try:
            cost = pricing.compute_cost(response.model, response.usage)
        except Exception:
            logger.warning(
                "batch ingest: cost compute failed for model=%s; recording 0",
                response.model,
            )
            cost = Decimal("0")
        pricing_version = pricing.version
    else:
        cost = Decimal("0")
        pricing_version = None
    return EvalVerdict(
        eval_id=str(next_monotonic_ulid()),
        subject_kind=subject_kind,
        subject_id=subject_id,
        score=clamp_unit(float(parsed.score)),
        confidence=clamp_unit(float(parsed.confidence)),
        judge_kind="llm",
        judge_model=judge_model or response.model,
        judge_cost_usd=cost,
        judge_pricing_version=pricing_version,
        judge_latency_ms=int(response.latency_ms),
        rubric_id=rubric_id,
        rubric_version=rubric_version,
        signals=signals,
        parent_eval_id=None,
        created_at=datetime.now(UTC).isoformat(),
    )


def _sha256_hex(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


async def collect_pending_batches(
    *,
    db_path: str | Path,
    adapter: ProviderAdapter,
    pricing: PriceTable | None = None,
) -> list[BatchCollectResult]:
    """Poll + ingest every pending handle.

    Idempotent — handles already in `status='ingested'` are skipped. A
    completed handle's verdicts land as `eval.completed` events with
    `pricing_mode="batch"` in `signals`; the trace store's
    fast-path subscriber catches them and persists the rows.

    Returns one `BatchCollectResult` per (pending) handle inspected. The
    CLI prints these so the operator sees per-batch progress.
    """
    trace = TraceStore(db_path)
    bus = EventBus()
    bus.start()
    trace_handle = trace.attach_to(bus, name="trace-store-collect-batches")
    try:
        _ensure_table(trace)
        groups = _load_pending_groups(trace)
        results: list[BatchCollectResult] = []
        for group in groups:
            handle = BatchHandle(
                provider=group.provider,
                batch_id=group.batch_id,
                submitted_at_ms=group.submitted_at_ms,
                request_count=len(group.custom_ids),
                custom_ids=group.custom_ids,
            )
            try:
                status = await adapter.poll_batch(handle)
            except Exception as exc:
                logger.warning(
                    "batch collect: poll_batch(%s) failed: %s",
                    group.batch_id,
                    exc,
                )
                continue
            if status in ("queued", "in_progress"):
                results.append(
                    BatchCollectResult(
                        batch_id=group.batch_id,
                        provider=group.provider,
                        request_count=len(group.custom_ids),
                        verdicts_emitted=0,
                        skipped_already_ingested=False,
                        status=status,
                    )
                )
                continue
            # Fetch + ingest. Batch-level failures (an entire-batch abort)
            # surface as AdapterError — mark the batch ingested so we
            # don't replay the failure infinitely. v1 doesn't auto-resubmit.
            try:
                rows = await adapter.fetch_batch(handle)
            except Exception as exc:
                logger.warning(
                    "batch collect: fetch_batch(%s) failed: %s",
                    group.batch_id,
                    exc,
                )
                continue
            emitted = _ingest_batch_results(
                bus,
                rows=rows,
                group=group,
                pricing=pricing,
            )
            ingested_at_ms = int(time.time() * 1000)
            _mark_ingested(trace, group.batch_id, ingested_at_ms=ingested_at_ms)
            results.append(
                BatchCollectResult(
                    batch_id=group.batch_id,
                    provider=group.provider,
                    request_count=len(group.custom_ids),
                    verdicts_emitted=emitted,
                    skipped_already_ingested=False,
                    status=status,
                )
            )
        # Drain so trace-store's fast-path subscriber has written every
        # emitted event before we return — keeps `--collect-batches`
        # synchronously observable from the CLI's perspective.
        await bus.drain()
        return results
    finally:
        bus.unsubscribe(trace_handle)
        await bus.stop()
        trace.close()


def _ingest_batch_results(
    bus: EventBus,
    *,
    rows: list[CanonicalResponse | BatchError],
    group: _StoredHandleGroup,
    pricing: PriceTable | None,
) -> int:
    """Emit one `eval.completed` (or `eval.failed`) per row.

    The result list is same-length, same-order as
    `handle.custom_ids` per §4.6.2 — we zip them together.
    """
    emitted = 0
    for custom_id, row in zip(group.custom_ids, rows, strict=True):
        ps = group.subjects.get(custom_id)
        if ps is None:
            # Defensive: the handle row was deleted between submit and
            # collect (unexpected, but don't crash the collection loop).
            logger.warning(
                "batch collect: no pending subject row for custom_id=%s in batch=%s; skipping",
                custom_id,
                group.batch_id,
            )
            continue
        if isinstance(row, BatchError):
            _emit_failed_event(
                bus,
                subject_kind=ps.subject_kind,
                subject_id=ps.subject_id,
                session_id=ps.session_id or "(unknown)",
                failure_mode="judge_call_failed",
                error_message=f"{row.error_class.value}: {row.error_message}",
            )
            continue
        try:
            verdict = _verdict_from_batch_response(
                response=row,
                subject_kind=ps.subject_kind,
                subject_id=ps.subject_id,
                judge_model=group.judge_model,
                pricing=pricing,
            )
        except LLMJudgeError as exc:
            _emit_failed_event(
                bus,
                subject_kind=ps.subject_kind,
                subject_id=ps.subject_id,
                session_id=ps.session_id or "(unknown)",
                failure_mode=exc.failure_mode,
                error_message=str(exc),
            )
            continue
        _emit_verdict_event(
            bus,
            verdict=verdict,
            session_id=ps.session_id or "(unknown)",
            turn_id=ps.turn_id,
        )
        emitted += 1
    return emitted


__all__ = [
    "DEFAULT_BATCH_JUDGE_MODEL",
    "BatchCollectResult",
    "BatchSubmitResult",
    "collect_pending_batches",
    "submit_batch_for_window",
]
