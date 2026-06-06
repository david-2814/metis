"""LLMRouter module tests (routing-engine.md §4.6 + routing/llm_router.py).

These tests exercise the meta-call logic directly with a scripted adapter,
the actual price table, and a fresh BudgetTracker. The engine-side wiring
of slot 5 is covered in test_engine_llm_router.py.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from metis.core.adapters.protocol import (
    CanonicalRequest,
    CanonicalResponse,
    StopReason,
    TokenUsage,
)
from metis.core.canonical.capabilities import AdapterCapabilities
from metis.core.canonical.content import TextBlock, ToolUseBlock
from metis.core.canonical.ids import next_monotonic_ulid
from metis.core.eval.budget import BudgetTracker
from metis.core.pricing.table import ModelPricing, PriceTable
from metis.core.routing.availability import AvailabilityState, ProviderAvailability
from metis.core.routing.llm_router import (
    LLMRouter,
    requirements_from_ctx,
)
from metis.core.routing.policy import LLMRouterConfig
from metis.core.routing.registry import ModelRegistry

# ---- Fixtures ----------------------------------------------------------


def _caps(**overrides) -> AdapterCapabilities:
    base = dict(
        supports_thinking=False,
        supports_images=True,
        supports_tools=True,
        supports_system_prompt=True,
        supports_structured_output=False,
        supports_streaming=True,
        supports_streaming_tool_calls=True,
        supports_parallel_tool_calls=True,
        supports_prompt_caching=False,
        supports_system_messages_in_list=False,
        max_context_tokens=200_000,
        max_output_tokens=8192,
        accepted_image_media_types=["image/png", "image/jpeg"],
    )
    base.update(overrides)
    return AdapterCapabilities(**base)


class _ScriptedAdapter:
    """Adapter that returns a canned response on each `complete()` call.

    `responses` is a list of (content, usage) tuples consumed in order.
    Raising classes (e.g. TimeoutError) can be queued instead of tuples;
    they will be raised on the corresponding call.
    """

    name = "scripted"

    def __init__(
        self,
        caps_map: dict[str, AdapterCapabilities],
        responses: list,
    ) -> None:
        self.caps_map = caps_map
        self._responses = list(responses)
        self.requests: list[CanonicalRequest] = []

    def capabilities_for(self, model: str) -> AdapterCapabilities:
        return self.caps_map[model]

    async def complete(self, request: CanonicalRequest) -> CanonicalResponse:
        self.requests.append(request)
        if not self._responses:
            raise RuntimeError("scripted adapter exhausted")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item()
        content, usage = item
        return CanonicalResponse(
            request_id=request.request_id,
            model=request.model,
            provider="scripted",
            content=content,
            stop_reason=StopReason.TOOL_USE,
            usage=usage,
            latency_ms=12,
        )

    def estimate_input_tokens(self, *args, **kwargs) -> int:
        return 0

    async def cancel(self, request_id: str) -> bool:
        return False

    async def close(self) -> None:
        return

    def stream(self, request):
        raise NotImplementedError


class _HangingAdapter(_ScriptedAdapter):
    """Adapter whose `complete()` never returns; used to exercise timeout."""

    async def complete(self, request):
        self.requests.append(request)
        await asyncio.Event().wait()  # blocks forever


def _registry_with(adapter, model_ids: list[str]) -> ModelRegistry:
    reg = ModelRegistry()
    for mid in model_ids:
        reg.register(model_id=mid, adapter=adapter)
    return reg


def _price_table() -> PriceTable:
    """Cheap, predictable per-MTok rates for deterministic cost math."""
    return PriceTable(
        version="t1",
        models={
            "openrouter:qwen/qwen-plus": ModelPricing(
                input_per_mtok=Decimal("0.40"),
                output_per_mtok=Decimal("1.20"),
            ),
            "anthropic:claude-haiku-4-5": ModelPricing(
                input_per_mtok=Decimal("0.80"),
                output_per_mtok=Decimal("4.00"),
            ),
            "anthropic:claude-sonnet-4-6": ModelPricing(
                input_per_mtok=Decimal("3.00"),
                output_per_mtok=Decimal("15.00"),
            ),
            "openai:gpt-text-only": ModelPricing(
                input_per_mtok=Decimal("0.50"),
                output_per_mtok=Decimal("1.50"),
            ),
        },
    )


def _tool_use_response(model_id: str, reason: str = "") -> list:
    return [
        ToolUseBlock(
            id=f"tu_{next_monotonic_ulid()}",
            name="choose_model",
            input={"model_id": model_id, "reason": reason},
        )
    ]


def _usage(input_tokens=100, output_tokens=40) -> TokenUsage:
    return TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)


# ---- Successful pick ---------------------------------------------------


async def test_router_picks_a_candidate_and_records_cost():
    caps = {
        "openrouter:qwen/qwen-plus": _caps(),
        "anthropic:claude-haiku-4-5": _caps(),
        "anthropic:claude-sonnet-4-6": _caps(),
    }
    adapter = _ScriptedAdapter(
        caps_map=caps,
        responses=[
            (_tool_use_response("anthropic:claude-haiku-4-5", reason="quick edit"), _usage()),
        ],
    )
    registry = _registry_with(
        adapter,
        [
            "openrouter:qwen/qwen-plus",
            "anthropic:claude-haiku-4-5",
            "anthropic:claude-sonnet-4-6",
        ],
    )
    budget = BudgetTracker()
    router = LLMRouter(
        config=LLMRouterConfig(enabled=True, model="openrouter:qwen/qwen-plus"),
        registry=registry,
        availability=ProviderAvailability(),
        price_table=_price_table(),
        budget_tracker=budget,
    )
    result = await router.decide(user_prompt="refactor this", session_id="s1")
    assert result.chosen_model == "anthropic:claude-haiku-4-5"
    assert result.failure_reason is None
    assert result.reason_text == "quick edit"
    # Cost = 100 input * $0.40/MTok + 40 output * $1.20/MTok = $0.00004 + $0.000048 = $0.000088
    expected = (Decimal(100) * Decimal("0.40") + Decimal(40) * Decimal("1.20")) / Decimal("1000000")
    assert result.meta_cost_usd == expected
    assert budget.session_spend("s1") == expected
    # Adapter was called once with the candidate enum NOT including the router model itself.
    assert len(adapter.requests) == 1
    tool_schema = adapter.requests[0].tools[0].input_schema
    enum = tool_schema["properties"]["model_id"]["enum"]
    assert "openrouter:qwen/qwen-plus" not in enum  # router can't pick itself
    assert "anthropic:claude-haiku-4-5" in enum
    assert "anthropic:claude-sonnet-4-6" in enum


async def test_router_excludes_unhealthy_candidates():
    caps = {
        "openrouter:qwen/qwen-plus": _caps(),
        "anthropic:claude-haiku-4-5": _caps(),
        "anthropic:claude-sonnet-4-6": _caps(),
    }
    adapter = _ScriptedAdapter(
        caps_map=caps,
        responses=[(_tool_use_response("anthropic:claude-haiku-4-5"), _usage())],
    )
    registry = _registry_with(
        adapter,
        [
            "openrouter:qwen/qwen-plus",
            "anthropic:claude-haiku-4-5",
            "anthropic:claude-sonnet-4-6",
        ],
    )
    availability = ProviderAvailability()
    # Force sonnet into UNAVAILABLE state.
    state = availability._models
    from metis.core.routing.availability import _ModelState

    state[("anthropic", "anthropic:claude-sonnet-4-6")] = _ModelState(
        state=AvailabilityState.UNAVAILABLE,
        last_call_at=1e18,  # far future so auto-recovery doesn't fire
    )
    router = LLMRouter(
        config=LLMRouterConfig(enabled=True, model="openrouter:qwen/qwen-plus"),
        registry=registry,
        availability=availability,
        price_table=_price_table(),
        budget_tracker=BudgetTracker(),
    )
    await router.decide(user_prompt="hello", session_id="s1")
    enum = adapter.requests[0].tools[0].input_schema["properties"]["model_id"]["enum"]
    assert "anthropic:claude-sonnet-4-6" not in enum
    assert "anthropic:claude-haiku-4-5" in enum


async def test_router_filters_by_capability_requirements():
    caps = {
        "openrouter:qwen/qwen-plus": _caps(),
        "anthropic:claude-haiku-4-5": _caps(),
        "openai:gpt-text-only": _caps(supports_images=False, supports_tools=False),
    }
    adapter = _ScriptedAdapter(
        caps_map=caps,
        responses=[(_tool_use_response("anthropic:claude-haiku-4-5"), _usage())],
    )
    registry = _registry_with(
        adapter,
        [
            "openrouter:qwen/qwen-plus",
            "anthropic:claude-haiku-4-5",
            "openai:gpt-text-only",
        ],
    )
    router = LLMRouter(
        config=LLMRouterConfig(enabled=True, model="openrouter:qwen/qwen-plus"),
        registry=registry,
        availability=ProviderAvailability(),
        price_table=_price_table(),
        budget_tracker=BudgetTracker(),
    )
    reqs = requirements_from_ctx(
        estimated_input_tokens=100,
        has_images=True,
        has_tool_definitions=True,
        has_system_prompt=False,
        requires_structured_output=False,
    )
    await router.decide(user_prompt="hi", session_id="s1", ctx_requirements=reqs)
    enum = adapter.requests[0].tools[0].input_schema["properties"]["model_id"]["enum"]
    assert "openai:gpt-text-only" not in enum  # no images, no tools
    assert "anthropic:claude-haiku-4-5" in enum


# ---- Failure modes -----------------------------------------------------


async def test_router_timeout_returns_failure():
    caps = {
        "openrouter:qwen/qwen-plus": _caps(),
        "anthropic:claude-haiku-4-5": _caps(),
    }
    adapter = _HangingAdapter(caps_map=caps, responses=[])
    registry = _registry_with(adapter, ["openrouter:qwen/qwen-plus", "anthropic:claude-haiku-4-5"])
    router = LLMRouter(
        config=LLMRouterConfig(
            enabled=True, model="openrouter:qwen/qwen-plus", timeout_seconds=1.0
        ),
        registry=registry,
        availability=ProviderAvailability(),
        price_table=_price_table(),
        budget_tracker=BudgetTracker(),
    )
    result = await router.decide(user_prompt="hi", session_id="s1")
    assert result.chosen_model is None
    assert result.failure_reason == "timeout"
    assert result.meta_cost_usd == Decimal("0")


async def test_router_network_error_returns_failure():
    caps = {
        "openrouter:qwen/qwen-plus": _caps(),
        "anthropic:claude-haiku-4-5": _caps(),
    }
    adapter = _ScriptedAdapter(
        caps_map=caps,
        responses=[ConnectionError("DNS failure")],
    )
    registry = _registry_with(adapter, ["openrouter:qwen/qwen-plus", "anthropic:claude-haiku-4-5"])
    router = LLMRouter(
        config=LLMRouterConfig(enabled=True, model="openrouter:qwen/qwen-plus"),
        registry=registry,
        availability=ProviderAvailability(),
        price_table=_price_table(),
        budget_tracker=BudgetTracker(),
    )
    result = await router.decide(user_prompt="hi", session_id="s1")
    assert result.chosen_model is None
    assert result.failure_reason == "network_error: ConnectionError"


async def test_router_invalid_model_id_returns_failure():
    caps = {
        "openrouter:qwen/qwen-plus": _caps(),
        "anthropic:claude-haiku-4-5": _caps(),
    }
    adapter = _ScriptedAdapter(
        caps_map=caps,
        responses=[
            (_tool_use_response("hallucinated:claude-opus-99"), _usage()),
        ],
    )
    registry = _registry_with(adapter, ["openrouter:qwen/qwen-plus", "anthropic:claude-haiku-4-5"])
    router = LLMRouter(
        config=LLMRouterConfig(enabled=True, model="openrouter:qwen/qwen-plus"),
        registry=registry,
        availability=ProviderAvailability(),
        price_table=_price_table(),
        budget_tracker=BudgetTracker(),
    )
    result = await router.decide(user_prompt="hi", session_id="s1")
    assert result.chosen_model is None
    assert result.failure_reason is not None
    assert result.failure_reason.startswith("invalid_model_id: hallucinated")
    # Cost was still recorded — the call happened even though the pick was rejected.
    assert result.meta_cost_usd > 0


async def test_router_no_model_chosen_returns_failure():
    caps = {
        "openrouter:qwen/qwen-plus": _caps(),
        "anthropic:claude-haiku-4-5": _caps(),
    }
    adapter = _ScriptedAdapter(
        caps_map=caps,
        responses=[
            ([TextBlock(text="I refuse to call the tool")], _usage()),
        ],
    )
    registry = _registry_with(adapter, ["openrouter:qwen/qwen-plus", "anthropic:claude-haiku-4-5"])
    router = LLMRouter(
        config=LLMRouterConfig(enabled=True, model="openrouter:qwen/qwen-plus"),
        registry=registry,
        availability=ProviderAvailability(),
        price_table=_price_table(),
        budget_tracker=BudgetTracker(),
    )
    result = await router.decide(user_prompt="hi", session_id="s1")
    assert result.chosen_model is None
    assert result.failure_reason == "no_model_chosen"


async def test_router_no_candidates_when_registry_empty():
    """Only the router model is registered → no candidates left after self-exclusion."""
    caps = {"openrouter:qwen/qwen-plus": _caps()}
    adapter = _ScriptedAdapter(caps_map=caps, responses=[])
    registry = _registry_with(adapter, ["openrouter:qwen/qwen-plus"])
    router = LLMRouter(
        config=LLMRouterConfig(enabled=True, model="openrouter:qwen/qwen-plus"),
        registry=registry,
        availability=ProviderAvailability(),
        price_table=_price_table(),
        budget_tracker=BudgetTracker(),
    )
    result = await router.decide(user_prompt="hi", session_id="s1")
    assert result.chosen_model is None
    assert result.failure_reason == "no_candidates"
    assert adapter.requests == []  # no LLM call attempted


async def test_router_budget_exhausted_short_circuits():
    caps = {
        "openrouter:qwen/qwen-plus": _caps(),
        "anthropic:claude-haiku-4-5": _caps(),
    }
    adapter = _ScriptedAdapter(
        caps_map=caps,
        responses=[(_tool_use_response("anthropic:claude-haiku-4-5"), _usage())],
    )
    registry = _registry_with(adapter, ["openrouter:qwen/qwen-plus", "anthropic:claude-haiku-4-5"])
    # Pre-charge the budget tracker so the next projected call exceeds the cap.
    budget = BudgetTracker(per_session_max_usd=Decimal("0.001"))
    budget.record(session_id="s1", cost_usd=Decimal("0.0009"))
    router = LLMRouter(
        config=LLMRouterConfig(enabled=True, model="openrouter:qwen/qwen-plus"),
        registry=registry,
        availability=ProviderAvailability(),
        price_table=_price_table(),
        budget_tracker=budget,
    )
    result = await router.decide(user_prompt="hi", session_id="s1")
    assert result.chosen_model is None
    assert result.failure_reason == "budget_exhausted"
    assert adapter.requests == []  # the call was skipped


async def test_system_prompt_includes_per_mtok_prices():
    """Catalog lines must carry input + output $/MTok rates so the router LLM
    can anchor "cheapest" against concrete numbers. The first-cut prompt
    omitted prices and qwen-plus picked sonnet-4-6 for `test`."""
    caps = {
        "openrouter:qwen/qwen-plus": _caps(),
        "anthropic:claude-haiku-4-5": _caps(),
        "anthropic:claude-sonnet-4-6": _caps(),
    }
    adapter = _ScriptedAdapter(
        caps_map=caps,
        responses=[(_tool_use_response("anthropic:claude-haiku-4-5"), _usage())],
    )
    registry = _registry_with(
        adapter,
        [
            "openrouter:qwen/qwen-plus",
            "anthropic:claude-haiku-4-5",
            "anthropic:claude-sonnet-4-6",
        ],
    )
    router = LLMRouter(
        config=LLMRouterConfig(enabled=True, model="openrouter:qwen/qwen-plus"),
        registry=registry,
        availability=ProviderAvailability(),
        price_table=_price_table(),
        budget_tracker=BudgetTracker(),
    )
    await router.decide(user_prompt="test", session_id="s1")
    system = adapter.requests[0].system_prompt or ""
    # Concrete rates from _price_table() pinned at the top of this file.
    assert "in $0.8/MTok, out $4/MTok" in system  # haiku-4-5
    assert "in $3/MTok, out $15/MTok" in system  # sonnet-4-6
    # The catalog line label tells the LLM lower = cheaper.
    assert "lower = cheaper" in system


async def test_system_prompt_biases_toward_cheap_default():
    """Guidance must explicitly say 'default to cheapest for short / ambiguous /
    conversational prompts' — not 'when in doubt pick balanced'. The latter is
    why qwen-plus picked sonnet for a one-word prompt on 2026-06-04."""
    caps = {
        "openrouter:qwen/qwen-plus": _caps(),
        "anthropic:claude-haiku-4-5": _caps(),
    }
    adapter = _ScriptedAdapter(
        caps_map=caps,
        responses=[(_tool_use_response("anthropic:claude-haiku-4-5"), _usage())],
    )
    registry = _registry_with(adapter, ["openrouter:qwen/qwen-plus", "anthropic:claude-haiku-4-5"])
    router = LLMRouter(
        config=LLMRouterConfig(enabled=True, model="openrouter:qwen/qwen-plus"),
        registry=registry,
        availability=ProviderAvailability(),
        price_table=_price_table(),
        budget_tracker=BudgetTracker(),
    )
    await router.decide(user_prompt="test", session_id="s1")
    system = adapter.requests[0].system_prompt or ""
    assert "Default to the cheapest" in system
    # The old fallback ("when in doubt pick balanced") must not appear — the
    # phrasing is gone, replaced by the explicit cheap-default rule.
    assert "When in doubt" not in system
    # Examples in the prompt enumerate the kind of prompts we don't want
    # escalated.
    assert '"test"' in system or "test" in system.lower()


async def test_system_prompt_handles_missing_pricing_gracefully():
    """A candidate without a PriceTable entry shouldn't crash the prompt
    build — it just gets a `price unknown` marker."""
    caps = {
        "openrouter:qwen/qwen-plus": _caps(),
        "openrouter:exotic/unpriced-model": _caps(),
    }
    adapter = _ScriptedAdapter(
        caps_map=caps,
        responses=[(_tool_use_response("openrouter:exotic/unpriced-model"), _usage())],
    )
    registry = _registry_with(
        adapter, ["openrouter:qwen/qwen-plus", "openrouter:exotic/unpriced-model"]
    )
    router = LLMRouter(
        config=LLMRouterConfig(enabled=True, model="openrouter:qwen/qwen-plus"),
        registry=registry,
        availability=ProviderAvailability(),
        price_table=_price_table(),  # exotic/unpriced-model is NOT in it
        budget_tracker=BudgetTracker(),
    )
    result = await router.decide(user_prompt="hello", session_id="s1")
    # Router still completes; unpriced model marked but selectable.
    assert result.chosen_model == "openrouter:exotic/unpriced-model"
    system = adapter.requests[0].system_prompt or ""
    assert "openrouter:exotic/unpriced-model" in system
    assert "price unknown" in system


async def test_emits_llm_call_events_stamped_actor_router():
    """routing-engine.md §4.6.7: the meta-call must land in the trace store
    as `llm.call_started` + `llm.call_completed` stamped Actor.ROUTER so
    analytics can attribute meta-spend separately from planner traffic."""
    from metis.core.events.bus import EventBus, EventFilter, Subscription
    from metis.core.events.envelope import Actor

    caps = {
        "openrouter:qwen/qwen-plus": _caps(),
        "anthropic:claude-haiku-4-5": _caps(),
    }
    adapter = _ScriptedAdapter(
        caps_map=caps,
        responses=[(_tool_use_response("anthropic:claude-haiku-4-5", reason="quick"), _usage())],
    )
    registry = _registry_with(adapter, ["openrouter:qwen/qwen-plus", "anthropic:claude-haiku-4-5"])
    bus = EventBus()
    bus.start()
    captured = []

    async def handler(event):
        captured.append(event)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="t", fast_path=True))

    router = LLMRouter(
        config=LLMRouterConfig(enabled=True, model="openrouter:qwen/qwen-plus"),
        registry=registry,
        availability=ProviderAvailability(),
        price_table=_price_table(),
        budget_tracker=BudgetTracker(),
        bus=bus,
    )
    result = await router.decide(user_prompt="refactor this", session_id="s1")
    await bus.drain()
    await bus.stop()

    assert result.chosen_model == "anthropic:claude-haiku-4-5"
    starts = [e for e in captured if e.type == "llm.call_started"]
    completes = [e for e in captured if e.type == "llm.call_completed"]
    assert len(starts) == 1
    assert len(completes) == 1
    assert starts[0].actor == Actor.ROUTER
    assert completes[0].actor == Actor.ROUTER
    assert starts[0].payload["model"] == "openrouter:qwen/qwen-plus"
    assert completes[0].payload["model"] == "openrouter:qwen/qwen-plus"
    assert completes[0].payload["produced_tool_calls"] == 1
    # Happy path produced only a ToolUseBlock — no text preview to capture.
    assert completes[0].payload["response_text_preview"] is None


async def test_no_model_chosen_populates_response_text_preview():
    """The motivating case: when the router writes prose instead of a tool
    call, the trace store should carry the prose so the failure is
    inspectable post-hoc. Discarding it left users blind on 2026-06-04
    when qwen3.6-27b failed `no_model_chosen` against a 17K-token catalog."""
    from metis.core.events.bus import EventBus, EventFilter, Subscription

    caps = {
        "openrouter:qwen/qwen-plus": _caps(),
        "anthropic:claude-haiku-4-5": _caps(),
    }
    failure_prose = (
        "Looking at the candidates, the best balanced choice would be "
        "claude-sonnet-4-6 given the strong reasoning capabilities."
    )
    adapter = _ScriptedAdapter(
        caps_map=caps,
        responses=[([TextBlock(text=failure_prose)], _usage())],
    )
    registry = _registry_with(adapter, ["openrouter:qwen/qwen-plus", "anthropic:claude-haiku-4-5"])
    bus = EventBus()
    bus.start()
    captured = []

    async def handler(event):
        captured.append(event)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="t", fast_path=True))

    router = LLMRouter(
        config=LLMRouterConfig(enabled=True, model="openrouter:qwen/qwen-plus"),
        registry=registry,
        availability=ProviderAvailability(),
        price_table=_price_table(),
        budget_tracker=BudgetTracker(),
        bus=bus,
    )
    result = await router.decide(user_prompt="hi", session_id="s1")
    await bus.drain()
    await bus.stop()

    assert result.failure_reason == "no_model_chosen"
    completes = [e for e in captured if e.type == "llm.call_completed"]
    assert len(completes) == 1
    # The router's prose is captured on the completed event so a SQLite
    # query like `SELECT json_extract(payload_json, '$.response_text_preview')
    # FROM events WHERE type='llm.call_completed' AND actor='router'` can
    # debug `no_model_chosen` failures post-hoc.
    assert completes[0].payload["response_text_preview"] == failure_prose
    assert completes[0].payload["produced_tool_calls"] == 0


async def test_response_text_preview_truncates_long_prose():
    """A router that writes a 5K-token essay shouldn't bloat every event row."""
    from metis.core.events.bus import EventBus, EventFilter, Subscription

    caps = {
        "openrouter:qwen/qwen-plus": _caps(),
        "anthropic:claude-haiku-4-5": _caps(),
    }
    long_prose = "x" * 5000
    adapter = _ScriptedAdapter(
        caps_map=caps,
        responses=[([TextBlock(text=long_prose)], _usage())],
    )
    registry = _registry_with(adapter, ["openrouter:qwen/qwen-plus", "anthropic:claude-haiku-4-5"])
    bus = EventBus()
    bus.start()
    captured = []

    async def handler(event):
        captured.append(event)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="t", fast_path=True))

    router = LLMRouter(
        config=LLMRouterConfig(enabled=True, model="openrouter:qwen/qwen-plus"),
        registry=registry,
        availability=ProviderAvailability(),
        price_table=_price_table(),
        budget_tracker=BudgetTracker(),
        bus=bus,
    )
    await router.decide(user_prompt="hi", session_id="s1")
    await bus.drain()
    await bus.stop()

    completes = [e for e in captured if e.type == "llm.call_completed"]
    preview = completes[0].payload["response_text_preview"]
    assert preview is not None
    assert len(preview) <= 500  # _RESPONSE_PREVIEW_MAX_CHARS upstream cap
    assert preview.endswith("…")  # ellipsis marker on truncation


