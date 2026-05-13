"""Textual app for Metis.

A minimal but real TUI:
- Header shows the active model and running session cost.
- A scrollable Log renders the conversation in plain text, soft-wrapped
  to the log's current width via `_write_wrapped`. Text deltas append
  live; tool calls get inline `→ tool(...)` markers. Log is used (rather
  than RichLog) so the user can drag-select text natively — RichLog has
  no selection support in Textual 8.x.
- An Input at the bottom takes user messages. Empty submit, `exit`/`quit`,
  and slash commands all work as in the REPL.
- A status footer shows session metadata and key bindings.

The TUI shares its setup (registry, routing, dispatcher, session manager)
with the REPL via `cli/runtime.py` — no duplication.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
from decimal import Decimal
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import Footer, Header, Input, Log

from metis.adapters.errors import AdapterError
from metis.adapters.streaming import (
    MessageStart,
    StreamingEvent,
    TextDelta,
    ToolUseStart,
)
from metis.canonical.content import TextBlock
from metis.canonical.messages import Message, Role
from metis.cli.models_display import (
    format_models_lines,
    parse_models_command,
    resolve_models,
    truncation_hint,
)
from metis.cli.runtime import ChatRuntime, SetupError, setup_runtime, shutdown_runtime
from metis.routing.engine import RoutingError
from metis.sessions import AmbiguousModelError, UnknownAliasError
from metis.sessions.store import Session

logger = logging.getLogger(__name__)


def _is_apple_terminal() -> bool:
    """True when running inside macOS Terminal.app, which silently drops
    OSC 52 *and* intercepts Cmd+C at the menu level — so the standard
    macOS copy shortcut never reaches our process. Used at startup to
    warn the user once and point them at Ctrl+C / `/copy`."""
    return os.environ.get("TERM_PROGRAM") == "Apple_Terminal"


def _extract_message_text(message: Message) -> str:
    """Concatenate TextBlock content from a canonical message.

    Tool-use and thinking blocks are skipped — `/copy` is for grabbing
    the prose reply, which is what users want 99% of the time.
    """
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
    return "".join(parts)


def _write_wrapped(log: Log, text: str, *, subsequent_indent: str = "") -> None:
    """Write text to a Log with soft-wrapping at the widget's content width.

    Log doesn't soft-wrap natively (long lines clip at the viewport), so we
    pre-wrap with `textwrap` against the log's current size. The widget's
    `size.width` is the outer width including the round border (2 cells) and
    `padding: 0 1` (2 cells) defined in the CSS — hence the `- 4`. If the
    widget hasn't been sized yet (e.g. very early in `on_mount`), fall back
    to a conservative 80-column default; subsequent writes pick up the real
    size. Existing lines do not re-wrap on terminal resize — only new writes.

    `subsequent_indent` is prepended to continuation lines. Useful when a
    column-formatted row (like `/models`) wraps and continuation chunks
    should visually align under a meaningful column rather than column 0.
    """
    inner = log.size.width - 4
    width = inner if inner >= 20 else 80
    for paragraph in text.split("\n"):
        if not paragraph:
            log.write_line("")
            continue
        wrapped = textwrap.wrap(
            paragraph,
            width=width,
            break_long_words=False,
            break_on_hyphens=False,
            subsequent_indent=subsequent_indent,
        )
        if not wrapped:
            log.write_line("")
        else:
            for line in wrapped:
                log.write_line(line)


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
    /* Override the default screen-selection style so highlighting text
       doesn't paint over it with a solid ANSI color. We use a 30% alpha
       primary and let the foreground inherit. Textual's truecolor theme
       already does this; we make it consistent across themes. */
    .screen--selection {
        background: $primary 30%;
        color: $text;
        text-style: none;
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
        # Priority-binding ctrl+c/cmd+c to screen.copy_text. The Input widget
        # binds `ctrl+c,super+c` to its own `action_copy` (which only copies
        # Input's internal selection), and Input is the focused widget during
        # chat — so without priority, screen-level copy never fires and Log
        # selections can never be copied. `action_copy_text` raises SkipAction
        # when there's no screen-level selection, so it falls through to
        # Input's own copy when the user has selected text inside the prompt.
        Binding(
            "ctrl+c,super+c",
            "screen.copy_text",
            "Copy",
            priority=True,
            show=True,
            key_display="^C",
        ),
        Binding("ctrl+d", "quit_app", "Quit", key_display="^D"),
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
            yield Log(id="log", auto_scroll=True)
            yield Input(id="prompt", placeholder="Type your message (Ctrl-D to quit)...")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"Metis • {self.session.active_model or self.runtime.global_default_model}"
        self.sub_title = (
            f"workspace: {self.session.workspace_path} • session: {self.session.id[:14]}…"
        )
        log = self.query_one(Log)
        _write_wrapped(log, "Metis chat")
        providers = sorted(
            {self.runtime.registry.provider_of(m) for m in self.runtime.registry.list_models()}
        )
        _write_wrapped(log, f"Providers: {', '.join(providers)}")
        _write_wrapped(
            log,
            f"Active model: {self.session.active_model or f'(default: {self.runtime.global_default_model})'}",
        )
        _write_wrapped(
            log,
            "Commands: /model <alias|id>, /model -, /cost, /models, /copy, /help. "
            "Ctrl-D to quit.",
        )
        if _is_apple_terminal():
            _write_wrapped(
                log,
                "Note: Terminal.app intercepts Cmd+C — it won't copy from this app. "
                "Use Ctrl+C to copy selected text, or /copy to grab the last "
                "assistant response. iTerm2 / Ghostty / WezTerm give standard "
                "Cmd+C behavior if you'd like to switch.",
            )
        log.write_line("")
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
        log = self.query_one(Log)
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/help", "/?"):
            # Short fixed-format help lines: keep as raw write_line so the
            # column alignment isn't re-wrapped on narrow terminals.
            log.write_line("Commands:")
            log.write_line("  /model <alias|id>     set the Active model for this session")
            log.write_line("  /model -              clear the Active model (use defaults)")
            log.write_line("  /model show           print the current Active model")
            log.write_line("  /cost                 session cost so far")
            log.write_line("  /models               list primary (latest) models")
            log.write_line("  /models all           list every registered model")
            log.write_line("  /models <pattern>     filter by substring (e.g. /models opus)")
            log.write_line("  /share                include last slash output in the next message")
            log.write_line("  /copy [n]             copy nth-most-recent reply (default 1)")
            log.write_line("  /help, /?             this list")
            log.write_line("  exit, quit, ^D      leave")
            return
        if cmd == "/copy":
            self._handle_copy_command(arg, log)
            return
        if cmd == "/model":
            if not arg or arg == "show":
                # Refresh first so a stale local snapshot can't lie.
                self.session = self.runtime.manager.get_session(self.session.id)
                _write_wrapped(
                    log,
                    f"Active model: {self.session.active_model or '(none — using defaults)'}",
                )
                return
            if arg == "-":
                self.runtime.manager.set_active_model(self.session.id, None)
                self.session = self.runtime.manager.get_session(self.session.id)
                _write_wrapped(log, "Active model cleared")
                self._refresh_title()
                return
            try:
                self.runtime.manager.set_active_model(self.session.id, arg)
                # Refresh local session so the title / future /model show
                # reads the truth, not the stale snapshot we cached at
                # session creation.
                self.session = self.runtime.manager.get_session(self.session.id)
                resolved = self.session.active_model
                if resolved != arg:
                    _write_wrapped(
                        log, f"Active model: {resolved}   (matched from {arg!r})"
                    )
                else:
                    _write_wrapped(log, f"Active model: {resolved}")
                self._refresh_title()
            except AmbiguousModelError as exc:
                _write_wrapped(log, f"ambiguous model: {exc.input}")
                _write_wrapped(log, "  did you mean one of:")
                for candidate in exc.candidates:
                    _write_wrapped(log, f"    {candidate}")
            except UnknownAliasError as exc:
                _write_wrapped(log, f"unknown model: {exc.alias}")
            return
        if cmd == "/models":
            mode, pattern = parse_models_command(arg)
            displayed, total = resolve_models(
                registry=self.runtime.registry,
                mode=mode,
                pattern=pattern,
                always_include=self.session.active_model,
            )
            # Continuation lines for wrapped rows indent under the model
            # column (4 cells past the left edge: "  " gutter + "* " marker
            # column) so wrapped label content visually nests under its
            # model entry rather than starting at column 0.
            rendered = format_models_lines(
                displayed,
                registry=self.runtime.registry,
                pricing=self.runtime.pricing,
                sticky_model=self.session.active_model,
            )
            for line in rendered:
                _write_wrapped(log, line, subsequent_indent="    ")
            hint = truncation_hint(displayed, total, mode=mode, pattern=pattern)
            if hint:
                _write_wrapped(log, hint)
                rendered.append(hint)
            self.runtime.manager.buffer_slash_output(
                self.session.id, "\n".join(rendered)
            )
            return
        if cmd == "/share":
            buffered = self.runtime.manager.mark_share_pending(self.session.id)
            if buffered is None:
                _write_wrapped(
                    log,
                    "Nothing to share yet — run /models (or another slash command) first.",
                )
            else:
                line_count = buffered.count("\n") + 1
                _write_wrapped(
                    log,
                    f"Will share {line_count} line(s) of recent slash output "
                    f"with the agent on your next message.",
                )
            return
        if cmd == "/cost":
            _write_wrapped(
                log,
                f"session cost so far: ${self.session.cost_so_far_usd:.4f} "
                f"({self.session.turn_count} turns)",
            )
            return
        _write_wrapped(log, f"unknown command: {cmd}. /help for the list.")

    def _refresh_title(self) -> None:
        self.title = f"Metis • {self.session.active_model or self.runtime.global_default_model}"

    def _handle_copy_command(self, arg: str, log: Log) -> None:
        """Copy the nth-most-recent assistant message text to clipboard.

        `/copy` copies the last assistant reply; `/copy 2` copies the one
        before that; etc. Counts from 1, where 1 is the most recent. Skips
        non-text content (tool_use blocks etc.) — only TextBlock text is
        copied, which is what users almost always want.
        """
        n = 1
        if arg:
            try:
                n = int(arg)
                if n < 1:
                    raise ValueError
            except ValueError:
                _write_wrapped(
                    log,
                    "usage: /copy [n]  (n >= 1; default 1 = most recent reply)",
                )
                return
        messages = self.runtime.session_store.get_messages(self.session.id)
        assistant_messages = [m for m in messages if m.role == Role.ASSISTANT]
        if not assistant_messages:
            _write_wrapped(log, "no assistant messages yet")
            return
        if n > len(assistant_messages):
            _write_wrapped(
                log, f"only {len(assistant_messages)} assistant message(s) so far"
            )
            return
        target = assistant_messages[-n]
        text = _extract_message_text(target)
        if not text:
            _write_wrapped(log, "that message has no text content (tool calls only)")
            return
        self.copy_to_clipboard(text)

    # ---- Turn worker --------------------------------------------------

    @work(exclusive=True)
    async def _submit_turn(self, text: str) -> None:
        log = self.query_one(Log)
        _write_wrapped(log, f"> {text}")
        renderer = _TUIRenderer(log)
        try:
            result = await self.runtime.manager.submit_turn(
                self.session.id, text, on_streaming_event=renderer.handle
            )
        except UnknownAliasError as exc:
            _write_wrapped(log, f"unknown alias: @{exc.alias}")
            return
        except RoutingError as exc:
            _write_wrapped(log, f"routing failed: {exc}")
            return
        except AdapterError as exc:
            _write_wrapped(log, f"adapter error [{exc.error_class.value}]: {exc.message}")
            return
        except Exception as exc:
            logger.exception("unhandled error in turn worker")
            _write_wrapped(log, f"error: {exc}")
            return

        renderer.finalize()
        if result.routing_fallthrough:
            _write_wrapped(log, result.routing_fallthrough)
        cost = f"${result.cost_usd:.4f}" if result.cost_usd >= Decimal("0.0001") else "<$0.0001"
        _write_wrapped(
            log,
            f"[{result.chosen_model} • {cost} • "
            f"{result.llm_call_count} LLM / {result.tool_call_count} tool]",
        )
        log.write_line("")
        # Refresh reactive state from the persisted session.
        fresh = self.runtime.session_store.get_session(self.session.id)
        self.session.cost_so_far_usd = fresh.cost_so_far_usd
        self.session.turn_count = fresh.turn_count
        self.cost_so_far = float(fresh.cost_so_far_usd)

    # ---- Quit ---------------------------------------------------------

    async def action_quit_app(self) -> None:
        self.exit()

    # ---- Clipboard ----------------------------------------------------

    def copy_to_clipboard(self, text: str) -> None:
        """Copy text to the OS clipboard.

        Textual's default emits an OSC 52 escape, which macOS Terminal.app
        silently drops (Textual's own docstring acknowledges this). On
        Darwin we shell out to `pbcopy` instead — it works regardless of
        terminal capabilities and is universally available on macOS. Other
        platforms fall back to Textual's OSC 52 path, which works in iTerm2,
        WezTerm, Alacritty, Kitty, Ghostty, and most modern terminals.
        """
        if sys.platform == "darwin":
            try:
                subprocess.run(
                    ["pbcopy"],
                    input=text.encode("utf-8"),
                    check=True,
                    timeout=2,
                )
                self.notify(f"Copied {len(text)} chars to clipboard")
                return
            except (subprocess.SubprocessError, FileNotFoundError) as exc:
                logger.warning("pbcopy failed: %s; falling back to OSC 52", exc)
        super().copy_to_clipboard(text)
        self.notify(f"Copied {len(text)} chars to clipboard")


# ---------------------------------------------------------------------------
# Streaming renderer
# ---------------------------------------------------------------------------


class _TUIRenderer:
    """Buffers TextDeltas and flushes them to Log on newlines or markers.

    Text deltas are batched into chunks that get written on newline
    boundaries or when a non-text event (tool call marker, end of message)
    arrives. The result feels like live streaming for multi-sentence
    responses without producing one log entry per token.
    """

    def __init__(self, log: Log) -> None:
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
            _write_wrapped(self._log, f"→ {event.tool_name}(...)")
            self._in_text = False

    def finalize(self) -> None:
        self._flush()

    def _flush(self) -> None:
        if not self._buffer:
            return
        text = "".join(self._buffer).strip("\n")
        if text:
            _write_wrapped(self._log, text)
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
