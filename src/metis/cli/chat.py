"""Interactive REPL.

Minimal Phase 1 chat surface: stdin → submit_turn → print assistant text.
Slash commands: /model, /cost, /help. EOF / 'exit' / 'quit' terminates.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from decimal import Decimal
from pathlib import Path

from metis.adapters.anthropic import AnthropicAdapter
from metis.adapters.errors import AdapterError
from metis.events.bus import EventBus
from metis.pricing import DEFAULT_PRICE_TABLE
from metis.routing import ModelRegistry, RoutingEngine
from metis.routing.engine import RoutingError
from metis.sessions import InMemorySessionStore, SessionManager, UnknownAliasError
from metis.tools.builtins import register_builtins
from metis.tools.dispatcher import ToolDispatcher
from metis.trace.store import TraceStore

logger = logging.getLogger(__name__)


ANTHROPIC_MODELS = {
    "anthropic:claude-opus-4-7": ["opus", "deep"],
    "anthropic:claude-sonnet-4-6": ["sonnet", "balanced"],
    "anthropic:claude-haiku-4-5": ["haiku", "fast"],
}


async def run_chat(
    *,
    workspace_path: str,
    initial_model: str | None,
    db_path: str | None,
    global_default_model: str,
) -> int:
    workspace = Path(workspace_path).expanduser().resolve()
    if not workspace.is_dir():
        print(f"error: workspace {workspace} is not a directory", file=sys.stderr)
        return 1

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "error: ANTHROPIC_API_KEY environment variable is not set.",
            file=sys.stderr,
        )
        return 1

    # ---- Wire up the server side -----------------------------------------

    bus = EventBus()
    bus.start()

    db_file = Path(db_path).expanduser() if db_path else _default_db_path()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    trace = TraceStore(db_file)
    trace.attach_to(bus)

    adapter = AnthropicAdapter(api_key=api_key)
    registry = ModelRegistry()
    for model_id, aliases in ANTHROPIC_MODELS.items():
        registry.register(model_id=model_id, adapter=adapter, aliases=aliases)

    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)
    register_builtins(dispatcher)

    manager = SessionManager(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=InMemorySessionStore(),
        pricing=DEFAULT_PRICE_TABLE,
        global_default_model=global_default_model,
    )

    # Resolve initial model (alias accepted).
    resolved_initial = None
    if initial_model is not None:
        resolved_initial = registry.resolve_alias(initial_model)
        if resolved_initial is None:
            print(
                f"error: unknown model {initial_model!r}. "
                f"Known: {', '.join(sorted(registry.list_models()))}",
                file=sys.stderr,
            )
            await _shutdown(bus, adapter, trace)
            return 1

    session = manager.create_session(workspace_path=str(workspace), active_model=resolved_initial)

    # ---- Banner ----------------------------------------------------------

    print(f"Metis chat • workspace: {workspace}")
    print(f"Session: {session.id}")
    print(f"Active model: {session.active_model or '(default)'} • trace: {db_file}")
    print(
        "Type your message. Commands: /model <id>, /model -, /cost, /help. Ctrl-D or 'exit' to quit."
    )
    print()

    # ---- REPL ------------------------------------------------------------

    exit_code = 0
    try:
        while True:
            try:
                line = await _async_input("> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            text = line.strip()
            if not text:
                continue
            if text in ("exit", "quit"):
                break
            if text.startswith("/"):
                handled = await _handle_slash(text, manager, session, registry)
                if handled == "quit":
                    break
                continue
            try:
                result = await manager.submit_turn(session.id, text)
            except UnknownAliasError as exc:
                print(f"unknown alias: @{exc.alias}", file=sys.stderr)
                continue
            except RoutingError as exc:
                print(f"routing failed: {exc}", file=sys.stderr)
                continue
            except AdapterError as exc:
                print(
                    f"adapter error [{exc.error_class.value}]: {exc.message}",
                    file=sys.stderr,
                )
                continue
            _print_result(result)
    except Exception:
        logger.exception("unhandled error in chat loop")
        exit_code = 1
    finally:
        await _shutdown(bus, adapter, trace)
    return exit_code


# ---- REPL helpers ----------------------------------------------------------


async def _async_input(prompt: str) -> str:
    """input() in a thread so we don't block the event loop.

    The event loop matters: bus dispatch and adapter timers all live there.
    """
    return await asyncio.to_thread(input, prompt)


async def _handle_slash(
    text: str,
    manager: SessionManager,
    session,
    registry: ModelRegistry,
) -> str | None:
    """Returns 'quit' if the command should terminate the REPL; else None."""
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/help", "/?"):
        print(
            "Commands:\n"
            "  /model <alias|id>   set sticky model\n"
            "  /model -            clear sticky (use defaults)\n"
            "  /model show         print current sticky\n"
            "  /cost               session cost so far\n"
            "  /models             list configured models\n"
            "  /help, /?           this list\n"
            "  exit, quit, ^D      leave"
        )
        return None
    if cmd == "/model":
        if not arg or arg == "show":
            print(f"sticky: {session.active_model or '(none — using defaults)'}")
            return None
        if arg == "-":
            manager.set_active_model(session.id, None)
            print("sticky cleared")
            return None
        try:
            manager.set_active_model(session.id, arg)
            print(f"sticky: {session.active_model}")
        except UnknownAliasError as exc:
            print(f"unknown model: {exc.alias}", file=sys.stderr)
        return None
    if cmd == "/models":
        for model_id in registry.list_models():
            entry = registry.get(model_id)
            aliases = ", ".join(entry.aliases) or "—"
            print(f"  {model_id}  (aliases: {aliases})")
        return None
    if cmd == "/cost":
        print(f"session cost so far: ${session.cost_so_far_usd:.4f} ({session.turn_count} turns)")
        return None
    print(f"unknown command: {cmd}. /help for the list.", file=sys.stderr)
    return None


def _print_result(result) -> None:
    cost = f"${result.cost_usd:.4f}" if result.cost_usd >= Decimal("0.0001") else "<$0.0001"
    tag = (
        f"[{result.chosen_model} • {cost} • "
        f"{result.llm_call_count} LLM / {result.tool_call_count} tool]"
    )
    print()
    print(tag)
    print(result.assistant_text)
    print()


async def _shutdown(bus: EventBus, adapter: AnthropicAdapter, trace: TraceStore) -> None:
    try:
        await bus.drain()
    except Exception:
        pass
    try:
        await bus.stop()
    except Exception:
        pass
    try:
        await adapter.close()
    except Exception:
        pass
    try:
        trace.close()
    except Exception:
        pass


def _default_db_path() -> Path:
    return Path.home() / ".metis" / "trace.db"