async def test_emits_no_events_when_bus_not_injected():
    """The bus arg is optional so tests that don't care about the trace
    can construct an LLMRouter without one. Verify no crashes."""
    caps = {"openrouter:qwen/qwen-plus": _caps(), "anthropic:claude-haiku-4-5": _caps()}
    adapter = _ScriptedAdapter(
        caps_map=caps,
        responses=[(_tool_use_response("anthropic:claude-haiku-4-5"), _usage())],
    )
    registry = _registry_with(adapter, ["openrouter:qwen/qwen-plus", "anthropic:claude-haiku-4-5"])
    router = LLMRouter(
        config=LLMRouterConfig(enabled=True, model="openrouter:qwen/qwen-plus"),
        registry=registry,
        availability=ProviderAvailability(),
        price_table=_price_table(),
        budget_tracker=BudgetTracker(),
        # bus omitted
    )
    result = await router.decide(user_prompt="hi", session_id="s1")
    assert result.chosen_model == "anthropic:claude-haiku-4-5"


async def test_adapter_cancelled_classifies_as_timeout():
    """When our `wait_for` fires its timeout, the cancellation propagates
    into the adapter, which catches `asyncio.CancelledError` and re-raises
    as `adapters.errors.CancelledError`. That wrapped exception escapes
    `wait_for` (which only converts the bare asyncio variant) and would
    otherwise be misclassified as `network_error: CancelledError`. The
    router must instead classify it as `timeout` for accurate telemetry."""
    from metis.core.adapters.errors import CancelledError as AdapterCancelledError

    caps = {
        "openrouter:qwen/qwen-plus": _caps(),
        "anthropic:claude-haiku-4-5": _caps(),
    }
    adapter = _ScriptedAdapter(
        caps_map=caps,
        responses=[AdapterCancelledError("request cancelled", request_id="rq_test")],
    )
    registry = _registry_with(adapter, ["openrouter:qwen/qwen-plus", "anthropic:claude-haiku-4-5"])
    router = LLMRouter(
        config=LLMRouterConfig(enabled=True, model="openrouter:qwen/qwen-plus"),
        registry=registry,
        availability=ProviderAvailability(),
        price_table=_price_table(),
        budget_tracker=BudgetTracker(),
    )
    result = await router.decide(user_prompt="hi", session_id="s1")
    assert result.chosen_model is None
    assert result.failure_reason == "timeout"
    # Critically NOT "network_error: CancelledError" — that's the
    # misclassification we're guarding against.


async def test_router_unknown_router_model_returns_failure():
    caps = {"anthropic:claude-haiku-4-5": _caps()}
    adapter = _ScriptedAdapter(caps_map=caps, responses=[])
    registry = _registry_with(adapter, ["anthropic:claude-haiku-4-5"])
    router = LLMRouter(
        config=LLMRouterConfig(enabled=True, model="not:registered"),
        registry=registry,
        availability=ProviderAvailability(),
        price_table=_price_table(),
        budget_tracker=BudgetTracker(),
    )
    result = await router.decide(user_prompt="hi", session_id="s1")
    assert result.chosen_model is None
    assert result.failure_reason is not None
    assert "invalid_router_model" in result.failure_reason
