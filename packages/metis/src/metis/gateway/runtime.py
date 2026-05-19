"""Gateway runtime setup.

Composes the shared `metis-core` building blocks (event bus, trace store,
model registry, adapters, routing engine) into a stateless harness suitable
for HTTP-driven per-request calls. Unlike `metis.cli.runtime.setup_runtime`,
this builds *no* session manager, tool dispatcher, memory store, or skill
store — the gateway is a transparent proxy, not an agent (gateway.md §2).
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from metis.core.adapters.anthropic import AnthropicAdapter
from metis.core.adapters.openai import OpenAIAdapter
from metis.core.adapters.openrouter import OpenRouterAdapter
from metis.core.adapters.protocol import ProviderAdapter
from metis.core.events.bus import EventBus
from metis.core.pricing import DEFAULT_PRICE_TABLE, PriceTable
from metis.core.routing import EMPTY_POLICY, ModelRegistry, RoutingEngine
from metis.core.routing.profiles import standard_profile_for
from metis.core.trace.store import TraceStore
from metis.gateway.auth import Keystore
from metis.gateway.quotas import QuotaTracker

logger = logging.getLogger(__name__)


ANTHROPIC_MODELS = {
    "anthropic:claude-opus-4-7": ["opus", "deep"],
    "anthropic:claude-sonnet-4-6": ["sonnet", "balanced"],
    "anthropic:claude-haiku-4-5": ["haiku", "fast"],
}

OPENAI_MODELS = {
    "openai:gpt-5": ["gpt5", "gpt-5"],
    "openai:gpt-5-mini": ["gpt5-mini", "mini"],
}


class GatewaySetupError(Exception):
    """Raised when gateway runtime setup fails (missing API keys, bad keystore)."""


@dataclass
class GatewayRuntime:
    bus: EventBus
    trace: TraceStore
    registry: ModelRegistry
    routing: RoutingEngine
    pricing: PriceTable
    keystore: Keystore
    adapters: list[ProviderAdapter]
    db_file: Path
    global_default_model: str
    quota_tracker: QuotaTracker | None = None


async def setup_gateway_runtime(
    *,
    keystore_path: Path,
    db_path: Path | None,
    global_default_model: str,
) -> GatewayRuntime:
    """Build a fully-wired GatewayRuntime ready for the HTTP app.

    Raises `GatewaySetupError` on configuration problems so the CLI can render
    a friendly error.
    """
    if not keystore_path.exists():
        raise GatewaySetupError(f"gateway keystore not found: {keystore_path}")
    try:
        keystore = Keystore.from_file(keystore_path)
    except Exception as exc:
        raise GatewaySetupError(f"failed to load keystore {keystore_path}: {exc}") from exc

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    if not any((anthropic_key, openai_key, openrouter_key)):
        raise GatewaySetupError("set ANTHROPIC_API_KEY, OPENAI_API_KEY, and/or OPENROUTER_API_KEY")

    bus = EventBus()
    bus.start()

    db_file = db_path.expanduser() if db_path else default_db_path()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    trace = TraceStore(db_file)
    trace.attach_to(bus)

    registry = ModelRegistry()
    adapters: list[ProviderAdapter] = []
    pricing_table: PriceTable = DEFAULT_PRICE_TABLE
    if anthropic_key:
        anth = AnthropicAdapter(api_key=anthropic_key)
        adapters.append(anth)
        for model_id, aliases in ANTHROPIC_MODELS.items():
            registry.register(
                model_id=model_id,
                adapter=anth,
                aliases=aliases,
                task_profile=standard_profile_for(model_id),
            )
    if openai_key:
        oai = OpenAIAdapter(api_key=openai_key)
        adapters.append(oai)
        for model_id, aliases in OPENAI_MODELS.items():
            registry.register(
                model_id=model_id,
                adapter=oai,
                aliases=aliases,
                task_profile=standard_profile_for(model_id),
            )
    if openrouter_key:
        or_adapter = OpenRouterAdapter(
            api_key=openrouter_key,
            app_name="metis-gateway",
            http_referer="https://metis.local",
        )
        adapters.append(or_adapter)
        try:
            catalog = await or_adapter.fetch_catalog()
        except Exception as exc:
            print(
                f"warning: OpenRouter catalog fetch failed ({exc}); "
                "OpenRouter models will not be available.",
                file=sys.stderr,
            )
        else:
            for model_id in sorted(catalog.capabilities.keys()):
                registry.register(
                    model_id=model_id,
                    adapter=or_adapter,
                    aliases=[],
                    task_profile=standard_profile_for(model_id),
                )
            if catalog.pricing:
                pricing_table = pricing_table.with_overlay(
                    overlay_version=catalog.version,
                    overlay_models=catalog.pricing,
                )

    if global_default_model not in registry:
        fallback = registry.list_models()[0] if registry.list_models() else None
        if fallback:
            print(
                f"note: global default {global_default_model!r} not configured; "
                f"using {fallback!r}.",
                file=sys.stderr,
            )
            global_default_model = fallback

    routing = RoutingEngine(
        registry=registry,
        bus=bus,
        policy=EMPTY_POLICY,
    )

    quota_tracker = QuotaTracker(db_file)

    return GatewayRuntime(
        bus=bus,
        trace=trace,
        registry=registry,
        routing=routing,
        pricing=pricing_table,
        keystore=keystore,
        adapters=adapters,
        db_file=db_file,
        global_default_model=global_default_model,
        quota_tracker=quota_tracker,
    )


async def shutdown_gateway_runtime(runtime: GatewayRuntime) -> None:
    try:
        await runtime.bus.drain()
    except Exception:
        pass
    try:
        await runtime.bus.stop()
    except Exception:
        pass
    for adapter in runtime.adapters:
        try:
            await adapter.close()
        except Exception:
            pass
    try:
        runtime.trace.close()
    except Exception:
        pass
    if runtime.quota_tracker is not None:
        try:
            runtime.quota_tracker.close()
        except Exception:
            pass


def default_db_path() -> Path:
    return Path.home() / ".metis" / "metis.db"


def default_keystore_path() -> Path:
    return Path.home() / ".metis" / "gateway" / "keys.json"
