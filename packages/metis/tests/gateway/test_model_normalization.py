"""Tests for inbound-model normalization (gateway.md §4.8).

The GA-readiness audit (§2.4) surfaced that SDK clients commonly send bare
model names (Anthropic SDK: `claude-3-5-haiku-20241022`; OpenAI SDK:
`gpt-4o-mini`) because the upstream APIs reject the `anthropic:` / `openai:`
prefix Metis uses internally. Without normalization the routing chain's
`per_message_override` slot can't resolve the bare name, falls through to
`global_default`, and bills under whatever model that points at — for haiku
requests, that's a ~6x cost over-report.

These tests pin the normalization rule:

- Bare provider name (no `:` and no `metis://`) → prepend the inbound shape's
  prefix.
- Metis aliases (`haiku`, `metis://auto`) survive unchanged.
- Already-canonical `provider:name` strings survive unchanged.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import httpx
import pytest
from metis.core.pricing import ModelPricing
from metis.core.routing.profiles import standard_profile_for
from metis.gateway.app import build_app
from metis.gateway.harness import _normalize_inbound_model

# ---------------------------------------------------------------------------
# Unit tests on the normalizer itself
# ---------------------------------------------------------------------------


def test_normalizer_passes_through_known_alias(runtime) -> None:
    # `haiku` is a registered alias in the test runtime — no rewrite.
    out = _normalize_inbound_model("haiku", inbound_shape="anthropic", registry=runtime.registry)
    assert out == "haiku"


def test_normalizer_passes_through_canonical_id(runtime) -> None:
    out = _normalize_inbound_model(
        "anthropic:claude-haiku-4-5",
        inbound_shape="anthropic",
        registry=runtime.registry,
    )
    assert out == "anthropic:claude-haiku-4-5"


def test_normalizer_passes_through_metis_alias(runtime) -> None:
    # `metis://auto` is the documented opt-out for routing-decides-everything.
    # Normalization MUST NOT prepend a provider prefix.
    out = _normalize_inbound_model(
        "metis://auto", inbound_shape="openai", registry=runtime.registry
    )
    assert out == "metis://auto"


def test_normalizer_prepends_anthropic_prefix_for_anthropic_shape(runtime) -> None:
    out = _normalize_inbound_model(
        "claude-3-5-haiku-20241022",
        inbound_shape="anthropic",
        registry=runtime.registry,
    )
    assert out == "anthropic:claude-3-5-haiku-20241022"


def test_normalizer_prepends_openai_prefix_for_openai_shape(runtime) -> None:
    out = _normalize_inbound_model("gpt-4o-mini", inbound_shape="openai", registry=runtime.registry)
    assert out == "openai:gpt-4o-mini"


def test_normalizer_pass_through_for_unknown_shape(runtime) -> None:
    # Defensive: only `openai` and `anthropic` are wired today. An unknown
    # shape returns the bare name rather than guessing — the routing chain
    # falls through to global_default as if no override was sent.
    out = _normalize_inbound_model(
        "gpt-4o-mini", inbound_shape="future-shape", registry=runtime.registry
    )
    assert out == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# End-to-end: bare name in inbound body → canonical model on the trace +
# correct per-token cost
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(runtime):
    app = build_app(runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def _register_haiku_3_5(runtime, scripted_adapter) -> None:
    """Wire a registry entry + pricing for `anthropic:claude-3-5-haiku-20241022`
    so the normalization smoke covers a model the buyer might actually pass."""
    model_id = "anthropic:claude-3-5-haiku-20241022"
    runtime.registry.register(
        model_id=model_id,
        adapter=scripted_adapter,
        aliases=[],
        task_profile=standard_profile_for(model_id),
    )
    runtime.pricing = runtime.pricing.with_overlay(
        overlay_version="test-3-5-haiku",
        overlay_models={
            model_id: ModelPricing(
                input_per_mtok=Decimal("0.80"),
                output_per_mtok=Decimal("4.00"),
                cached_read_per_mtok=Decimal("0.08"),
                cache_creation_per_mtok=Decimal("1.00"),
            )
        },
    )


def _register_gpt_4o_mini(runtime, scripted_adapter) -> None:
    model_id = "openai:gpt-4o-mini"
    runtime.registry.register(
        model_id=model_id,
        adapter=scripted_adapter,
        aliases=[],
        task_profile=standard_profile_for(model_id),
    )
    runtime.pricing = runtime.pricing.with_overlay(
        overlay_version="test-gpt-4o-mini",
        overlay_models={
            model_id: ModelPricing(
                input_per_mtok=Decimal("0.15"),
                output_per_mtok=Decimal("0.60"),
                cached_read_per_mtok=Decimal("0.075"),
            )
        },
    )


async def test_anthropic_shape_bare_name_normalizes_to_canonical_id(
    client, bearer_token, scripted_adapter, runtime
) -> None:
    """Anthropic SDK clients send `claude-3-5-haiku-20241022` (no prefix).
    The gateway must route to the canonical id, not fall through to the
    sonnet global_default."""
    _register_haiku_3_5(runtime, scripted_adapter)
    scripted_adapter.push_response(text="ok", input_tokens=1000, output_tokens=200)
    r = await client.post(
        "/v1/messages",
        headers={"x-api-key": bearer_token, "anthropic-version": "2023-06-01"},
        json={
            "model": "claude-3-5-haiku-20241022",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200, r.text
    # Buyer echoes back the raw client string (their SDK doesn't speak
    # canonical ids on its outbound parse).
    assert r.json()["model"] == "claude-3-5-haiku-20241022"

    # The scripted adapter MUST have been called with the canonical id —
    # this is what proves slot 1 (per_message_override) resolved instead of
    # falling through to slot 7 (global_default = sonnet).
    assert len(scripted_adapter.requests) == 1
    assert scripted_adapter.requests[0].model == "anthropic:claude-3-5-haiku-20241022"

    await runtime.bus.drain()
    payloads = _load_event_payloads(runtime)

    # route.decided: chosen_model is the canonical id, slot 1 wins.
    route = payloads["route.decided"]
    assert route["chosen_model"] == "anthropic:claude-3-5-haiku-20241022"

    # llm.call_completed: model + cost reflect the canonical rate (not sonnet).
    completed = payloads["llm.call_completed"]
    assert completed["model"] == "anthropic:claude-3-5-haiku-20241022"
    expected_cost = (Decimal(1000) * Decimal("0.80") + Decimal(200) * Decimal("4.00")) / Decimal(
        1_000_000
    )
    assert completed["cost_usd"] == pytest.approx(float(expected_cost))
    # Sanity: this is well under the sonnet rate ($3 in / $15 out x same tokens
    # = $0.006), confirming the haiku→sonnet fallthrough is gone.
    sonnet_cost = (Decimal(1000) * Decimal("3.00") + Decimal(200) * Decimal("15.00")) / Decimal(
        1_000_000
    )
    assert completed["cost_usd"] < float(sonnet_cost)


async def test_openai_shape_bare_name_normalizes_to_canonical_id(
    client, bearer_token, scripted_adapter, runtime
) -> None:
    """OpenAI SDK clients send `gpt-4o-mini` (no prefix). Same fix path —
    the bare name must resolve to the canonical `openai:gpt-4o-mini`."""
    _register_gpt_4o_mini(runtime, scripted_adapter)
    scripted_adapter.push_response(text="ok", input_tokens=500, output_tokens=100)
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {bearer_token}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200, r.text
    # Buyer echoes back the raw client string.
    assert r.json()["model"] == "gpt-4o-mini"

    assert len(scripted_adapter.requests) == 1
    assert scripted_adapter.requests[0].model == "openai:gpt-4o-mini"

    await runtime.bus.drain()
    payloads = _load_event_payloads(runtime)
    route = payloads["route.decided"]
    assert route["chosen_model"] == "openai:gpt-4o-mini"

    completed = payloads["llm.call_completed"]
    assert completed["model"] == "openai:gpt-4o-mini"
    expected_cost = (Decimal(500) * Decimal("0.15") + Decimal(100) * Decimal("0.60")) / Decimal(
        1_000_000
    )
    assert completed["cost_usd"] == pytest.approx(float(expected_cost))


async def test_anthropic_shape_haiku_canonical_id_unchanged(
    client, bearer_token, scripted_adapter, runtime
) -> None:
    """The pre-existing canonical `anthropic:claude-haiku-4-5` registration is
    still honored — normalization passes the name through and routing slot 1
    picks it (the GA-blocker repro's "intended" path that today only works if
    the client happens to pass the prefixed form)."""
    scripted_adapter.push_response(text="ok", input_tokens=200, output_tokens=50)
    r = await client.post(
        "/v1/messages",
        headers={"x-api-key": bearer_token, "anthropic-version": "2023-06-01"},
        json={
            "model": "claude-haiku-4-5",  # bare; SDK strips the `anthropic:`
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200, r.text
    assert scripted_adapter.requests[0].model == "anthropic:claude-haiku-4-5"

    await runtime.bus.drain()
    payloads = _load_event_payloads(runtime)
    assert payloads["route.decided"]["chosen_model"] == "anthropic:claude-haiku-4-5"

    completed = payloads["llm.call_completed"]
    # DEFAULT_PRICE_TABLE haiku rates: $1.00/M in, $5.00/M out.
    expected_cost = (Decimal(200) * Decimal("1.00") + Decimal(50) * Decimal("5.00")) / Decimal(
        1_000_000
    )
    assert completed["cost_usd"] == pytest.approx(float(expected_cost))


def _load_event_payloads(runtime) -> dict[str, dict]:
    conn = sqlite3.connect(runtime.db_file)
    try:
        rows = conn.execute("SELECT type, payload_json FROM events ORDER BY id").fetchall()
        return {row[0]: json.loads(row[1]) for row in rows}
    finally:
        conn.close()
