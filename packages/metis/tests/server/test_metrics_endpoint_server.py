"""HTTP-level tests for the server's `GET /metrics` endpoint.

End-to-end: hit the route via httpx ASGITransport, parse the body
back through prometheus_client, and assert the bounded metric
families show up. Synthetic events emitted on the bus drive the
counter samples; the polled session-count gauge reads the runtime's
session store.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from metis.core.events.envelope import Actor
from metis.core.events.payloads import (
    EvalCompleted,
    LLMCallCompleted,
    LLMCallFailed,
    PolicyEvaluation,
    RouteDecided,
    ToolCalled,
    ToolCompleted,
    make_event,
)
from metis.server.app import build_app
from prometheus_client.parser import text_string_to_metric_families


@pytest.fixture
async def client(runtime):
    app = build_app(runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def _families(text: str) -> dict[str, list]:
    return {f.name: list(f.samples) for f in text_string_to_metric_families(text)}


async def test_metrics_endpoint_returns_prometheus_text(client) -> None:
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    families = _families(r.text)
    # Bounded family set per observability.md §3 — server flavor.
    assert "metis_llm_calls" in families
    assert "metis_routing_decisions" in families
    assert "metis_session_count" in families


async def test_session_count_reflects_session_store(client, runtime, workspace) -> None:
    families = _families((await client.get("/metrics")).text)
    sample = next(s for s in families["metis_session_count"] if s.name == "metis_session_count")
    assert sample.value == 0.0

    runtime.manager.create_session(workspace_path=str(workspace))
    runtime.manager.create_session(workspace_path=str(workspace))

    families = _families((await client.get("/metrics")).text)
    sample = next(s for s in families["metis_session_count"] if s.name == "metis_session_count")
    assert sample.value == 2.0


async def test_llm_completed_event_propagates_to_metrics(client, runtime) -> None:
    runtime.bus.emit(
        make_event(
            type="llm.call_completed",
            session_id="sess_metrics_server",
            actor=Actor.AGENT,
            timestamp=datetime.now(UTC),
            payload=LLMCallCompleted(
                model="anthropic:claude-sonnet-4-6",
                provider="anthropic",
                input_tokens=300,
                output_tokens=120,
                cached_input_tokens=0,
                cache_creation_input_tokens=0,
                cost_usd=0.10,
                pricing_version="v1",
                latency_ms=750,
                stop_reason="end_turn",
                produced_tool_calls=0,
                produced_thinking_blocks=0,
            ),
        )
    )
    await runtime.bus.drain()

    families = _families((await client.get("/metrics")).text)
    calls = [s for s in families["metis_llm_calls"] if s.name == "metis_llm_calls_total"]
    assert any(s.labels["model"] == "anthropic:claude-sonnet-4-6" for s in calls)
    cost = [s for s in families["metis_llm_cost_usd"] if s.name == "metis_llm_cost_usd_total"]
    assert any(s.value > 0 for s in cost)


async def test_route_decided_drives_routing_decisions_counter(client, runtime) -> None:
    runtime.bus.emit(
        make_event(
            type="route.decided",
            session_id="sess_metrics_route",
            actor=Actor.AGENT,
            timestamp=datetime.now(UTC),
            turn_id="turn_metrics_001",
            payload=RouteDecided(
                chosen_model="anthropic:claude-haiku-4-5",
                winner_index=0,
                elapsed_ms=0.5,
                chain=[
                    PolicyEvaluation(
                        policy="global_default",
                        verdict="chose",
                        reason="default",
                    )
                ],
            ),
        )
    )
    await runtime.bus.drain()

    families = _families((await client.get("/metrics")).text)
    samples = [
        s for s in families["metis_routing_decisions"] if s.name == "metis_routing_decisions_total"
    ]
    matching = [
        s
        for s in samples
        if s.labels.get("winning_slot") == "global_default"
        and s.labels.get("chosen_model") == "anthropic:claude-haiku-4-5"
    ]
    assert len(matching) == 1


async def test_route_decided_drives_routing_latency_histogram(client, runtime) -> None:
    """Wave 14a — `elapsed_ms` projects onto the routing latency histogram."""
    runtime.bus.emit(
        make_event(
            type="route.decided",
            session_id="sess_routing_lat",
            actor=Actor.AGENT,
            timestamp=datetime.now(UTC),
            turn_id="turn_routing_lat",
            payload=RouteDecided(
                chosen_model="anthropic:claude-haiku-4-5",
                winner_index=0,
                elapsed_ms=1.25,
                chain=[
                    PolicyEvaluation(
                        policy="global_default",
                        verdict="chose",
                        reason="default",
                    )
                ],
            ),
        )
    )
    await runtime.bus.drain()

    families = _families((await client.get("/metrics")).text)
    samples = families["metis_routing_decision_latency_seconds"]
    count_sample = next(s for s in samples if s.name.endswith("_count"))
    assert count_sample.value >= 1.0


async def test_tool_lifecycle_drives_tool_latency_histogram(client, runtime) -> None:
    """`tool.called → tool.completed` round-trip drives the tool-latency
    histogram via the in-collector LRU. Tool name flows through the label.
    """
    runtime.bus.emit(
        make_event(
            type="tool.called",
            session_id="sess_tool_lat",
            actor=Actor.AGENT,
            timestamp=datetime.now(UTC),
            turn_id="turn_tool_lat",
            payload=ToolCalled(
                tool_use_id="tu_lat",
                tool_name="read_file",
                input_hash="h",
                input_size_bytes=1,
                side_effects="read",
            ),
        )
    )
    runtime.bus.emit(
        make_event(
            type="tool.completed",
            session_id="sess_tool_lat",
            actor=Actor.AGENT,
            timestamp=datetime.now(UTC),
            turn_id="turn_tool_lat",
            payload=ToolCompleted(
                tool_use_id="tu_lat",
                success=True,
                output_size_bytes=10,
                latency_ms=8,
            ),
        )
    )
    await runtime.bus.drain()

    families = _families((await client.get("/metrics")).text)
    samples = families["metis_tool_call_latency_seconds"]
    matching = [s for s in samples if s.labels.get("tool_name") == "read_file"]
    count_sample = next(s for s in matching if s.name.endswith("_count"))
    assert count_sample.value == 1.0


async def test_llm_failed_increments_dedicated_error_counter(client, runtime) -> None:
    """`metis_llm_call_errors_total{error_class}` is the alert-rule input
    for the LLM error-rate alert. Drives off the same `llm.call_failed`
    event the legacy mixed-status counter already sees.
    """
    runtime.bus.emit(
        make_event(
            type="llm.call_failed",
            session_id="sess_llm_err",
            actor=Actor.AGENT,
            timestamp=datetime.now(UTC),
            payload=LLMCallFailed(
                model="anthropic:claude-haiku-4-5",
                provider="anthropic",
                error_class="rate_limit",
                error_message_redacted="429",
                retry_count=0,
                latency_ms=12,
            ),
        )
    )
    await runtime.bus.drain()

    families = _families((await client.get("/metrics")).text)
    errs = [s for s in families["metis_llm_call_errors"] if s.name == "metis_llm_call_errors_total"]
    matching = [
        s
        for s in errs
        if s.labels.get("error_class") == "rate_limit" and s.labels.get("provider") == "anthropic"
    ]
    assert matching and matching[0].value >= 1.0


async def test_eval_completed_drives_verdict_counter(client, runtime) -> None:
    from decimal import Decimal

    runtime.bus.emit(
        make_event(
            type="eval.completed",
            session_id="sess_metrics_eval",
            actor=Actor.SYSTEM,
            timestamp=datetime.now(UTC),
            payload=EvalCompleted(
                eval_id="ev_server",
                subject_kind="turn",
                subject_id="turn_xyz",
                score=0.6,
                confidence=0.8,
                judge_kind="heuristic",
                judge_cost_usd=Decimal("0.0"),
                judge_latency_ms=2,
                rubric_id="turn-heuristic",
                rubric_version="v1",
                signals={},
            ),
        )
    )
    await runtime.bus.drain()

    families = _families((await client.get("/metrics")).text)
    samples = [s for s in families["metis_eval_verdicts"] if s.name == "metis_eval_verdicts_total"]
    matching = [
        s
        for s in samples
        if s.labels.get("judge_kind") == "heuristic" and s.labels.get("subject_kind") == "turn"
    ]
    assert len(matching) == 1
