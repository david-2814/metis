"""LLM-as-judge and hybrid escalation (evaluator.md §5.2 / §5.3).

The LLM judge prompts a small classifier model (haiku-class by default,
configurable) with a stable rubric prompt and parses the response back
into an `EvalVerdict`. The hybrid judge composes a heuristic floor with
LLM escalation when heuristic confidence drops below a threshold.

Budget discipline: both judges respect the shared `BudgetTracker` caps.
The LLMJudge refuses to make an LLM call when over budget and returns a
low-confidence verdict marked `signals.budget_exhausted=True`; HybridJudge
then falls back to the heuristic verdict it already computed.

Why a small model for the judge: it's a classification task, not synthesis,
so spending opus to grade haiku inverts the cost story (evaluator.md §5.2).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import msgspec

from metis_core.adapters.protocol import (
    CanonicalRequest,
    CanonicalResponse,
    ProviderAdapter,
    StopReason,
)
from metis_core.canonical.content import TextBlock
from metis_core.canonical.ids import next_monotonic_ulid
from metis_core.eval.budget import BudgetTracker
from metis_core.eval.judge import HeuristicJudge, Judge, SubjectContext
from metis_core.eval.verdict import EvalVerdict, clamp_unit
from metis_core.pricing.table import PriceTable

logger = logging.getLogger(__name__)


# Bumped when the rubric prompt changes — produces a new score series on the
# dashboard (evaluator.md §12 invariant 7). Heuristic+LLM hybrid landed at
# v1.0.0.
TURN_LLM_RUBRIC_ID = "turn-llm-v1"
TURN_LLM_RUBRIC_VERSION = "1.0.0"

TURN_HYBRID_RUBRIC_ID = "turn-hybrid-v1"
TURN_HYBRID_RUBRIC_VERSION = "1.0.0"

WORKLOAD_LLM_RUBRIC_ID = "workload-llm-v1"
WORKLOAD_LLM_RUBRIC_VERSION = "1.0.0"

WORKLOAD_HYBRID_RUBRIC_ID = "workload-hybrid-v1"
WORKLOAD_HYBRID_RUBRIC_VERSION = "1.0.0"

# Default escalation threshold (evaluator.md §5.3 / §7). Heuristic confidence
# at or above this skips the LLM call. Tunable per workspace; the value here
# is the v1 default — the right number for a given workspace falls out of the
# agreement-rate view once data accumulates.
DEFAULT_ESCALATION_THRESHOLD = 0.7

# Token caps for the judge call. The judge returns a tiny JSON object; we
# don't need much output, and a small input estimate is enough since the
# rubric prompt + per-turn evidence is short.
DEFAULT_JUDGE_MAX_OUTPUT_TOKENS = 256
_DEFAULT_JUDGE_PROJECTED_INPUT_TOKENS = 1500
_DEFAULT_JUDGE_PROJECTED_OUTPUT_TOKENS = 200


_SYSTEM_PROMPT = """\
You are an evaluator scoring whether an AI assistant's turn succeeded.

Given a user prompt, the assistant's final response, and a summary of tool
activity, judge whether the turn met the user's apparent intent. Reply with a
single JSON object — no prose before or after — with exactly three keys:

  - "score": float in [0.0, 1.0], one decimal place. 1.0 = clearly successful,
    0.0 = clearly failed. Use intermediate values for partial success.
  - "confidence": float in [0.0, 1.0], one decimal place. Your own certainty
    in the score. Low confidence is honest when evidence conflicts.
  - "rationale": one short sentence (<= 200 chars) explaining the score.

Scoring guide:
  - A refusal, empty response, or hard error is a failure (score near 0.0).
  - A tool failure that's recovered from is partial (score around 0.5).
  - A clean response that directly addresses the user's request is success
    (score near 1.0).
  - When the user asked for a specific deliverable, check that it was produced.

