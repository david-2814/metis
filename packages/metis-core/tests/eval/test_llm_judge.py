"""LLMJudge and HybridJudge contract tests (evaluator.md §5.2 / §5.3).

These exercise the contract without spending real money: the scripted
adapter returns canned text-block content that the judge parses against
its JSON schema. Live-API validation lives in `scripts/smoke_eval.py`.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from metis_core.adapters.protocol import StopReason
from metis_core.canonical.content import TextBlock
from metis_core.eval.budget import BudgetTracker
from metis_core.eval.judge import SubjectContext
from metis_core.eval.llm_judge import (
    DEFAULT_ESCALATION_THRESHOLD,
    HybridJudge,
    LLMJudge,
    LLMJudgeConfig,
    LLMJudgeError,
)
from metis_core.pricing.table import DEFAULT_PRICE_TABLE

from tests_shared.scripted_adapter import _ScriptedAnthropicAdapter, _ScriptedResponse

from .helpers import (
    build_llm_completed,
    build_tool_called,
    build_tool_completed,
    build_tool_failed,
    build_turn_completed,
    new_tool_use_id,
    new_turn_id,
)

SESSION_ID = "sess_llmjudge_test"


def _judge_response(*, score: float, confidence: float, rationale: str = "ok") -> _ScriptedResponse:
    """Build a scripted adapter response that returns a parseable judge JSON."""
    return _ScriptedResponse(
        content=[
            TextBlock(
                text=json.dumps({"score": score, "confidence": confidence, "rationale": rationale})
            )
        ],
        stop_reason=StopReason.END_TURN,
        input_tokens=1200,
        output_tokens=80,
    )


def _clean_turn_ctx(session_id: str = SESSION_ID) -> SubjectContext:
    """Build a turn-subject context with a clean lifecycle (high-confidence path)."""
    turn_id = new_turn_id()
    events = [
        build_llm_completed(session_id=session_id, turn_id=turn_id),
        build_turn_completed(session_id=session_id, turn_id=turn_id),
    ]
    return SubjectContext(
        subject_kind="turn",
        subject_id=turn_id,
        events=events,
        session_id=session_id,
        signals_extra={
            "user_prompt_text": "Help me factor 1729.",
            "assistant_response_text": "1729 = 7 * 13 * 19.",
        },
    )


def _messy_turn_ctx(session_id: str = SESSION_ID) -> SubjectContext:
    """Build a turn context with one tool failure → heuristic confidence dips below the threshold."""
    turn_id = new_turn_id()
    tool_use_id = new_tool_use_id()
    events = [
        build_tool_called(
            session_id=session_id,
            turn_id=turn_id,
            tool_use_id=tool_use_id,
            tool_name="read_file",
        ),
        build_tool_failed(session_id=session_id, turn_id=turn_id, tool_use_id=tool_use_id),
        build_llm_completed(session_id=session_id, turn_id=turn_id),
        build_turn_completed(session_id=session_id, turn_id=turn_id, stop_reason="end_turn"),
    ]
    return SubjectContext(
        subject_kind="turn",
        subject_id=turn_id,
        events=events,
        session_id=session_id,
        signals_extra={
            "user_prompt_text": "Read file X.",
            "assistant_response_text": "I tried but the read failed.",
        },
    )


def _build_llm_judge(
    *,
    responses: list[_ScriptedResponse],
    budget: BudgetTracker | None = None,
    config: LLMJudgeConfig | None = None,
    rubric_provider=None,
) -> tuple[LLMJudge, _ScriptedAnthropicAdapter]:
    adapter = _ScriptedAnthropicAdapter(responses)
    judge = LLMJudge(
        adapter=adapter,
        pricing=DEFAULT_PRICE_TABLE,
        config=config or LLMJudgeConfig(),
        budget=budget or BudgetTracker(),
        rubric_provider=rubric_provider,
    )
    return judge, adapter


# ----- LLMJudge --------------------------------------------------------------


async def test_llm_judge_parses_clean_json() -> None:
    """Happy path: scripted JSON → parsed verdict with score/confidence."""
    judge, adapter = _build_llm_judge(
        responses=[_judge_response(score=0.9, confidence=0.8, rationale="clear win")]
    )
    verdict = await judge.evaluate(_clean_turn_ctx())
    assert verdict.judge_kind == "llm"
    assert verdict.score == 0.9
    assert verdict.confidence == 0.8
    assert verdict.judge_cost_usd > Decimal("0")
    assert verdict.judge_pricing_version == DEFAULT_PRICE_TABLE.version
    assert verdict.judge_model == "anthropic:claude-haiku-4-5"
    assert verdict.rubric_id == "turn-llm-v1"
    assert verdict.signals.get("rationale_preview") == "clear win"
    # Adapter was called once.
    assert len(adapter.requests) == 1


async def test_llm_judge_tolerates_preamble_around_json() -> None:
    """Small models sometimes wrap the JSON in markdown; the judge should still parse it."""
    judge, _ = _build_llm_judge(
        responses=[
            _ScriptedResponse(
                content=[
                    TextBlock(
                        text=(
                            "Sure, here is my analysis:\n```json\n"
                            '{"score": 0.7, "confidence": 0.6, "rationale": "partial"}\n'
                            "```\nThat's my call."
                        )
                    )
                ],
                stop_reason=StopReason.END_TURN,
                input_tokens=1200,
                output_tokens=120,
            )
        ]
    )
    verdict = await judge.evaluate(_clean_turn_ctx())
    assert verdict.score == 0.7
    assert verdict.confidence == 0.6


async def test_llm_judge_parse_failure_retries_then_raises() -> None:
    """Two parse failures (one retry) → LLMJudgeError surfaced."""
    bad = _ScriptedResponse(
        content=[TextBlock(text="this is not JSON at all")],
        stop_reason=StopReason.END_TURN,
    )
    judge, adapter = _build_llm_judge(
        responses=[bad, bad],
        config=LLMJudgeConfig(max_retries=1),
    )
    with pytest.raises(LLMJudgeError) as exc_info:
        await judge.evaluate(_clean_turn_ctx())
    assert exc_info.value.failure_mode == "judge_output_invalid"
    # Two attempts: initial + one retry.
    assert len(adapter.requests) == 2


async def test_llm_judge_rejects_score_out_of_range() -> None:
    """JSON parses but score is invalid → LLMJudgeError."""
    judge, _ = _build_llm_judge(
        responses=[
            _ScriptedResponse(
                content=[
                    TextBlock(text=json.dumps({"score": 1.5, "confidence": 0.5, "rationale": "x"}))
                ],
                stop_reason=StopReason.END_TURN,
            ),
            _ScriptedResponse(
                content=[
                    TextBlock(text=json.dumps({"score": 1.5, "confidence": 0.5, "rationale": "x"}))
                ],
                stop_reason=StopReason.END_TURN,
            ),
        ],
        config=LLMJudgeConfig(max_retries=1),
    )
    with pytest.raises(LLMJudgeError):
        await judge.evaluate(_clean_turn_ctx())


async def test_llm_judge_budget_cap_returns_low_confidence_verdict() -> None:
    """Pre-populate budget so the projected cost exhausts the cap. Judge
    refuses the LLM call and emits a confidence=0 budget_exhausted verdict.

    This is the LLMJudge's "I refuse to call" signal. Score is neutral (0.5)
    so a downstream consumer treating it as a real score doesn't accidentally
    drive routing toward or away from anything; the gate is confidence=0.
    """
    budget = BudgetTracker(
        per_session_max_usd=Decimal("0.0001"),
        per_day_max_usd=Decimal("1.0"),
    )
    # Pre-charge to push the session over its tiny cap.
    budget.record(session_id=SESSION_ID, cost_usd=Decimal("0.001"))
    judge, adapter = _build_llm_judge(
        responses=[_judge_response(score=0.9, confidence=0.9)],
        budget=budget,
    )
    verdict = await judge.evaluate(_clean_turn_ctx())
    assert verdict.judge_kind == "llm"
    assert verdict.confidence == 0.0
    assert verdict.signals.get("budget_exhausted") is True
    assert verdict.signals.get("throttled_reason") == "session_cap"
    # Adapter was NOT called.
    assert len(adapter.requests) == 0
    # Score is neutral, not zero — see docstring.
    assert verdict.score == 0.5
    # No spend recorded.
    assert verdict.judge_cost_usd == Decimal("0")


async def test_llm_judge_daily_cap_fires() -> None:
    """Day cap exhausts → throttled_reason='daily_cap'."""
    budget = BudgetTracker(
        per_session_max_usd=Decimal("10.0"),
        per_day_max_usd=Decimal("0.0001"),
    )
    budget.record(session_id="other_session", cost_usd=Decimal("0.001"))
    judge, adapter = _build_llm_judge(
        responses=[_judge_response(score=0.9, confidence=0.9)],
        budget=budget,
    )
    verdict = await judge.evaluate(_clean_turn_ctx())
    assert verdict.signals.get("budget_exhausted") is True
    assert verdict.signals.get("throttled_reason") == "daily_cap"
    assert len(adapter.requests) == 0


async def test_llm_judge_delegates_for_non_turn_subjects() -> None:
    """tool_cycle / session subjects are heuristic-only in v1 (§5.5/§5.6).

    LLMJudge transparently delegates so callers can swap it in without
    surprising the spec's heuristic-only commitment for those kinds.
    """
    judge, adapter = _build_llm_judge(
        responses=[_judge_response(score=0.5, confidence=0.5)],
    )
    turn_id = new_turn_id()
    tool_use_id = new_tool_use_id()
    ctx = SubjectContext(
        subject_kind="tool_cycle",
        subject_id=tool_use_id,
        events=[
            build_tool_called(
                session_id=SESSION_ID,
                turn_id=turn_id,
                tool_use_id=tool_use_id,
                tool_name="read",
            ),
            build_tool_completed(session_id=SESSION_ID, turn_id=turn_id, tool_use_id=tool_use_id),
        ],
        session_id=SESSION_ID,
    )
    verdict = await judge.evaluate(ctx)
    # Falls back to heuristic — no adapter call, heuristic kind, zero cost.
    assert verdict.judge_kind == "heuristic"
    assert verdict.judge_cost_usd == Decimal("0")
    assert len(adapter.requests) == 0


# ----- HybridJudge -----------------------------------------------------------


async def test_hybrid_skips_llm_when_heuristic_confidence_high() -> None:
    """Clean turn → heuristic confidence ≥ threshold → LLM never called."""
    judge, adapter = _build_llm_judge(
        responses=[_judge_response(score=0.0, confidence=0.0, rationale="never seen")],
    )
    hybrid = HybridJudge(llm_judge=judge, escalation_threshold=0.5)
    verdict = await hybrid.evaluate(_clean_turn_ctx())
    assert verdict.judge_kind == "heuristic"
    assert verdict.judge_cost_usd == Decimal("0")
    assert len(adapter.requests) == 0  # short-circuit


async def test_hybrid_escalates_when_heuristic_confidence_low() -> None:
    """Messy turn → heuristic confidence < threshold → LLM call → hybrid verdict."""
    judge, adapter = _build_llm_judge(
        responses=[_judge_response(score=0.4, confidence=0.85, rationale="bad")]
    )
    # Threshold at 0.9 forces escalation on any heuristic verdict.
    hybrid = HybridJudge(llm_judge=judge, escalation_threshold=0.9)
    ctx = _messy_turn_ctx()
    verdict = await hybrid.evaluate(ctx)
    assert verdict.judge_kind == "hybrid"
    assert verdict.score == 0.4
    assert verdict.confidence == 0.85
    assert verdict.signals.get("escalated") is True
    assert verdict.signals.get("heuristic_score") is not None
    assert verdict.signals.get("heuristic_confidence") is not None
    assert verdict.rubric_id == "turn-hybrid-v1"
    assert len(adapter.requests) == 1


async def test_hybrid_falls_back_to_heuristic_when_budget_exhausted() -> None:
    """Heuristic low-confidence + budget exhausted → heuristic verdict with
    `escalation_skipped='budget_exhausted'`. Spec §7: throttling downgrades
    judge_kind; verdict still lands."""
    budget = BudgetTracker(
        per_session_max_usd=Decimal("0.0001"),
        per_day_max_usd=Decimal("10.0"),
    )
    budget.record(session_id=SESSION_ID, cost_usd=Decimal("0.001"))
    judge, adapter = _build_llm_judge(
        responses=[_judge_response(score=0.4, confidence=0.85)],
        budget=budget,
    )
    hybrid = HybridJudge(llm_judge=judge, escalation_threshold=0.9)
    verdict = await hybrid.evaluate(_messy_turn_ctx())
    assert verdict.judge_kind == "heuristic"
    assert verdict.signals.get("escalation_skipped") == "budget_exhausted"
    assert verdict.signals.get("throttled_reason") == "session_cap"
    assert verdict.signals.get("heuristic_score") is not None
    assert len(adapter.requests) == 0


async def test_hybrid_falls_back_when_llm_call_raises() -> None:
    """LLM parse/call failure → heuristic verdict annotated with escalation_skipped."""
    judge, _ = _build_llm_judge(
        responses=[
            _ScriptedResponse(
                content=[TextBlock(text="not json")],
                stop_reason=StopReason.END_TURN,
            ),
            _ScriptedResponse(
                content=[TextBlock(text="still not json")],
                stop_reason=StopReason.END_TURN,
            ),
        ],
        config=LLMJudgeConfig(max_retries=1),
    )
    hybrid = HybridJudge(llm_judge=judge, escalation_threshold=0.9)
    verdict = await hybrid.evaluate(_messy_turn_ctx())
    assert verdict.judge_kind == "heuristic"
    assert verdict.signals.get("escalation_skipped") == "judge_output_invalid"


async def test_hybrid_default_threshold_matches_spec_default() -> None:
    """v1 default escalation_threshold is 0.7 (evaluator.md §7)."""
    judge, _ = _build_llm_judge(responses=[])
    hybrid = HybridJudge(llm_judge=judge)
    assert hybrid.escalation_threshold == DEFAULT_ESCALATION_THRESHOLD == 0.7


async def test_hybrid_threshold_validation() -> None:
    """Out-of-range escalation_threshold raises ValueError."""
    judge, _ = _build_llm_judge(responses=[])
    with pytest.raises(ValueError):
        HybridJudge(llm_judge=judge, escalation_threshold=1.5)
    with pytest.raises(ValueError):
        HybridJudge(llm_judge=judge, escalation_threshold=-0.1)


async def test_hybrid_passes_through_for_tool_cycle_and_session() -> None:
    """tool_cycle / session subjects → heuristic-only in v1; HybridJudge
    transparently delegates without consulting the threshold."""
    judge, adapter = _build_llm_judge(
        responses=[_judge_response(score=0.0, confidence=0.0)],
    )
    hybrid = HybridJudge(llm_judge=judge, escalation_threshold=0.0)
    turn_id = new_turn_id()
    tool_use_id = new_tool_use_id()
    ctx = SubjectContext(
        subject_kind="tool_cycle",
        subject_id=tool_use_id,
        events=[
            build_tool_called(
                session_id=SESSION_ID,
                turn_id=turn_id,
                tool_use_id=tool_use_id,
                tool_name="read",
            ),
            build_tool_completed(session_id=SESSION_ID, turn_id=turn_id, tool_use_id=tool_use_id),
        ],
        session_id=SESSION_ID,
    )
    verdict = await hybrid.evaluate(ctx)
    assert verdict.judge_kind == "heuristic"
    assert len(adapter.requests) == 0


# ----- §4.5: JudgeRubricProvider integration -------------------------------


_FAKE_RUBRIC_LIBRARY_PROMPTS: dict[tuple[str, str], str] = {
    ("turn", "regex-with-edge-cases"): "PRO_REGEX_RUBRIC_PROMPT",
    ("workload", "regex-with-edge-cases"): "PRO_WORKLOAD_RUBRIC_PROMPT",
}


class _FakeRubricLibrary:
    """Minimal `JudgeRubricProvider` stand-in.

    Mirrors what `metis_pro.judges.rubrics.ProRubricLibrary` will look like:
    a per-(subject_kind, workload_id) lookup table that returns custom
    prompt strings, falling back to `None` for unknown combinations.
    """

    VERSION = "fake-pro-1.0"

    def rubric_for(self, subject_kind: str, workload_id: str | None) -> str | None:
        return _FAKE_RUBRIC_LIBRARY_PROMPTS.get((subject_kind, workload_id))

    def rubric_version(self) -> str:
        return self.VERSION


async def test_llm_judge_falls_back_to_noop_rubric_when_no_provider() -> None:
    """The default LLMJudge uses NoopJudgeRubricProvider → falls back to the
    OSS `_SYSTEM_PROMPT`. The rubric_id / rubric_version on the verdict
    must be the OSS built-in constants (pre-§4.5 behavior preserved).
    """
    judge, adapter = _build_llm_judge(
        responses=[_judge_response(score=0.9, confidence=0.8, rationale="ok")]
    )
    verdict = await judge.evaluate(_clean_turn_ctx())
    assert verdict.rubric_id == "turn-llm-v1"
    assert verdict.rubric_version == "1.0.0"
    # The adapter received the OSS prompt (a non-empty system_prompt that
    # starts with the recognizable opening line).
    assert adapter.requests[-1].system_prompt is not None
    assert "You are an evaluator" in adapter.requests[-1].system_prompt


async def test_llm_judge_consumes_pro_rubric_when_workload_matches() -> None:
    """When the Pro provider returns a custom prompt for the
    (subject_kind, workload_id) pair, LLMJudge uses it AND stamps the
    provider's rubric_version on the verdict.

    `workload_id` is resolved from `SubjectContext.signals_extra` (the
    harness-injected pass-through dict) — `WorkloadRubric` doesn't carry
    a workload_id field in v1.
    """
    judge, adapter = _build_llm_judge(
        responses=[_judge_response(score=0.7, confidence=0.6, rationale="partial")],
        rubric_provider=_FakeRubricLibrary(),
    )
    ctx = SubjectContext(
        subject_kind="turn",
        subject_id=new_turn_id(),
        events=[
            build_llm_completed(session_id=SESSION_ID, turn_id="t1"),
            build_turn_completed(session_id=SESSION_ID, turn_id="t1"),
        ],
        session_id=SESSION_ID,
        signals_extra={"workload_id": "regex-with-edge-cases"},
    )
    verdict = await judge.evaluate(ctx)
    assert verdict.rubric_id == "fake-pro-1.0"
    assert verdict.rubric_version == "fake-pro-1.0"
    assert adapter.requests[-1].system_prompt == "PRO_REGEX_RUBRIC_PROMPT"


async def test_llm_judge_falls_back_when_pro_rubric_unknown_workload() -> None:
    """The Pro provider returns None for unknown (subject_kind,
    workload_id) pairs; LLMJudge falls back to the OSS prompt + built-in
    rubric stamping.
    """
    judge, adapter = _build_llm_judge(
        responses=[_judge_response(score=0.9, confidence=0.8, rationale="ok")],
        rubric_provider=_FakeRubricLibrary(),
    )
    ctx = SubjectContext(
        subject_kind="turn",
        subject_id=new_turn_id(),
        events=[
            build_llm_completed(session_id=SESSION_ID, turn_id="t2"),
            build_turn_completed(session_id=SESSION_ID, turn_id="t2"),
        ],
        session_id=SESSION_ID,
        signals_extra={"workload_id": "not-in-pro-library"},
    )
    verdict = await judge.evaluate(ctx)
    assert verdict.rubric_id == "turn-llm-v1"
    assert verdict.rubric_version == "1.0.0"
    assert "You are an evaluator" in adapter.requests[-1].system_prompt
