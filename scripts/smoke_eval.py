"""Smoke test for the LLM judge tier (evaluator.md §5.2 / §5.3).

Calls a real Anthropic haiku model with a fabricated turn transcript and
asserts the judge parses a verdict. Budget: ~$0.001 per run (haiku is
cheap). Set `ANTHROPIC_API_KEY` to run.

Usage:
    uv run python scripts/smoke_eval.py
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from pathlib import Path

from metis.core.adapters.anthropic import AnthropicAdapter
from metis.core.eval.judge import HeuristicJudge, SubjectContext
from metis.core.eval.llm_judge import (
    HybridJudge,
    LLMJudge,
    LLMJudgeConfig,
)
from metis.core.pricing import DEFAULT_PRICE_TABLE

JUDGE_MODEL = "anthropic:claude-haiku-4-5"
SESSION_ID = "sess_smoke_eval"

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _ctx_for(*, user_prompt: str, assistant_response: str) -> SubjectContext:
    """Build a turn-subject context. No events — the judge falls back to the
    text the caller supplies via `signals_extra`."""
    return SubjectContext(
        subject_kind="turn",
        subject_id="smoke_turn_id",
        events=[],
        session_id=SESSION_ID,
        signals_extra={
            "user_prompt_text": user_prompt,
            "assistant_response_text": assistant_response,
            "chosen_model": "anthropic:claude-haiku-4-5",
        },
    )


async def _smoke_llm_clean(adapter: AnthropicAdapter) -> Decimal:
    """A clean response should score high."""
    judge = LLMJudge(
        adapter=adapter,
        pricing=DEFAULT_PRICE_TABLE,
        config=LLMJudgeConfig(judge_model=JUDGE_MODEL),
    )
    ctx = _ctx_for(
        user_prompt="What is 2+2?",
        assistant_response="2 + 2 = 4.",
    )
    verdict = await judge.evaluate(ctx)
    assert verdict.judge_kind == "llm", verdict.judge_kind
    print(
        f"  clean: score={verdict.score} confidence={verdict.confidence} "
        f"cost=${verdict.judge_cost_usd}"
    )
    print(f"    rationale={verdict.signals.get('rationale_preview')!r}")
    assert 0.0 <= verdict.score <= 1.0
    assert 0.0 <= verdict.confidence <= 1.0
    # The judge should agree this is a clean turn — score >= 0.5.
    assert verdict.score >= 0.5, f"expected clean answer to score >= 0.5; got {verdict.score}"
    return verdict.judge_cost_usd


async def _smoke_llm_refusal(adapter: AnthropicAdapter) -> Decimal:
    """A refusal should score low."""
    judge = LLMJudge(
        adapter=adapter,
        pricing=DEFAULT_PRICE_TABLE,
        config=LLMJudgeConfig(judge_model=JUDGE_MODEL),
    )
    ctx = _ctx_for(
        user_prompt="What is 2+2?",
        assistant_response="I cannot help with that request.",
    )
    verdict = await judge.evaluate(ctx)
    print(
        f"  refusal: score={verdict.score} confidence={verdict.confidence} "
        f"cost=${verdict.judge_cost_usd}"
    )
    print(f"    rationale={verdict.signals.get('rationale_preview')!r}")
    assert verdict.score < 0.5, f"expected refusal to score < 0.5; got {verdict.score}"
    return verdict.judge_cost_usd


async def _smoke_hybrid_short_circuit(adapter: AnthropicAdapter) -> Decimal:
    """Heuristic should fire high-confidence here, so the LLM is never called.
    The verdict.judge_kind should be 'heuristic' with judge_cost_usd == 0.
    """
    llm = LLMJudge(
        adapter=adapter,
        pricing=DEFAULT_PRICE_TABLE,
        config=LLMJudgeConfig(judge_model=JUDGE_MODEL),
    )
    hybrid = HybridJudge(llm_judge=llm, heuristic=HeuristicJudge(), escalation_threshold=0.5)
    # A workload subject WITHOUT per-turn scores or assertions has low
    # heuristic confidence (0.4). But we need a turn — build a clean-
    # enough heuristic turn first.
    ctx = _ctx_for(
        user_prompt="What is 2+2?",
        assistant_response="2 + 2 = 4.",
    )
    verdict = await hybrid.evaluate(ctx)
    print(
        f"  hybrid clean: judge_kind={verdict.judge_kind} score={verdict.score} "
        f"confidence={verdict.confidence} cost=${verdict.judge_cost_usd}"
    )
    return verdict.judge_cost_usd


async def main() -> int:
    _load_dotenv(REPO_ROOT / ".env")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY missing — set it in .env or env to run this smoke")
        return 1
    adapter = AnthropicAdapter(api_key=api_key)
    total_cost = Decimal("0")
    try:
        print("LLMJudge (clean turn):")
        total_cost += await _smoke_llm_clean(adapter)
        print("LLMJudge (refusal):")
        total_cost += await _smoke_llm_refusal(adapter)
        print("HybridJudge (heuristic short-circuit):")
        total_cost += await _smoke_hybrid_short_circuit(adapter)
    finally:
        await adapter.close()
    print(f"\nTotal judge spend: ${total_cost}")
    # Budget guard — refuse to print "success" if a regression somehow
    # blew through the cap (a haiku judge call shouldn't exceed $0.02).
    assert total_cost < Decimal("0.05"), f"smoke spent > $0.05 (got ${total_cost}) — investigate"
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
