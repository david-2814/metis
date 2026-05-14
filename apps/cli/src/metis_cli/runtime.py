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

from metis_core.adapters.anthropic import AnthropicAdapter
from metis_core.adapters.openai import OpenAIAdapter
from metis_core.adapters.openrouter import OpenRouterAdapter
from metis_core.adapters.protocol import ProviderAdapter
from metis_core.eval import Evaluator, register_evaluator
from metis_core.events.bus import EventBus
from metis_core.events.envelope import Actor
from metis_core.events.payloads import RoutingPolicyInvalid, make_event
from metis_core.memory import MemoryStore, register_memory_tools
from metis_core.patterns import PatternEventSubscriber, PatternStore
from metis_core.patterns.fingerprint import FingerprintInputs
from metis_core.pricing import DEFAULT_PRICE_TABLE, PriceTable
from metis_core.routing import (
    EMPTY_POLICY,
    ModelRegistry,
    PolicyValidationError,
    RoutingEngine,
    RoutingPolicy,
    load_policy_file,
    standard_profile_for,
)
from metis_core.sessions import (
    SessionManager,
    SqliteSessionStore,
)
from metis_core.sessions.store import SessionStore
from metis_core.skills import SkillStore, load_skills, register_skill_tools
from metis_core.tools.builtins import register_builtins
from metis_core.tools.dispatcher import ToolDispatcher
from metis_core.trace.store import TraceStore

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
    evaluator: Evaluator | None = None
    pattern_subscriber: PatternEventSubscriber | None = None
    pattern_stores: dict[str, PatternStore] | None = None


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
    _configure_file_logging()

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
                aliases = _pick_auto_alias(model_id, registry)
                # OpenRouter models start with no curated profile — customers
                # tag them via routing rules. Known mirrors of native models
                # (e.g. openrouter:anthropic/claude-opus-*) intentionally do
                # NOT inherit the native profile here; the curated list is
                # explicit, not pattern-matched.
                registry.register(
                    model_id=model_id,
                    adapter=or_adapter,
                    aliases=aliases,
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

    policy = _load_routing_policy(
        explicit_path=routing_policy_path,
        registry=registry,
        bus=bus,
    )

    pattern_stores: dict[str, PatternStore] = {}

    def _pattern_store_resolver(workspace_path: str) -> PatternStore | None:
        try:
            existing = pattern_stores.get(workspace_path)
            if existing is not None:
                return existing
            store = PatternStore(workspace_path)
            pattern_stores[workspace_path] = store
            return store
        except Exception:
            logger.exception("pattern store resolver failed for %s", workspace_path)
            return None

    def _routing_fingerprint_inputs(ctx) -> FingerprintInputs:
        # Mirror the pattern subscriber's default_fingerprint_builder so the
        # query-side fingerprint matches what record() persists. The raw user
        # message text isn't on the bus (only its hash) so the recording side
        # leaves user_message_text="" and intent_tags empty; we do the same
        # at query time to keep structural signatures aligned end-to-end.
        return FingerprintInputs(
            user_message_text="",
            workspace_path=ctx.workspace_path,
            estimated_input_tokens=ctx.estimated_input_tokens,
            has_images=ctx.has_images,
            has_tool_calls_in_history=ctx.has_tool_calls_in_history,
        )

    routing = RoutingEngine(
        registry=registry,
        bus=bus,
        policy=policy,
        pattern_store_resolver=_pattern_store_resolver,
        fingerprint_inputs_builder=_routing_fingerprint_inputs,
    )
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

    evaluator, _ = register_evaluator(bus, trace)

    def _workspace_resolver(session_id: str) -> str | None:
        try:
            return session_store.get_session(session_id).workspace_path
        except Exception:
            return None

    pattern_subscriber = PatternEventSubscriber(
        store_factory=lambda ws: pattern_stores.setdefault(ws, PatternStore(ws)),
        workspace_resolver=_workspace_resolver,
        bus=bus,
    )
    pattern_subscriber.attach()

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
        evaluator=evaluator,
        pattern_subscriber=pattern_subscriber,
        pattern_stores=pattern_stores,
    )