Be terse. Return only the JSON object.
"""


class _JudgeResponse(msgspec.Struct):
    """Schema the LLM judge is asked to produce.

    Parsed from the assistant's text block; parse failures emit
    `eval.failed.failure_mode='judge_output_invalid'` after the bounded
    retry per evaluator.md §5.2.
    """

    score: float
    confidence: float
    rationale: str


class LLMJudgeError(Exception):
    """Raised when the LLM judge can't produce a parseable verdict.

    The subscriber catches this and emits `eval.failed`. The hybrid judge
    falls back to its heuristic verdict on this exception.
    """

    def __init__(self, message: str, *, failure_mode: str = "judge_output_invalid") -> None:
        super().__init__(message)
        self.failure_mode = failure_mode


@dataclass(frozen=True)
class LLMJudgeConfig:
    """Tunables for the LLM judge.

    `projected_input_tokens` / `projected_output_tokens` are the up-front
    estimate used for budget gating before the call — actual cost is
    re-computed from the real usage after the response. Conservative
    estimates here mean we may refuse to call when we'd have fit within
    budget; under-estimates mean we may slip over the cap by one call.
    """

    judge_model: str = "anthropic:claude-haiku-4-5"
    max_output_tokens: int = DEFAULT_JUDGE_MAX_OUTPUT_TOKENS
    max_retries: int = 1
    projected_input_tokens: int = _DEFAULT_JUDGE_PROJECTED_INPUT_TOKENS
    projected_output_tokens: int = _DEFAULT_JUDGE_PROJECTED_OUTPUT_TOKENS


class LLMJudge:
    """Calls an LLM with the rubric prompt to score a subject.

    The judge is provider-agnostic: it takes a ProviderAdapter and the
    canonical model id. The adapter handles wire translation; the judge
    just builds a CanonicalRequest, parses the JSON reply, and computes
    cost via the supplied PriceTable.

    Subjects supported in v1: `turn` and `workload`. Other subject_kinds
    fall back to the (passed-through) HeuristicJudge to preserve the
    spec's heuristic-only commitment for tool_cycle / session.
    """

    judge_kind = "llm"

    def __init__(
        self,
        *,
        adapter: ProviderAdapter,
        pricing: PriceTable,
        config: LLMJudgeConfig | None = None,
        budget: BudgetTracker | None = None,
        heuristic_for_unsupported: Judge | None = None,
    ) -> None:
        self._adapter = adapter
        self._pricing = pricing
        self._config = config or LLMJudgeConfig()
        self._budget = budget or BudgetTracker()
        # tool_cycle / session stay heuristic-only in v1; the LLM judge
        # delegates to this for those subject kinds rather than re-imple-
        # menting the heuristic.
        self._fallback = heuristic_for_unsupported or HeuristicJudge()

    @property
    def budget(self) -> BudgetTracker:
        return self._budget

    @property
    def judge_model(self) -> str:
        return self._config.judge_model

    async def evaluate(self, ctx: SubjectContext) -> EvalVerdict:
        # Only turn and workload are LLM-eligible in v1 (evaluator.md §5.4,
        # §5.5, §5.6). Everything else delegates to heuristic — keeps the
        # contract intact without duplicating heuristic logic here.
        if ctx.subject_kind not in ("turn", "workload"):
            return await self._fallback.evaluate(ctx)

        started_ns = time.perf_counter_ns()
        rubric_id, rubric_version = _llm_rubric_for(ctx.subject_kind)

        # Budget pre-check. The estimate is conservative; the call is only
        # made when projected spend fits both per-session and per-day caps.
        projected_cost = self._projected_cost()
        throttle = self._budget.throttle_reason(
            session_id=ctx.session_id or "(unknown)",
            projected_cost_usd=projected_cost,
        )
        if throttle is not None:
            return _budget_exhausted_verdict(
                ctx=ctx,
                judge_model=self._config.judge_model,
                rubric_id=rubric_id,
                rubric_version=rubric_version,
                started_ns=started_ns,
                throttle_reason=throttle,
            )

        # Build prompt → call adapter → parse. The bounded retry handles
        # one parse failure; beyond that the judge raises and the
        # subscriber emits eval.failed.
        last_error: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                response = await self._call_adapter(ctx)
                parsed = _parse_response(response)
                cost = self._compute_cost(response)
                # NB: don't `self._budget.record(...)` here. The subscriber
                # (`Evaluator._run_judge`) is the single recorder of judge
                # spend against the budget — calling here would double-
                # charge per-session and per-day caps. Standalone callers
                # (tests, direct re-evaluation) record explicitly.
                latency_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
                rationale = parsed.rationale[:200]
                signals: dict[str, Any] = {
                    "rationale_hash": _sha256_hex(rationale),
                    "rationale_preview": rationale,
                    "attempts": attempt + 1,
                }
                return EvalVerdict(
                    eval_id=str(next_monotonic_ulid()),
                    subject_kind=ctx.subject_kind,
                    subject_id=ctx.subject_id,
                    score=clamp_unit(float(parsed.score)),
                    confidence=clamp_unit(float(parsed.confidence)),
                    judge_kind="llm",
                    judge_model=self._config.judge_model,
                    judge_cost_usd=cost,
                    judge_pricing_version=self._pricing.version,
                    judge_latency_ms=int(latency_ms),
                    rubric_id=rubric_id,
                    rubric_version=rubric_version,
                    signals=signals,
                    parent_eval_id=ctx.parent_eval_id,
                    created_at=datetime.now(UTC).isoformat(),
                )
            except LLMJudgeError as exc:
                last_error = exc
                logger.info(
                    "LLM judge parse retry %d/%d: %s",
                    attempt + 1,
                    self._config.max_retries,
                    exc,
                )
                continue
        assert last_error is not None
        raise last_error

    # ---- internals ----------------------------------------------------

    async def _call_adapter(self, ctx: SubjectContext) -> CanonicalResponse:
        user_text = _build_user_message(ctx)
        # Reuse the canonical user-message shape rather than depending on a
        # provider-specific shape — every adapter accepts this.
        from metis_core.canonical.messages import Message, Role

        request = CanonicalRequest(
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
            model=self._config.judge_model,
            max_output_tokens=self._config.max_output_tokens,
            temperature=0.0,
        )
        try:
            return await self._adapter.complete(request)
        except Exception as exc:  # provider errors surface as judge_call_failed
            raise LLMJudgeError(
                f"adapter call failed: {type(exc).__name__}: {exc}",
                failure_mode="judge_call_failed",
            ) from exc

    def _projected_cost(self) -> Decimal:
        """Up-front cost estimate for budget gating.

        Uses the configured per-call token estimates and the price table's
        per-model rate. Conservative by design — better to refuse a call
        than to overshoot the daily cap.
        """
        from metis_core.adapters.protocol import TokenUsage

        usage = TokenUsage(
            input_tokens=self._config.projected_input_tokens,
            output_tokens=self._config.projected_output_tokens,
        )
        try:
            return self._pricing.compute_cost(self._config.judge_model, usage)
        except Exception:
            # If the judge model isn't priced, fail closed: don't call it.
            # The subscriber will see the budget_exhausted verdict and the
            # operator will notice the bad config.
            return Decimal("9999")

    def _compute_cost(self, response: CanonicalResponse) -> Decimal:
        try:
            return self._pricing.compute_cost(response.model, response.usage)
        except Exception:
            # Stamped cost is the source of truth on the bus event; failing
            # the cost computation here would surface as a verdict with
            # judge_cost_usd=0 even though we did call the model. We log
            # and fall back to 0 to keep the verdict landing — the actual
            # spend lives on the llm.call_completed event if the adapter
            # wrote one. In v1 the LLM judge calls the adapter directly,
            # bypassing the SessionManager, so there's no llm.call_completed
            # — the verdict's judge_cost_usd is the only record.
            logger.warning(
                "LLM judge cost compute failed for model=%s; recording 0",
                response.model,
            )
            return Decimal("0")


class HybridJudge:
    """Heuristic first; escalate to LLM only when confidence is low.

    Algorithm (evaluator.md §5.3):
      1. Run the heuristic judge.
      2. If `heuristic_confidence >= escalation_threshold`, emit the
         heuristic verdict and stop.
      3. Otherwise, run the LLM judge. If it returns a budget-exhausted
         verdict, keep the heuristic verdict (annotated with
         `signals.escalation_skipped='budget_exhausted'`); otherwise emit
         a verdict with `judge_kind='hybrid'`, the LLM's score+confidence,
         and the heuristic's score+confidence embedded in signals.

    For tool_cycle / session subjects, the hybrid judge delegates to the
    heuristic (spec §5.5 / §5.6: heuristic-only at those levels in v1).
    """

    judge_kind = "hybrid"

    def __init__(
        self,
        *,
        llm_judge: LLMJudge,
        heuristic: HeuristicJudge | None = None,
        escalation_threshold: float = DEFAULT_ESCALATION_THRESHOLD,
    ) -> None:
        if not 0.0 <= escalation_threshold <= 1.0:
            raise ValueError(f"escalation_threshold must be in [0, 1]; got {escalation_threshold}")
        self._llm = llm_judge
        self._heuristic = heuristic or HeuristicJudge()
        self._escalation_threshold = escalation_threshold

    @property
    def escalation_threshold(self) -> float:
        return self._escalation_threshold

    @property
    def budget(self) -> BudgetTracker:
        return self._llm.budget

    async def evaluate(self, ctx: SubjectContext) -> EvalVerdict:
        heuristic_verdict = await self._heuristic.evaluate(ctx)

        # Spec §5.5 / §5.6: tool_cycle and session subjects are heuristic-
        # only in v1. The hybrid wrapper is transparent for those.
        if ctx.subject_kind not in ("turn", "workload"):
            return heuristic_verdict

        if heuristic_verdict.confidence >= self._escalation_threshold:
            return heuristic_verdict

        try:
            llm_verdict = await self._llm.evaluate(ctx)
        except LLMJudgeError as exc:
            # LLM failed — fall back to heuristic with a note in signals.
            signals = dict(heuristic_verdict.signals)
            signals["escalation_skipped"] = exc.failure_mode
            signals["heuristic_score"] = heuristic_verdict.score
            signals["heuristic_confidence"] = heuristic_verdict.confidence
            return _replace_signals(heuristic_verdict, signals)

        if llm_verdict.signals.get("budget_exhausted"):
            # Budget cap fired — return heuristic verdict, but record that
            # the LLM was wanted (operator can see this in the audit trail).
            signals = dict(heuristic_verdict.signals)
            signals["escalation_skipped"] = "budget_exhausted"
            signals["heuristic_score"] = heuristic_verdict.score
            signals["heuristic_confidence"] = heuristic_verdict.confidence
            signals["throttled_reason"] = llm_verdict.signals.get("throttled_reason")
            return _replace_signals(heuristic_verdict, signals)

        # Successful escalation: hybrid verdict carries the LLM's score
        # and confidence with both judges' evidence in signals.
        signals = dict(llm_verdict.signals)
        signals["heuristic_score"] = heuristic_verdict.score
        signals["heuristic_confidence"] = heuristic_verdict.confidence
        signals["heuristic_flags"] = heuristic_verdict.signals.get("flags", [])
        signals["heuristic_flags_negative"] = heuristic_verdict.signals.get("flags_negative", [])
        signals["escalated"] = True
        rubric_id, rubric_version = _hybrid_rubric_for(ctx.subject_kind)
        return EvalVerdict(
            eval_id=llm_verdict.eval_id,
            subject_kind=llm_verdict.subject_kind,
            subject_id=llm_verdict.subject_id,
            score=llm_verdict.score,
            confidence=llm_verdict.confidence,
            judge_kind="hybrid",
            judge_model=llm_verdict.judge_model,
            judge_cost_usd=llm_verdict.judge_cost_usd,
            judge_pricing_version=llm_verdict.judge_pricing_version,
            judge_latency_ms=llm_verdict.judge_latency_ms,
            rubric_id=rubric_id,
            rubric_version=rubric_version,
            signals=signals,
            parent_eval_id=llm_verdict.parent_eval_id,
            created_at=llm_verdict.created_at,
        )


# ---- helpers ----------------------------------------------------------


def _llm_rubric_for(subject_kind: str) -> tuple[str, str]:
    if subject_kind == "workload":
        return WORKLOAD_LLM_RUBRIC_ID, WORKLOAD_LLM_RUBRIC_VERSION
    return TURN_LLM_RUBRIC_ID, TURN_LLM_RUBRIC_VERSION


def _hybrid_rubric_for(subject_kind: str) -> tuple[str, str]:
    if subject_kind == "workload":
        return WORKLOAD_HYBRID_RUBRIC_ID, WORKLOAD_HYBRID_RUBRIC_VERSION
    return TURN_HYBRID_RUBRIC_ID, TURN_HYBRID_RUBRIC_VERSION


def _budget_exhausted_verdict(
    *,
    ctx: SubjectContext,
    judge_model: str,
    rubric_id: str,
    rubric_version: str,
    started_ns: int,
    throttle_reason: str,
) -> EvalVerdict:
    """Build a confidence=0 verdict marked budget_exhausted.

    This is the LLM judge's "I refuse to call the model" verdict. The
    hybrid judge inspects this and falls back to its heuristic verdict
    instead of using this directly; analytics readers can spot these via
    `signals.budget_exhausted=True`.
    """
    latency_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
    return EvalVerdict(
        eval_id=str(next_monotonic_ulid()),
        subject_kind=ctx.subject_kind,
        subject_id=ctx.subject_id,
        score=0.5,  # neutral — no signal
        confidence=0.0,
        judge_kind="llm",
        judge_model=judge_model,
        judge_cost_usd=Decimal("0"),
        judge_pricing_version=None,
        judge_latency_ms=int(latency_ms),
        rubric_id=rubric_id,
        rubric_version=rubric_version,
        signals={
            "budget_exhausted": True,
            "throttled_reason": throttle_reason,
        },
        parent_eval_id=ctx.parent_eval_id,
        created_at=datetime.now(UTC).isoformat(),
    )


def _replace_signals(verdict: EvalVerdict, signals: dict) -> EvalVerdict:
    """Return a copy of `verdict` with `signals` replaced."""
    return EvalVerdict(
        eval_id=verdict.eval_id,
        subject_kind=verdict.subject_kind,
        subject_id=verdict.subject_id,
        score=verdict.score,
        confidence=verdict.confidence,
        judge_kind=verdict.judge_kind,
        judge_model=verdict.judge_model,
        judge_cost_usd=verdict.judge_cost_usd,
        judge_pricing_version=verdict.judge_pricing_version,
        judge_latency_ms=verdict.judge_latency_ms,
        rubric_id=verdict.rubric_id,
        rubric_version=verdict.rubric_version,
        signals=signals,
        parent_eval_id=verdict.parent_eval_id,
        created_at=verdict.created_at,
    )


def _build_user_message(ctx: SubjectContext) -> str:
    """Compose the rubric-input text the judge LLM reads.

    The user message bundles: subject metadata, user prompt, assistant
    response, tool activity summary, chosen model. The assistant /user
    text is pulled from `signals_extra` when the caller plumbed it
    (subscriber for online path; benchmark harness for workload path);
    when absent, we degrade gracefully to an event-only summary.

    For workload subjects with grounding rubric inputs configured, the
    expected and forbidden grounding tokens are also surfaced so the LLM
    can judge paraphrased grounding (citing a real symbol differently)
    that the heuristic substring match would miss.
    """
    extra = ctx.signals_extra or {}
    user_text = str(extra.get("user_prompt_text") or "").strip()
    asst_text = str(extra.get("assistant_response_text") or "").strip()
    chosen_model = extra.get("chosen_model")

    lines: list[str] = [
        f"Subject kind: {ctx.subject_kind}",
        f"Subject id: {ctx.subject_id}",
    ]
    if chosen_model:
        lines.append(f"Model under evaluation: {chosen_model}")

    # User prompt — truncated so a runaway prompt doesn't blow the judge's
    # context. The judge needs intent, not every token.
    lines.append("---")
    lines.append("USER PROMPT:")
    lines.append(_truncate(user_text or "(not available)", limit=2000))
    lines.append("---")
    lines.append("ASSISTANT FINAL RESPONSE:")
    lines.append(_truncate(asst_text or "(not available)", limit=2000))

    # Tool activity summary from events (when present).
    tool_summary = _tool_activity_summary(ctx)
    if tool_summary:
        lines.append("---")
        lines.append("TOOL ACTIVITY:")
        lines.append(tool_summary)

    # Lifecycle signals: stop_reason, failures.
    lifecycle = _lifecycle_summary(ctx)
    if lifecycle:
        lines.append("---")
        lines.append("TURN LIFECYCLE:")
        lines.append(lifecycle)

    # Grounding hints (workload only, when the rubric configured them). The
    # heuristic matched substrings literally; the LLM tier can recognize
    # paraphrased grounding (a real symbol named correctly without the
    # exact substring) and fabricated symbols (plausible-but-wrong names).
    grounding_hint = _grounding_hint(ctx)
    if grounding_hint:
        lines.append("---")
        lines.append("GROUNDING HINTS (workload-rubric inputs):")
        lines.append(grounding_hint)

    lines.append("---")
    lines.append('Respond with a JSON object: {"score": ..., "confidence": ..., "rationale": ...}')
    return "\n".join(lines)


def _grounding_hint(ctx: SubjectContext) -> str:
    """Render the workload rubric's grounding-token lists for the judge.

    Returns "" when the subject isn't a workload or no grounding lists were
    configured. The output is short (one line per list) so it doesn't blow
    the judge's input estimate.
    """
    if ctx.subject_kind != "workload" or ctx.workload_rubric is None:
        return ""
    rubric = ctx.workload_rubric
    expected = list(rubric.grounding_tokens)
    forbidden = list(rubric.forbidden_grounding)
    if not expected and not forbidden:
        return ""
    parts: list[str] = []
    if expected:
        parts.append(
            "  expected grounding (real symbols the response should cite, "
            "verbatim or paraphrased): " + ", ".join(expected)
        )
    if forbidden:
        parts.append(
            "  forbidden grounding (plausible-but-fabricated names; presence "
            "is evidence of hallucination): " + ", ".join(forbidden)
        )
    return "\n".join(parts)


def _truncate(text: str, *, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated, {len(text) - limit} chars omitted]"


def _tool_activity_summary(ctx: SubjectContext) -> str:
    parts: list[str] = []
    for e in ctx.events:
        if e.type == "tool.called":
            name = e.payload.get("tool_name") or "?"
            parts.append(f"  called {name} (input_hash={e.payload.get('input_hash')})")
        elif e.type == "tool.completed":
            name = e.payload.get("tool_name") or ""
            success = e.payload.get("success")
            parts.append(f"  completed {name} success={success}")
        elif e.type == "tool.failed":
            name = e.payload.get("tool_name") or ""
            error_class = e.payload.get("error_class") or "?"
            parts.append(f"  failed {name} error_class={error_class}")
    return "\n".join(parts) if parts else ""


def _lifecycle_summary(ctx: SubjectContext) -> str:
    parts: list[str] = []
    for e in ctx.events:
        if e.type == "turn.completed":
            parts.append(f"  stop_reason={e.payload.get('stop_reason')}")
            parts.append(f"  tool_call_count={e.payload.get('tool_call_count')}")
        elif e.type == "llm.call_failed":
            parts.append(f"  llm.call_failed error_class={e.payload.get('error_class')}")
    return "\n".join(parts) if parts else ""


def _parse_response(response: CanonicalResponse) -> _JudgeResponse:
    """Parse the assistant's text-only response into the judge schema.

    Tolerates leading prose by extracting the first {...} JSON object; the
    spec asks the model for pure JSON but defensive parsing keeps the
    judge robust to the small models' occasional preamble.
    """
    if response.stop_reason not in (StopReason.END_TURN, StopReason.STOP_SEQUENCE):
        raise LLMJudgeError(
            f"judge call ended with unexpected stop_reason={response.stop_reason.value!r}",
        )
    text_parts = [b.text for b in response.content if isinstance(b, TextBlock)]
    text = "".join(text_parts).strip()
    if not text:
        raise LLMJudgeError("judge returned no text content")
    json_payload = _extract_first_json_object(text)
    if json_payload is None:
        raise LLMJudgeError(f"could not locate JSON object in judge output: {text[:200]!r}")
    try:
        parsed = msgspec.json.decode(json_payload, type=_JudgeResponse)
    except msgspec.ValidationError as exc:
        raise LLMJudgeError(f"judge output failed schema validation: {exc}") from exc
    except msgspec.DecodeError as exc:
        raise LLMJudgeError(f"judge output is not valid JSON: {exc}") from exc
    if not (0.0 <= parsed.score <= 1.0):
        raise LLMJudgeError(f"judge score out of [0, 1]: {parsed.score}")
    if not (0.0 <= parsed.confidence <= 1.0):
        raise LLMJudgeError(f"judge confidence out of [0, 1]: {parsed.confidence}")
    return parsed


def _extract_first_json_object(text: str) -> bytes | None:
    """Find the first balanced {...} JSON object in `text`.

    Small models sometimes wrap JSON in markdown fences or add a sentence
    of preamble. We scan for the first `{`, walk to its matching `}`
    respecting strings, and hand that slice to msgspec. Returns None when
    no balanced object exists.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1].encode("utf-8")
    return None


def _sha256_hex(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_ESCALATION_THRESHOLD",
    "DEFAULT_JUDGE_MAX_OUTPUT_TOKENS",
    "TURN_HYBRID_RUBRIC_ID",
    "TURN_HYBRID_RUBRIC_VERSION",
    "TURN_LLM_RUBRIC_ID",
    "TURN_LLM_RUBRIC_VERSION",
    "WORKLOAD_HYBRID_RUBRIC_ID",
    "WORKLOAD_HYBRID_RUBRIC_VERSION",
    "WORKLOAD_LLM_RUBRIC_ID",
    "WORKLOAD_LLM_RUBRIC_VERSION",
    "HybridJudge",
    "LLMJudge",
    "LLMJudgeConfig",
    "LLMJudgeError",
]
