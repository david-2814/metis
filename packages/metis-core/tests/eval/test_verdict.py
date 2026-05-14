"""EvalVerdict shape round-trip + clamp helpers."""

from __future__ import annotations

from decimal import Decimal

import msgspec
from metis_core.eval import EvalVerdict, clamp_unit


def test_eval_verdict_roundtrip():
    verdict = EvalVerdict(
        eval_id="01HZEVAL000000000000000001",
        subject_kind="turn",
        subject_id="01HZTURN0000000000000000001",
        score=0.82,
        confidence=0.71,
        judge_kind="heuristic",
        judge_cost_usd=Decimal("0"),
        judge_latency_ms=1,
        rubric_id="turn-heuristic-v1",
        rubric_version="1.0.0",
        signals={"flags": ["stop_reason_clean"], "flags_negative": []},
        created_at="2026-05-13T00:00:00+00:00",
    )
    blob = msgspec.json.encode(verdict)
    decoded = msgspec.json.decode(blob, type=EvalVerdict)
    assert decoded == verdict
    assert decoded.judge_cost_usd == Decimal("0")
    assert decoded.judge_model is None


def test_clamp_unit_bounds():
    assert clamp_unit(-0.1) == 0.0
    assert clamp_unit(0.5) == 0.5
    assert clamp_unit(1.5) == 1.0


def test_verdict_carries_optional_fields():
    verdict = EvalVerdict(
        eval_id="x",
        subject_kind="workload",
        subject_id="w",
        score=1.0,
        confidence=1.0,
        judge_kind="hybrid",
        judge_cost_usd=Decimal("0.0042"),
        judge_latency_ms=15,
        rubric_id="r",
        rubric_version="v",
        signals={},
        created_at="2026-05-13T00:00:00+00:00",
        judge_model="anthropic:claude-haiku-4-5",
        judge_pricing_version="test-1",
        parent_eval_id="parent",
    )
    assert verdict.judge_pricing_version == "test-1"
    assert verdict.parent_eval_id == "parent"
