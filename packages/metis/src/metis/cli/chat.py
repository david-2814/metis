"""Interactive line-based REPL (`metis dev`, alias `metis chat`).

For the richer Textual TUI, see `metis tui` (src/metis/tui/app.py).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import yaml

from metis.cli.models_display import (
    format_models_lines,
    parse_models_command,
    resolve_models,
    truncation_hint,
)
from metis.cli.runtime import ChatRuntime, SetupError, setup_runtime, shutdown_runtime
from metis.core.adapters.errors import AdapterError
from metis.core.adapters.streaming import (
    MessageStart,
    StreamingEvent,
    TextDelta,
    ToolUseStart,
)
from metis.core.routing import ModelRegistry
from metis.core.routing.engine import RoutingError
from metis.core.routing.policy import LLMRouterConfig
from metis.core.sessions import (
    AmbiguousModelError,
    SessionManager,
    UnknownAliasError,
    UserExplicitModelRejectedError,
)

logger = logging.getLogger(__name__)


async def run_chat(
    *,
    workspace_path: str,
    initial_model: str | None,
    db_path: str | None,
    global_default_model: str,
) -> int:
    try:
        runtime = await setup_runtime(
            workspace_path=workspace_path,
            db_path=db_path,
            global_default_model=global_default_model,
        )
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    registry = runtime.registry
    manager = runtime.manager

    # Resolve initial model (alias accepted).
    resolved_initial = None
    if initial_model is not None:
        resolved_initial = registry.resolve_alias(initial_model)
        if resolved_initial is None:
            print(
                f"error: unknown model {initial_model!r}. "
                f"Configured: {', '.join(sorted(registry.list_models()))}",
                file=sys.stderr,
            )
            await shutdown_runtime(runtime)
            return 1

    workspace = Path(workspace_path).expanduser().resolve()
    session = manager.create_session(workspace_path=str(workspace), active_model=resolved_initial)

    # ---- Banner ----------------------------------------------------------

    providers = sorted({registry.provider_of(m) for m in registry.list_models()})
    print(f"Metis chat • workspace: {workspace}")
    print(f"Session: {session.id}")
    print(f"Providers: {', '.join(providers)}")
    print(f"Active model: {session.active_model or f'(default: {runtime.global_default_model})'}")
    print(f"Trace: {runtime.db_file}")
    print(
        "Type your message. Commands: /model <id>, /model -, /cost, /models, /help. "
        "Ctrl-D or 'exit' to quit."
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
                handled = await _handle_slash(text, manager, session, registry, runtime)
                if handled == "quit":
                    break
                continue
            renderer = _LiveRenderer()
            try:
                result = await manager.submit_turn(
                    session.id, text, on_streaming_event=renderer.handle
                )
            except UnknownAliasError as exc:
                print(f"unknown alias: @{exc.alias}", file=sys.stderr)
                continue
            except UserExplicitModelRejectedError as exc:
                print(str(exc), file=sys.stderr)
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
            renderer.finalize()
            _print_result_tag(result)
    except Exception:
        logger.exception("unhandled error in chat loop")
        exit_code = 1
    finally:
        await shutdown_runtime(runtime)
    return exit_code


# ---- REPL helpers ----------------------------------------------------------


async def _async_input(prompt: str) -> str:
    """Read a line from stdin in a daemon thread.

    `asyncio.to_thread` uses the default ThreadPoolExecutor, whose worker
    threads are joined at interpreter shutdown — that hangs forever because
    `input()` is blocked on stdin. A daemon thread is terminated at process
    exit without being joined, which keeps Ctrl-C clean.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def reader() -> None:
        try:
            line = input(prompt)
        except EOFError:
            loop.call_soon_threadsafe(_set_exc_if_pending, future, EOFError())
        except BaseException as exc:
            loop.call_soon_threadsafe(_set_exc_if_pending, future, exc)
        else:
            loop.call_soon_threadsafe(_set_result_if_pending, future, line)

    threading.Thread(target=reader, daemon=True, name="metis-stdin").start()
    return await future


def _set_result_if_pending(future: asyncio.Future, value: object) -> None:
    if not future.done():
        future.set_result(value)


def _set_exc_if_pending(future: asyncio.Future, exc: BaseException) -> None:
    if not future.done():
        future.set_exception(exc)


