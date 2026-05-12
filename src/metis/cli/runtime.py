"""Shared setup for both the REPL (`metis chat`) and the Textual TUI (`metis tui`).

Both entry points wire the same components: event bus → trace store →
session store → adapters → routing → tool dispatcher → session manager.
Centralizing this in one place avoids drift between the two.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from metis.adapters.anthropic import AnthropicAdapter
from metis.adapters.openai import OpenAIAdapter
from metis.adapters.openrouter import OpenRouterAdapter
from metis.adapters.protocol import ProviderAdapter
from metis.events.bus import EventBus
from metis.events.envelope import Actor
from metis.events.payloads import RoutingPolicyInvalid, make_event
from metis.memory import MemoryStore, register_memory_tools
from metis.pricing import DEFAULT_PRICE_TABLE, PriceTable
from metis.routing import (
    EMPTY_POLICY,
    ModelRegistry,
    PolicyValidationError,
    RoutingEngine,
    RoutingPolicy,
    load_policy_file,
)
from metis.sessions import (
    SessionManager,
    SqliteSessionStore,
)
from metis.sessions.store import SessionStore
from metis.skills import SkillStore, load_skills, register_skill_tools
from metis.tools.builtins import register_builtins
from metis.tools.dispatcher import ToolDispatcher
from metis.trace.store import TraceStore

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


@dataclass
class ChatRuntime:
    bus: EventBus
    trace: TraceStore
    session_store: SessionStore
    registry: ModelRegistry
    routing: RoutingEngine
    dispatcher: ToolDispatcher
    manager: SessionManager
    adapters: list[ProviderAdapter]
    db_file: Path
    pricing: PriceTable
    global_default_model: str


class SetupError(Exception):
    """Raised when chat runtime setup fails (e.g., missing API keys, bad workspace)."""


async def setup_runtime(
    *,
    workspace_path: str,
    db_path: str | None,
    global_default_model: str,
    routing_policy_path: str | None = None,
) -> ChatRuntime:
    """Build a fully-wired ChatRuntime ready for either the REPL or the TUI.

    `routing_policy_path` may point at a routing.yaml; when omitted, defaults
    to `~/.metis/routing.yaml` (loaded if present, silently skipped if not).
    A malformed policy file is rejected and the engine falls back to the
    empty policy (matching the no-policy behavior); a `routing.policy_invalid`
    event is emitted so the failure is observable.

    Raises SetupError on workspace / API-key problems so callers can render a
    friendly error in their UI of choice.
    """
    workspace = Path(workspace_path).expanduser().resolve()
    if not workspace.is_dir():
        raise SetupError(f"workspace {workspace} is not a directory")

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    if not any((anthropic_key, openai_key, openrouter_key)):
        raise SetupError(
            "set ANTHROPIC_API_KEY, OPENAI_API_KEY, and/or OPENROUTER_API_KEY (in env or .env)"
        )

    bus = EventBus()
    bus.start()

    db_file = Path(db_path).expanduser() if db_path else default_db_path()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    trace = TraceStore(db_file)
    trace.attach_to(bus)
    session_store: SessionStore = SqliteSessionStore(db_file)

    registry = ModelRegistry()
    adapters: list[ProviderAdapter] = []
    pricing_table: PriceTable = DEFAULT_PRICE_TABLE
    if anthropic_key:
        anth = AnthropicAdapter(api_key=anthropic_key)
        adapters.append(anth)
        for model_id, aliases in ANTHROPIC_MODELS.items():
            registry.register(model_id=model_id, adapter=anth, aliases=aliases)
    if openai_key:
        oai = OpenAIAdapter(api_key=openai_key)
        adapters.append(oai)
        for model_id, aliases in OPENAI_MODELS.items():
            registry.register(model_id=model_id, adapter=oai, aliases=aliases)
    if openrouter_key:
        or_adapter = OpenRouterAdapter(
            api_key=openrouter_key, app_name="metis", http_referer="https://metis.local"
        )
        adapters.append(or_adapter)
        try:
            catalog = await or_adapter.fetch_catalog()
        except Exception as exc:
            print(
                f"warning: OpenRouter catalog fetch failed ({exc}); "
                "OpenRouter models will not be available this session.",
                file=sys.stderr,
            )
        else:
            for model_id in sorted(catalog.capabilities.keys()):
                registry.register(model_id=model_id, adapter=or_adapter, aliases=[])
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

    policy = _load_routing_policy(
        explicit_path=routing_policy_path,
        registry=registry,
        bus=bus,
    )
    routing = RoutingEngine(registry=registry, bus=bus, policy=policy)
    dispatcher = ToolDispatcher(bus)
    register_builtins(dispatcher)
    register_memory_tools(dispatcher)
    register_skill_tools(dispatcher)

    global_skills_dir = default_skills_dir()

    def _build_skill_store(workspace_path: str) -> SkillStore:
        workspace_skills = Path(workspace_path).expanduser() / ".metis" / "skills"
        return load_skills(
            global_dir=global_skills_dir if global_skills_dir.is_dir() else None,
            workspace_dir=workspace_skills if workspace_skills.is_dir() else None,
        )

    manager = SessionManager(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=session_store,
        pricing=pricing_table,
        global_default_model=global_default_model,
        memory_factory=lambda ws: MemoryStore(ws),
        skill_store_factory=_build_skill_store,
    )

    return ChatRuntime(
        bus=bus,
        trace=trace,
        session_store=session_store,
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        manager=manager,
        adapters=adapters,
        db_file=db_file,
        pricing=pricing_table,
        global_default_model=global_default_model,
    )


async def shutdown_runtime(runtime: ChatRuntime) -> None:
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
    if hasattr(runtime.session_store, "close"):
        try:
            runtime.session_store.close()
        except Exception:
            pass


def default_db_path() -> Path:
    """Shared SQLite file: events (trace store) + sessions/messages."""
    return Path.home() / ".metis" / "metis.db"


def default_routing_policy_path() -> Path:
    """Routing policy yaml (routing-engine.md §5.1)."""
    return Path.home() / ".metis" / "routing.yaml"


def default_skills_dir() -> Path:
    """Global user-library skills directory (server-api.md §7.3)."""
    return Path.home() / ".metis" / "skills"


def _load_routing_policy(
    *,
    explicit_path: str | None,
    registry: ModelRegistry,
    bus: EventBus,
) -> RoutingPolicy:
    """Load routing.yaml from `explicit_path` or the default location.

    Missing-file at the default path is silent. Missing-file at an explicit
    path is a SetupError. Validation errors emit `routing.policy_invalid`
    and fall back to the empty policy (last-known-good is unavailable for v1
    since we don't persist parsed policy state).
    """
    from datetime import UTC, datetime

    if explicit_path is not None:
        path = Path(explicit_path).expanduser()
        if not path.exists():
            raise SetupError(f"routing policy file not found: {path}")
    else:
        path = default_routing_policy_path()
        if not path.exists():
            return EMPTY_POLICY
    try:
        return load_policy_file(path, registry)
    except PolicyValidationError as exc:
        bus.emit(
            make_event(
                type="routing.policy_invalid",
                session_id="bootstrap",
                actor=Actor.SYSTEM,
                payload=RoutingPolicyInvalid(
                    policy_path=str(path),
                    errors=list(exc.errors),
                    using_last_known_good=False,
                ),
                timestamp=datetime.now(UTC),
            )
        )
        print(
            f"warning: routing policy {path} failed validation:\n  - "
            + "\n  - ".join(exc.errors)
            + "\n  routing will use the empty policy this session.",
            file=sys.stderr,
        )
        return EMPTY_POLICY
