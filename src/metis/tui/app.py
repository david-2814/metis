"""Textual app for Metis.

A minimal but real TUI:
- Header shows the active model and running session cost.
- A scrollable RichLog renders the conversation. Text deltas append live;
  tool calls get inline `[tool(...)]` markers.
- An Input at the bottom takes user messages. Empty submit, `exit`/`quit`,
  and slash commands all work as in the REPL.
- A status footer shows session metadata and key bindings.

The TUI shares its setup (registry, routing, dispatcher, session manager)
with the REPL via `cli/runtime.py` — no duplication.
"""

from __future__ import annotations

import logging
import sys
from decimal import Decimal
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import Footer, Header, Input, RichLog

from metis.adapters.errors import AdapterError
from metis.adapters.streaming import (
    MessageStart,
    StreamingEvent,
    TextDelta,
    ToolUseStart,
)
from metis.cli.runtime import ChatRuntime, SetupError, setup_runtime, shutdown_runtime
from metis.routing.engine import RoutingError
from metis.sessions import UnknownAliasError
from metis.sessions.store import Session

logger = logging.getLogger(__name__)


class MetisApp(App):
    """Textual app driving a Metis chat session.

    Lifecycle:
    - Constructor receives a fully-wired ChatRuntime + Session (built by
      `setup_runtime`). Tests can construct the app directly with their own
      fakes.
    - `on_mount` displays the welcome banner.
    - User input drives `_handle_turn` workers that stream events back to
      the log.
    """

    CSS = """
    Screen {
        layout: vertical;
    }
    #log {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    #prompt {
        border: round $accent;
        margin: 0 0 0 0;
    }
    """

    BINDINGS = [  # noqa: RUF012 — Textual reads this class attribute by convention
        Binding("ctrl+c", "quit_app", "Quit"),
        Binding("ctrl+d", "quit_app", "Quit"),
    ]

    cost_so_far = reactive(0.0)

    def __init__(self, runtime: ChatRuntime, session: Session) -> None:
        super().__init__()
        self.runtime = runtime
        self.session = session

    # ---- Layout -------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            yield RichLog(id="log", markup=True, wrap=True, auto_scroll=True)
            yield Input(id="prompt", placeholder="Type your message (Ctrl-D to quit)...")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"Metis • {self.session.active_model or self.runtime.global_default_model}"
        self.sub_title = (
            f"workspace: {self.session.workspace_path} • session: {self.session.id[:14]}…"
        )
        log = self.query_one(RichLog)
        log.write("[bold]Metis chat[/]")
        providers = sorted(
            {self.runtime.registry.provider_of(m) for m in self.runtime.registry.list_models()}
        )
        log.write(f"[dim]Providers: {', '.join(providers)}[/]")
        log.write(
            f"[dim]Active model: {self.session.active_model or f'(default: {self.runtime.global_default_model})'}[/]"
        )
        log.write(
            "[dim]Commands: /model <alias|id>, /model -, /cost, /models, /help. Ctrl-D to quit.[/]"
        )
        log.write("")
        self.query_one(Input).focus()
        self.cost_so_far = float(self.session.cost_so_far_usd)

    def watch_cost_so_far(self, cost: float) -> None:
        self.sub_title = (
            f"session: {self.session.id[:14]}… • "
            f"cost: ${cost:.4f} • turns: {self.session.turn_count}"
        )

    # ---- Input handling ----------------------------------------------

    async def on_input_submitted(self, message: Input.Submitted) -> None:
        text = message.value.strip()
        # Always clear the input box.
        self.query_one(Input).value = ""
        if not text:
            return
        if text in ("exit", "quit"):
            self.exit()
            return
        if text.startswith("/"):
            self._handle_slash(text)
            return
        self._submit_turn(text)

    def _handle_slash(self, text: str) -> None:
        log = self.query_one(RichLog)
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/help", "/?"):
            log.write("[bold]Commands[/]:")
            log.write("  /model <alias|id>   set sticky model")
            log.write("  /model -            clear sticky (use defaults)")
            log.write("  /model show         print current sticky")
            log.write("  /cost               session cost so far")
            log.write("  /models             list configured models")
            log.write("  /help, /?           this list")
            log.write("  exit, quit, ^D      leave")
            return
        if cmd == "/model":
            if not arg or arg == "show":
                log.write(f"sticky: {self.session.active_model or '(none — using defaults)'}")
                return
            if arg == "-":
                self.runtime.manager.set_active_model(self.session.id, None)
                log.write("sticky cleared")
                self._refresh_title()
                return
            try:
                self.runtime.manager.set_active_model(self.session.id, arg)
                log.write(f"sticky: {self.session.active_model}")
                self._refresh_title()
            except UnknownAliasError as exc:
                log.write(f"[red]unknown model: {exc.alias}[/]")
            return
        if cmd == "/models":
            for model_id in self.runtime.registry.list_models():
                entry = self.runtime.registry.get(model_id)
                aliases = ", ".join(entry.aliases) or "—"
                log.write(f"  {model_id}  (aliases: {aliases})")
            return
        if cmd == "/cost":
            log.write(
                f"session cost so far: ${self.session.cost_so_far_usd:.4f} "
                f"({self.session.turn_count} turns)"
            )
            return
        log.write(f"[red]unknown command: {cmd}. /help for the list.[/]")

    def _refresh_title(self) -> None:
        self.title = f"Metis • {self.session.active_model or self.runtime.global_default_model}"

    # ---- Turn worker --------------------------------------------------

    @work(exclusive=True)
    async def _submit_turn(self, text: str) -> None:
        log = self.query_one(RichLog)
        log.write(f"[bold cyan]>[/] {text}")
        renderer = _TUIRenderer(log)
        try:
            result = await self.runtime.manager.submit_turn(
                self.session.id, text, on_streaming_event=renderer.handle
            )
        except UnknownAliasError as exc:
            log.write(f"[red]unknown alias: @{exc.alias}[/]")
            return
        except RoutingError as exc:
            log.write(f"[red]routing failed: {exc}[/]")
            return
        except AdapterError as exc:
            log.write(f"[red]adapter error [{exc.error_class.value}]: {exc.message}[/]")
            return
        except Exception as exc:
            logger.exception("unhandled error in turn worker")
            log.write(f"[red]error: {exc}[/]")
            return

        renderer.finalize()
        cost = f"${result.cost_usd:.4f}" if result.cost_usd >= Decimal("0.0001") else "<$0.0001"
        log.write(
            f"[dim][{result.chosen_model} • {cost} • "
            f"{result.llm_call_count} LLM / {result.tool_call_count} tool][/]"
        )
        log.write("")
        # Refresh reactive state from the persisted session.
        fresh = self.runtime.session_store.get_session(self.session.id)
        self.session.cost_so_far_usd = fresh.cost_so_far_usd
        self.session.turn_count = fresh.turn_count
        self.cost_so_far = float(fresh.cost_so_far_usd)

    # ---- Quit ---------------------------------------------------------

    async def action_quit_app(self) -> None:
        self.exit()