async def _handle_slash(
    text: str,
    manager: SessionManager,
    session,
    registry: ModelRegistry,
    runtime,
) -> str | None:
    pricing = runtime.pricing
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/help", "/?"):
        print(
            "Commands:\n"
            "  /model <alias|id>     set the Active model for this session\n"
            "  /model -              clear the Active model (use defaults)\n"
            "  /model show           print the current Active model\n"
            "  /models               list primary (latest) models\n"
            "  /models all           list every registered model\n"
            "  /models <pattern>     filter by substring (e.g. /models opus)\n"
            "  /router llm on|off    enable/disable the LLM_ROUTER slot (routing-engine §4.6)\n"
            "  /router llm model <id>   set the router model id\n"
            "  /router llm status    print the current llm_router config + spend\n"
            "  /share                include the last slash output in the next message\n"
            "  /cost                 session cost so far\n"
            "  /help, /?             this list\n"
            "  exit, quit, ^D        leave"
        )
        return None
    if cmd == "/router":
        _handle_router_subcommand(arg, session, runtime)
        return None
    if cmd == "/model":
        if not arg or arg == "show":
            # Re-fetch so the display matches what `submit_turn` will see.
            current = manager.get_session(session.id).active_model
            print(f"Active model: {current or '(none — using defaults)'}")
            return None
        if arg == "-":
            manager.set_active_model(session.id, None)
            print("Active model cleared")
            return None
        try:
            resolved = manager.set_active_model(session.id, arg)
            if resolved != arg:
                # Suggest matched a longer canonical id; show the user what
                # we settled on so the resolution is transparent.
                print(f"Active model: {resolved}   (matched from {arg!r})")
            else:
                print(f"Active model: {resolved}")
        except AmbiguousModelError as exc:
            print(f"ambiguous model: {exc.input}", file=sys.stderr)
            print("  did you mean one of:", file=sys.stderr)
            for candidate in exc.candidates:
                print(f"    {candidate}", file=sys.stderr)
        except UnknownAliasError as exc:
            print(f"unknown model: {exc.alias}", file=sys.stderr)
        return None
    if cmd == "/models":
        mode, pattern = parse_models_command(arg)
        displayed, total = resolve_models(
            registry=registry,
            mode=mode,
            pattern=pattern,
            always_include=session.active_model,
        )
        rendered = format_models_lines(
            displayed,
            registry=registry,
            pricing=pricing,
            sticky_model=session.active_model,
        )
        for line in rendered:
            print(line)
        hint = truncation_hint(displayed, total, mode=mode, pattern=pattern)
        if hint:
            print(hint)
            rendered.append(hint)
        # Capture for /share so the agent can see this if the user asks.
        manager.buffer_slash_output(session.id, "\n".join(rendered))
        return None
    if cmd == "/share":
        buffered = manager.mark_share_pending(session.id)
        if buffered is None:
            print(
                "Nothing to share yet — run /models (or another slash command) first.",
                file=sys.stderr,
            )
        else:
            line_count = buffered.count("\n") + 1
            print(
                f"Will share {line_count} line(s) of recent slash output "
                f"with the agent on your next message."
            )
        return None
    if cmd == "/cost":
        print(f"session cost so far: ${session.cost_so_far_usd:.4f} ({session.turn_count} turns)")
        return None
    print(f"unknown command: {cmd}. /help for the list.", file=sys.stderr)
    return None


