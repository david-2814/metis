"""Batch re-evaluation entry point.

`metis evaluate --since <ts> --subject turn` walks the trace store within
the window and re-runs the configured judge against each subject. Each
re-evaluation produces a *new* `eval.completed` event with a fresh
`eval_id`; older verdicts are preserved (evaluator.md §4.6 / §6.2).

Re-evaluation shares the BudgetTracker with the online subscriber so
caps apply across both modes (evaluator.md §6.2).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from metis_core.eval.budget import BudgetTracker
from metis_core.eval.judge import HeuristicJudge, Judge
from metis_core.eval.subscriber import Evaluator
from metis_core.eval.verdict import EvalSubjectKind, EvalVerdict
from metis_core.events.bus import EventBus
from metis_core.trace.store import TraceStore


async def reevaluate(
    *,
    db_path: str | Path,
    subject_kind: EvalSubjectKind = "turn",
    since: datetime | None = None,
    until: datetime | None = None,
    session_id: str | None = None,
    judge: Judge | None = None,
    budget: BudgetTracker | None = None,
) -> list[EvalVerdict]:
    """Re-run the configured judge over a window.

    The trace store is opened read-only-style (we only call read methods
    on it). The evaluator runs against the same bus so emitted
    `eval.completed` events flow back into the trace store via the
    fast-path trace subscriber — i.e. re-evaluation produces a new
    persisted row.
    """
    trace = TraceStore(db_path)
    bus = EventBus()
    bus.start()
    trace_handle = trace.attach_to(bus, name="trace-store-reevaluate")
    evaluator = Evaluator(
        bus=bus,
        trace=trace,
        judge=judge or HeuristicJudge(),
        budget=budget or BudgetTracker(),
    )
    try:
        subjects = list(_subjects_in_window(trace, subject_kind, since, until, session_id))
        verdicts: list[EvalVerdict] = []
        for sub in subjects:
            verdict = await _evaluate_subject(evaluator, subject_kind, sub)
            if verdict is not None:
                verdicts.append(verdict)
        await bus.drain()
        return verdicts
    finally:
        bus.unsubscribe(trace_handle)
        await bus.stop()
        trace.close()


def _subjects_in_window(
    trace: TraceStore,
    subject_kind: EvalSubjectKind,
    since: datetime | None,
    until: datetime | None,
    session_id: str | None,
) -> Iterable[dict]:
    """Yield {session_id, subject_id} dicts for each subject of the right kind.

    Walks the trace store's events table directly via TraceStore's connection
    — keeping the SQL local rather than expanding the public API.
    """
    sql, params = _subject_query(subject_kind, since, until, session_id)
    cursor = trace._conn.execute(sql, params)
    rows = cursor.fetchall()
    for row in rows:
        sid, subject_id, turn_id = row[0], row[1], row[2]
        yield {"session_id": sid, "subject_id": subject_id, "turn_id": turn_id}


def _subject_query(
    subject_kind: EvalSubjectKind,
    since: datetime | None,
    until: datetime | None,
    session_id: str | None,
) -> tuple[str, list]:
    where = []
    params: list = []
    if subject_kind == "turn":
        where.append("type = ?")
        params.append("turn.completed")
        select_subject = "turn_id"
    elif subject_kind == "tool_cycle":
        where.append("type IN (?, ?)")
        params.extend(["tool.completed", "tool.failed"])
        # Subject is tool_use_id; pulled from payload_json at the python level.
        select_subject = "json_extract(payload_json, '$.tool_use_id')"
    elif subject_kind == "session":
        where.append("type = ?")
        params.append("session.ended")
        select_subject = "session_id"
    else:
        raise ValueError(f"unknown subject_kind: {subject_kind}")
    if since is not None:
        where.append("timestamp_us >= ?")
        params.append(_to_micros(since))
    if until is not None:
        where.append("timestamp_us <= ?")
        params.append(_to_micros(until))
    if session_id is not None:
        where.append("session_id = ?")
        params.append(session_id)
    sql = (
        f"SELECT DISTINCT session_id, {select_subject}, turn_id FROM events "
        f"WHERE {' AND '.join(where)} ORDER BY id"
    )
    return sql, params


def _to_micros(dt: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=dt.tzinfo or UTC)
    delta = (dt if dt.tzinfo else dt.replace(tzinfo=UTC)) - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


async def _evaluate_subject(
    evaluator: Evaluator,
    subject_kind: EvalSubjectKind,
    sub: dict,
) -> EvalVerdict | None:
    if subject_kind == "turn":
        return await evaluator.evaluate_turn(
            session_id=sub["session_id"],
            turn_id=sub["subject_id"],
            trigger="batch",
        )
    if subject_kind == "tool_cycle":
        if not sub["subject_id"]:
            return None
        return await evaluator.evaluate_tool_cycle(
            session_id=sub["session_id"],
            turn_id=sub["turn_id"],
            tool_use_id=sub["subject_id"],
            trigger="batch",
        )
    if subject_kind == "session":
        return await evaluator.evaluate_session(
            session_id=sub["session_id"],
            trigger="batch",
        )
    return None


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for `metis evaluate`.

    Wired by `metis_cli.main` so users can re-evaluate without spinning
    up the full chat runtime. Reads a trace DB, runs the heuristic judge
    over each matching subject, and prints a one-line summary.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="metis evaluate",
        description="Re-evaluate subjects in a trace DB with the heuristic judge.",
    )
    parser.add_argument("--db-path", required=True, help="Trace DB path.")
    parser.add_argument(
        "--subject",
        choices=("turn", "tool_cycle", "session"),
        default="turn",
        help="Subject kind to re-evaluate (default: turn).",
    )
    parser.add_argument("--since", help="ISO 8601 UTC start of window (inclusive).")
    parser.add_argument("--until", help="ISO 8601 UTC end of window (inclusive).")
    parser.add_argument(
        "--session-id",
        help="Restrict to a single session (default: all sessions in window).",
    )
    args = parser.parse_args(argv)

    since = datetime.fromisoformat(args.since) if args.since else None
    until = datetime.fromisoformat(args.until) if args.until else None

    verdicts = asyncio.run(
        reevaluate(
            db_path=args.db_path,
            subject_kind=args.subject,
            since=since,
            until=until,
            session_id=args.session_id,
        )
    )
    print(f"re-evaluated {len(verdicts)} {args.subject}(s):")
    for v in verdicts:
        print(
            f"  {v.subject_id} score={v.score:.3f} confidence={v.confidence:.3f} "
            f"judge={v.judge_kind} rubric={v.rubric_id}@{v.rubric_version}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
