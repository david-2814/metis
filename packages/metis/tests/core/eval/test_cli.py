"""Batch re-evaluation via the `metis evaluate` entry point."""

from __future__ import annotations

from pathlib import Path

from metis.core.eval import reevaluate
from metis.core.trace.store import TraceStore

from .helpers import build_turn_completed, new_turn_id


async def test_reevaluate_produces_new_eval_completed_per_turn(tmp_path: Path):
    """Re-evaluation appends a *new* eval.completed row; the verdict is not in-place updated."""
    db_path = tmp_path / "t.db"
    trace = TraceStore(db_path)
    session_id = "sess_reeval"
    turn_id = new_turn_id()
    trace.write(build_turn_completed(session_id=session_id, turn_id=turn_id))
    trace.close()

    verdicts_a = await reevaluate(db_path=db_path, subject_kind="turn")
    assert len(verdicts_a) == 1
    assert verdicts_a[0].subject_id == turn_id

    # Re-run; the trace store should now hold two eval.completed rows for the same turn.
    verdicts_b = await reevaluate(db_path=db_path, subject_kind="turn")
    assert len(verdicts_b) == 1
    assert verdicts_b[0].eval_id != verdicts_a[0].eval_id

    # Query the trace store directly to confirm both verdicts persisted.
    trace = TraceStore(db_path)
    try:
        rows = trace._conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'eval.completed' AND "
            "json_extract(payload_json, '$.subject_id') = ?",
            (turn_id,),
        ).fetchone()
    finally:
        trace.close()
    assert rows[0] == 2  # two re-eval runs → two appended verdicts


async def test_reevaluate_handles_no_subjects(tmp_path: Path):
    db_path = tmp_path / "empty.db"
    trace = TraceStore(db_path)
    trace.close()
    verdicts = await reevaluate(db_path=db_path, subject_kind="turn")
    assert verdicts == []