# ---------------------------------------------------------------------------
# Streaming renderer
# ---------------------------------------------------------------------------


class _TUIRenderer:
    """Buffers TextDeltas and flushes them to RichLog on newlines or markers.

    RichLog can only append whole lines, so we batch text deltas into chunks
    that get written on newline boundaries or when a non-text event (tool
    call marker, end of message) arrives. The result feels like live
    streaming for multi-sentence responses while playing well with RichLog's
    append-only nature.
    """

    def __init__(self, log: RichLog) -> None:
        self._log = log
        self._buffer: list[str] = []
        self._messages_started = 0
        self._in_text = False

    def handle(self, event: StreamingEvent) -> None:
        if isinstance(event, MessageStart):
            self._flush()
            self._messages_started += 1
            self._in_text = False
        elif isinstance(event, TextDelta):
            self._in_text = True
            self._buffer.append(event.text)
            # Flush on newline so the user sees text appear paragraph-by-paragraph.
            if "\n" in event.text:
                self._flush()
        elif isinstance(event, ToolUseStart):
            self._flush()
            # Use an arrow rather than `[...]` brackets: with `markup=True`
            # RichLog interprets brackets as Rich style tags, which would
            # eat the tool name.
            self._log.write(f"[yellow]→ {event.tool_name}(...)[/]")
            self._in_text = False

    def finalize(self) -> None:
        self._flush()

    def _flush(self) -> None:
        if not self._buffer:
            return
        text = "".join(self._buffer).strip("\n")
        if text:
            self._log.write(text)
        self._buffer.clear()


# ---------------------------------------------------------------------------
# Entry point used by `metis tui`
# ---------------------------------------------------------------------------


async def run_tui(
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

    resolved_initial = None
    if initial_model is not None:
        resolved_initial = runtime.registry.resolve_alias(initial_model)
        if resolved_initial is None:
            print(
                f"error: unknown model {initial_model!r}. "
                f"Configured: {', '.join(sorted(runtime.registry.list_models()))}",
                file=sys.stderr,
            )
            await shutdown_runtime(runtime)
            return 1

    workspace = str(Path(workspace_path).expanduser().resolve())
    session = runtime.manager.create_session(
        workspace_path=workspace, active_model=resolved_initial
    )

    app = MetisApp(runtime=runtime, session=session)
    try:
        await app.run_async()
    finally:
        await shutdown_runtime(runtime)
    return 0