def _handle_router_subcommand(arg: str, session, runtime) -> None:
    """Backs the `/router llm ...` family (routing-engine §9.1).

    Mutates the in-memory `RoutingPolicy` (effective on the next turn) and
    persists the change back to the active routing.yaml path.
    """
    parts = arg.split(maxsplit=2) if arg else []
    if not parts or parts[0] != "llm":
        print("usage: /router llm on|off|model <id>|status", file=sys.stderr)
        return
    if len(parts) == 1:
        print("usage: /router llm on|off|model <id>|status", file=sys.stderr)
        return
    sub = parts[1].lower()
    rest = parts[2] if len(parts) > 2 else ""

    routing = runtime.routing
    current_cfg = routing.policy.llm_router
    if sub == "status":
        budget = runtime.manager._llm_router.budget if runtime.manager._llm_router else None
        print(
            "llm_router status:\n"
            f"  enabled:                 {current_cfg.enabled}\n"
            f"  model:                   {current_cfg.model}\n"
            f"  per_session_budget_usd:  ${current_cfg.per_session_budget_usd:.4f}\n"
            f"  per_day_budget_usd:      ${current_cfg.per_day_budget_usd:.4f}\n"
            f"  timeout_seconds:         {current_cfg.timeout_seconds}\n"
            f"  session spend so far:    "
            f"${(budget.session_spend(session.id) if budget else 0):.4f}\n"
            f"  daily spend so far:      ${(budget.daily_spend() if budget else 0):.4f}\n"
            f"  source:                  {routing.policy.source_path or '(in-memory)'}"
        )
        return
    if sub == "on":
        new_cfg = replace(current_cfg, enabled=True)
    elif sub == "off":
        new_cfg = replace(current_cfg, enabled=False)
    elif sub == "model":
        if not rest:
            print("usage: /router llm model <id>", file=sys.stderr)
            return
        new_cfg = replace(current_cfg, model=rest)
    else:
        print(
            f"unknown /router llm subcommand: {sub} (use: on, off, model <id>, status)",
            file=sys.stderr,
        )
        return

    new_policy = replace(routing.policy, llm_router=new_cfg)
    routing.set_policy(new_policy)
    # Hot-swap the runtime LLMRouter so the live config matches the new policy.
    if runtime.manager._llm_router is not None:
        runtime.manager._llm_router._config = new_cfg

    persisted_to = _persist_llm_router_config(new_cfg, routing.policy.source_path, session)
    if persisted_to is not None:
        print(
            f"llm_router updated: enabled={new_cfg.enabled} model={new_cfg.model} "
            f"(persisted to {persisted_to})"
        )
    else:
        print(
            f"llm_router updated: enabled={new_cfg.enabled} model={new_cfg.model} "
            "(in-memory only; could not persist to routing.yaml)"
        )


def _persist_llm_router_config(
    cfg: LLMRouterConfig,
    source_path: str | None,
    session,
) -> Path | None:
    """Write the new `llm_router:` block back to routing.yaml.

    Prefers the policy's source path (the file we loaded the policy from);
    falls back to `<workspace>/.metis/routing.yaml` if the session has a
    workspace, else `~/.metis/routing.yaml`.

    Atomic write-temp-then-rename. Returns the path written or None on
    persistence failure (the in-memory swap still took effect).
    """
    target: Path | None = None
    if source_path:
        target = Path(source_path)
    elif session.workspace_path:
        target = Path(session.workspace_path) / ".metis" / "routing.yaml"
    else:
        target = Path.home() / ".metis" / "routing.yaml"
    try:
        existing: dict = {}
        if target.exists():
            text = target.read_text(encoding="utf-8")
            parsed = yaml.safe_load(text) if text.strip() else {}
            if isinstance(parsed, dict):
                existing = parsed
        existing["llm_router"] = {
            "enabled": cfg.enabled,
            "model": cfg.model,
            "per_session_budget_usd": cfg.per_session_budget_usd,
            "per_day_budget_usd": cfg.per_day_budget_usd,
            "timeout_seconds": cfg.timeout_seconds,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")
        tmp.replace(target)
        return target
    except OSError as exc:
        logger.warning("could not persist llm_router config to %s: %s", target, exc)
        return None


class _LiveRenderer:
    def __init__(self) -> None:
        self._in_text = False
        self._messages_started = 0

    def handle(self, event: StreamingEvent) -> None:
        if isinstance(event, MessageStart):
            self._messages_started += 1
            if self._messages_started > 1:
                sys.stdout.write("\n")
                sys.stdout.flush()
            self._in_text = False
        elif isinstance(event, TextDelta):
            if not self._in_text:
                sys.stdout.write("\n")
                self._in_text = True
            sys.stdout.write(event.text)
            sys.stdout.flush()
        elif isinstance(event, ToolUseStart):
            if self._in_text:
                sys.stdout.write("\n")
            sys.stdout.write(f"\n[{event.tool_name}(...)]")
            sys.stdout.flush()
            self._in_text = False

    def finalize(self) -> None:
        sys.stdout.write("\n")
        sys.stdout.flush()


def _print_result_tag(result) -> None:
    cost = f"${result.cost_usd:.4f}" if result.cost_usd >= Decimal("0.0001") else "<$0.0001"
    tag = (
        f"[{result.chosen_model} • {cost} • "
        f"{result.llm_call_count} LLM / {result.tool_call_count} tool]"
    )
    print(tag)
    print()


# Backwards-compatibility export — pyproject "metis = metis.cli.main:main".
__all__ = ["run_chat"]
_ = ChatRuntime  # re-export indirectly
