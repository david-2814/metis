"""v2 routing slot-4 integration: engine consults the embedding cache and
uses blended similarity end-to-end when a v2 workspace is configured.

Mirrors `test_routing_integration.py` but flips the PatternConfig into v2
mode and shows the cache-hit path lifts a neighbor with a closer embedding
above one whose structural features are equally similar but whose
embedding diverges.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

_routing_dir = Path(__file__).parent.parent / "routing"
if str(_routing_dir) not in sys.path:
    sys.path.insert(0, str(_routing_dir))

from _helpers import StubAdapter  # noqa: E402
from metis_core.canonical.capabilities import AdapterCapabilities  # noqa: E402
from metis_core.events.bus import EventBus, EventFilter, Subscription  # noqa: E402
from metis_core.events.envelope import Event  # noqa: E402
from metis_core.patterns.fingerprint import FingerprintInputs, compute_fingerprint  # noqa: E402
from metis_core.patterns.store import PatternStore  # noqa: E402
from metis_core.routing.context import TurnContext  # noqa: E402
from metis_core.routing.engine import RoutingEngine  # noqa: E402
from metis_core.routing.policy import (  # noqa: E402
    EMPTY_POLICY,
    PatternConfig,
    RoutingPolicy,
)
from metis_core.routing.registry import ModelRegistry  # noqa: E402


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


@pytest.fixture
def registry() -> ModelRegistry:
    caps_map = {
        "anthropic:haiku": _caps(),
        "anthropic:sonnet": _caps(),
    }
    adapter = StubAdapter(name="anthropic", caps_map=caps_map)
    reg = ModelRegistry()
    reg.register(model_id="anthropic:haiku", adapter=adapter, aliases=["haiku"])
    reg.register(model_id="anthropic:sonnet", adapter=adapter, aliases=["sonnet"])
    return reg


@pytest.fixture
async def bus() -> EventBus:
    b = EventBus()
    b.start()
    yield b
    await b.stop()


@pytest.fixture
async def event_log(bus: EventBus) -> list[Event]:
    log: list[Event] = []

    async def handler(e: Event) -> None:
        log.append(e)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="log", fast_path=True))
    return log


def _inputs_for_ctx(ctx: TurnContext) -> FingerprintInputs:
    return FingerprintInputs(
        user_message_text=ctx.user_message_text,
        workspace_path=ctx.workspace_path,
        estimated_input_tokens=ctx.estimated_input_tokens,
        has_images=ctx.has_images,
        has_tool_calls_in_history=ctx.has_tool_calls_in_history,
        file_extensions=(".py",),
        file_path_buckets=("src",),
        tool_names=("read_file",),
        side_effect_classes=("read",),
    )


def _ctx(workspace: str, **overrides) -> TurnContext:
    base = dict(
        session_id="sess_1",
        turn_id="turn_1",
        estimated_input_tokens=1_000,
        has_images=False,
        has_tool_definitions=False,
        has_system_prompt=False,
        has_tool_calls_in_history=False,
        per_message_override=None,
        session_active_model=None,
        workspace_default_model="anthropic:sonnet",
        global_default_model="anthropic:sonnet",
        user_message_text="please refactor this module",
        workspace_path=workspace,
    )
    base.update(overrides)
    return TurnContext(**base)


def _v2_policy() -> RoutingPolicy:
    return RoutingPolicy(
        schema_version=EMPTY_POLICY.schema_version,
        global_default=None,
        tiers=None,
        pattern=PatternConfig(
            fingerprint_version="v2",
            embedding_provider="openai:text-embedding-3-small",
            embedding_alpha=0.6,
            cost_weight=0.0,
            min_confidence=0.05,
            min_sample_size=2,
        ),
        rules=(),
        workspaces=(),
    )


async def test_v2_routing_uses_blended_similarity_via_cache_hit(
    registry, bus, event_log, tmp_path
) -> None:
    store = PatternStore(
        tmp_path,
        fingerprint_version="v2",
        embedding_alpha=0.6,
    )
    try:
        # Build prior history: haiku saw a "refactor"-shaped turn with an
        # embedding orthogonal to the query; sonnet saw an identically
        # structured turn whose embedding aligns with the query.
        ctx_seed = _ctx(workspace=str(tmp_path))
        base_inputs = _inputs_for_ctx(ctx_seed)

        haiku_inputs = FingerprintInputs(
            **{
                **{k: getattr(base_inputs, k) for k in base_inputs.__dataclass_fields__},
                "user_message_text": "haiku-seen",
                "embedding": (0.0, 1.0),
                "embedding_provider": "openai:text-embedding-3-small",
            }
        )
        sonnet_inputs = FingerprintInputs(
            **{
                **{k: getattr(base_inputs, k) for k in base_inputs.__dataclass_fields__},
                "user_message_text": "sonnet-seen",
                "embedding": (1.0, 0.0),
                "embedding_provider": "openai:text-embedding-3-small",
            }
        )
        haiku_fp = compute_fingerprint(haiku_inputs)
        sonnet_fp = compute_fingerprint(sonnet_inputs)
        # Tied costs so the cost-efficiency term is zero (cost_weight=0
        # already); success-score spread is what creates the confidence.
        for _ in range(5):
            store.record(haiku_fp, "anthropic:haiku", 0.5, Decimal("0.005"), 800.0, "v1")
        for _ in range(5):
            store.record(sonnet_fp, "anthropic:sonnet", 0.9, Decimal("0.005"), 800.0, "v1")

        # Pre-populate the cache with the query's embedding so the
        # routing-time lookup hits (no API call required).
        store.store_embedding(
            ctx_seed.user_message_text,
            "openai:text-embedding-3-small",
            (1.0, 0.0),
        )

        engine = RoutingEngine(
            registry=registry,
            bus=bus,
            policy=_v2_policy(),
            pattern_store_resolver=lambda ws: store if ws == str(tmp_path) else None,
            fingerprint_inputs_builder=_inputs_for_ctx,
        )
        decision = engine.decide(_ctx(workspace=str(tmp_path)))
        assert decision.chosen_model == "anthropic:sonnet"
        pattern_slot = next(p for p in decision.chain if p.policy == "pattern")
        assert pattern_slot.verdict == "chose"
        await bus.drain()
        matched = [e for e in event_log if e.type == "pattern.matched"]
        assert len(matched) == 1
        assert matched[0].payload["chosen_model"] == "anthropic:sonnet"
    finally:
        store.close()


async def test_v2_routing_falls_back_to_v1_on_cache_miss(
    registry, bus, event_log, tmp_path
) -> None:
    """No cache pre-population → routing query gets an embedding=None
    fingerprint → K-NN falls back to v1 weighted-Jaccard without blocking
    on an embedding API call (§16.6)."""
    store = PatternStore(tmp_path, fingerprint_version="v2", embedding_alpha=0.6)
    try:
        ctx_seed = _ctx(workspace=str(tmp_path))
        base_inputs = _inputs_for_ctx(ctx_seed)
        fp = compute_fingerprint(base_inputs)
        for _ in range(5):
            store.record(fp, "anthropic:haiku", 0.9, Decimal("0.005"), 800.0, "v1")
        for _ in range(5):
            store.record(fp, "anthropic:sonnet", 0.3, Decimal("0.020"), 1500.0, "v1")

        engine = RoutingEngine(
            registry=registry,
            bus=bus,
            policy=_v2_policy(),
            pattern_store_resolver=lambda ws: store if ws == str(tmp_path) else None,
            fingerprint_inputs_builder=_inputs_for_ctx,
        )
        decision = engine.decide(_ctx(workspace=str(tmp_path)))
        # Pure v1 jaccard picks haiku on the higher success score.
        assert decision.chosen_model == "anthropic:haiku"
        pattern_slot = next(p for p in decision.chain if p.policy == "pattern")
        assert pattern_slot.verdict == "chose"
    finally:
        store.close()
