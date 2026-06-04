"""Tests for the LLM_ROUTER summary line in the `metis dev` result tag.

The formatter is intentionally quiet when the slot was a no-op (disabled
/ worker re-entry / not pre-computed) and verbose when it actually did
something. See routing-engine.md §5.6.1 history note.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from metis.cli.chat import _format_router_summary
from metis.core.events.payloads import PolicyEvaluation


@dataclass
class _FakeResult:
    """Minimal stand-in for TurnResult — only `route_chain` matters here."""

    route_chain: tuple[PolicyEvaluation, ...]


def _chain(*entries: PolicyEvaluation) -> tuple[PolicyEvaluation, ...]:
    return entries


def _slot(**kwargs) -> PolicyEvaluation:
    return PolicyEvaluation(
        policy=kwargs.pop("policy", "llm_router"),
        verdict=kwargs.pop("verdict"),
        reason=kwargs.pop("reason", ""),
        **kwargs,
    )


def test_returns_none_when_no_chain():
    result = _FakeResult(route_chain=())
    assert _format_router_summary(result) is None


def test_returns_none_when_slot_missing():
    """Chain present but llm_router not in it (older trace shape)."""
    result = _FakeResult(
        route_chain=_chain(
            _slot(policy="per_message_override", verdict="not_applicable"),
        )
    )
    assert _format_router_summary(result) is None


def test_quiet_when_disabled():
    result = _FakeResult(
        route_chain=_chain(_slot(verdict="not_applicable", reason="llm_router disabled"))
    )
    assert _format_router_summary(result) is None


def test_quiet_when_worker_reentry():
    result = _FakeResult(
        route_chain=_chain(_slot(verdict="not_applicable", reason="delegate_request_in_flight"))
    )
    assert _format_router_summary(result) is None


def test_quiet_when_not_precomputed():
    result = _FakeResult(
        route_chain=_chain(_slot(verdict="not_applicable", reason="llm_router not pre-computed"))
    )
    assert _format_router_summary(result) is None


def test_verbose_when_chose():
    result = _FakeResult(
        route_chain=_chain(
            _slot(
                verdict="chose",
                candidate_model="anthropic:claude-sonnet-4-6",
                meta_cost_usd=0.00094,
                meta_tokens_input=1840,
                meta_tokens_output=72,
            )
        )
    )
    line = _format_router_summary(result)
    assert line is not None
    assert "router → anthropic:claude-sonnet-4-6" in line
    assert "meta $0.0009" in line
    assert "1840 in / 72 out" in line


def test_verbose_when_no_tool_call():
    """The motivating failure mode — surfaces verdict + meta-cost so the
    user knows the slot fired and burned tokens."""
    result = _FakeResult(
        route_chain=_chain(
            _slot(
                verdict="not_applicable",
                reason="no_tool_call",
                meta_cost_usd=0.004168,
                meta_tokens_input=16,
                meta_tokens_output=150,
            )
        )
    )
    line = _format_router_summary(result)
    assert line is not None
    assert "router → no_tool_call" in line
    assert "meta $0.0042" in line


def test_verbose_when_rejected():
    result = _FakeResult(
        route_chain=_chain(
            _slot(
                verdict="rejected",
                candidate_model="openai:gpt-text-only",
                validation_failure="no_vision_support",
                meta_cost_usd=0.0008,
                meta_tokens_input=900,
                meta_tokens_output=40,
            )
        )
    )
    line = _format_router_summary(result)
    assert line is not None
    assert "openai:gpt-text-only" in line
    assert "no_vision_support" in line


def test_verbose_when_budget_exhausted_zero_cost():
    """Budget short-circuit: no meta-call happened so no cost, but the slot
    still emitted a documented reason that the user should see."""
    result = _FakeResult(
        route_chain=_chain(_slot(verdict="not_applicable", reason="budget_exhausted"))
    )
    line = _format_router_summary(result)
    assert line is not None
    assert "router → budget_exhausted" in line
    # No meta-cost when the call was skipped.
    assert "meta" not in line


def test_finds_llm_router_among_other_slots():
    """LLM_ROUTER lives at slot 4 in the post-v3.4 chain. Make sure the
    formatter walks past slots 0-3 to find it."""
    result = _FakeResult(
        route_chain=_chain(
            _slot(policy="per_message_override", verdict="not_applicable"),
            _slot(policy="manual_sticky", verdict="not_applicable"),
            _slot(policy="rule", verdict="not_applicable"),
            _slot(policy="pattern", verdict="not_applicable"),
            _slot(
                verdict="chose",
                candidate_model="anthropic:claude-haiku-4-5",
                meta_cost_usd=0.0006,
                meta_tokens_input=1200,
                meta_tokens_output=60,
            ),
            _slot(policy="delegate_request", verdict="not_applicable"),
        )
    )
    line = _format_router_summary(result)
    assert line is not None
    assert "anthropic:claude-haiku-4-5" in line


# Decimal compatibility — TurnResult cost is Decimal but meta_cost_usd on
# the PolicyEvaluation is float; both shapes must format cleanly.
def test_handles_decimal_meta_cost():
    result = _FakeResult(
        route_chain=_chain(
            _slot(
                verdict="chose",
                candidate_model="anthropic:claude-haiku-4-5",
                meta_cost_usd=float(Decimal("0.00125")),
                meta_tokens_input=1000,
                meta_tokens_output=50,
            )
        )
    )
    line = _format_router_summary(result)
    assert line is not None
    assert "meta $0.0012" in line or "meta $0.0013" in line