async def shutdown_runtime(runtime: ChatRuntime) -> None:
    if runtime.evaluator is not None:
        try:
            runtime.evaluator.unregister()
        except Exception:
            pass
    if runtime.pattern_subscriber is not None:
        try:
            runtime.pattern_subscriber.detach()
        except Exception:
            pass
    try:
        await runtime.bus.drain()
    except Exception:
        pass
    try:
        await runtime.bus.stop()
    except Exception:
        pass
    if runtime.pattern_stores is not None:
        for store in runtime.pattern_stores.values():
            try:
                store.close()
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


def _auto_alias_candidates(model_id: str) -> list[str]:
    """Generate short alias candidates derived from a canonical model id.

    `openrouter:upstage/solar-pro-3` → ['solar-pro-3', 'upstage-solar-pro-3']
    `anthropic:claude-sonnet-4-6`   → ['claude-sonnet-4-6']
    `local-only-id`                 → []  (no `provider:` prefix → no candidates)

    The trailing path is the most-ergonomic alias; the `provider-tail` form is
    the fallback for collisions across providers that ship the same model name
    (e.g. `openrouter:anthropic/claude-...` collides with the directly-registered
    `anthropic:claude-...`). Forward slashes are flattened to hyphens so the
    alias works as a single shell token.
    """
    if ":" not in model_id:
        return []
    _, tail = model_id.split(":", 1)
    if "/" in tail:
        provider, name = tail.split("/", 1)
        short = name.replace("/", "-")
        return [short, f"{provider}-{short}"]
    return [tail]


def _pick_auto_alias(model_id: str, registry: ModelRegistry) -> list[str]:
    """Return the first non-colliding alias candidate, or `[]` if all are taken.

    Collision check uses the registry's own alias index (which considers model
    ids as their own aliases), so a candidate that matches an existing model id
    is correctly rejected.
    """
    for candidate in _auto_alias_candidates(model_id):
        if registry.resolve_alias(candidate) is None:
            return [candidate]
    return []


_FILE_LOGGING_CONFIGURED = False


def _configure_file_logging() -> None:
    """Attach a file handler to the `metis` logger.

    Adapter errors, dispatch warnings, and other diagnostics flow through
    Python's stdlib logging — but without a handler the records get dropped.
    This attaches a FileHandler so users have a place to grep when things
    go wrong (especially upstream provider rejections that compose into a
    one-line user error but carry a full body in the log).

    Configuration:

    - `METIS_LOG_FILE=` (empty) → disable file logging entirely.
    - `METIS_LOG_FILE=/path/to/file` → log there.
    - unset → default to `/tmp/metis.log` on Unix-like systems.

    Idempotent — calling multiple times only adds one handler.
    """
    global _FILE_LOGGING_CONFIGURED
    if _FILE_LOGGING_CONFIGURED:
        return

    raw = os.environ.get("METIS_LOG_FILE")
    if raw is None:
        path: Path | None = Path("/tmp/metis.log")
    elif raw == "":
        path = None
    else:
        path = Path(raw).expanduser()

    if path is None:
        _FILE_LOGGING_CONFIGURED = True
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    except OSError as exc:
        # If the configured path isn't writable, fall through silently
        # rather than blow up startup. The error itself goes to stderr.
        print(f"warning: could not open log file {path}: {exc}", file=sys.stderr)
        _FILE_LOGGING_CONFIGURED = True
        return

    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    # Attach to every Metis logger root. After the workspace split, modules
    # log under `metis_core.*`, `metis_server.*`, and `metis_cli.*` rather
    # than a single `metis.*` namespace.
    for logger_name in ("metis_core", "metis_server", "metis_cli"):
        metis_logger = logging.getLogger(logger_name)
        metis_logger.addHandler(handler)
        if metis_logger.level == logging.NOTSET or metis_logger.level > logging.INFO:
            metis_logger.setLevel(logging.INFO)
    _FILE_LOGGING_CONFIGURED = True


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
