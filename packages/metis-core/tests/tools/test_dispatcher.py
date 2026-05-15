"""Tests for ToolDispatcher: lookup, validation, confirmation, events, errors."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from metis_core.canonical.content import TextBlock, ToolUseBlock
from metis_core.canonical.tools import SideEffects, ToolDefinition
from metis_core.events.bus import EventBus, EventFilter, Subscription
from metis_core.events.envelope import Event
from metis_core.tools.confirmation import (
    ConfirmationDecision,
    ConfirmationMode,
    ConfirmationPolicy,
    ConfirmationRequest,
)
from metis_core.tools.dispatcher import ToolDispatcher
from metis_core.tools.errors import ToolRegistrationError
from metis_core.tools.protocol import ToolContext, ToolOutput

# ---- Fixtures -----------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
async def bus() -> EventBus:
    bus = EventBus()
    bus.start()
    return bus


@pytest.fixture
async def event_log(bus: EventBus) -> list[Event]:
    events: list[Event] = []

    async def handler(e: Event) -> None:
        # Filter bus.* lifecycle events; the dispatcher tests care about
        # domain events (tool.*, etc.) and would otherwise see a leading
        # bus.subscriber_registered from the test fixture's own subscribe.
        if e.type.startswith("bus."):
            return
        events.append(e)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="log", fast_path=True))
    return events


# ---- Simple tools for testing ------------------------------------------


class _EchoTool:
    definition = ToolDefinition(
        name="echo",
        description="Echo back the message.",
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
        side_effects=SideEffects.NONE,
        requires_workspace=False,
    )

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        return ToolOutput(content=[TextBlock(text=f"echo: {input['message']}")])

    async def cancel(self) -> bool:
        return True


class _SlowTool:
    definition = ToolDefinition(
        name="slow",
        description="Sleeps.",
        input_schema={
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
            "required": ["seconds"],
            "additionalProperties": False,
        },
        side_effects=SideEffects.NONE,
        requires_workspace=False,
    )

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        await asyncio.sleep(input["seconds"])
        return ToolOutput(content=[TextBlock(text="done")])

    async def cancel(self) -> bool:
        return True


class _RaisingTool:
    definition = ToolDefinition(
        name="raises",
        description="Always raises.",
        input_schema={"type": "object", "additionalProperties": True},
        side_effects=SideEffects.NONE,
        requires_workspace=False,
    )

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        raise RuntimeError("boom")

    async def cancel(self) -> bool:
        return True


class _WriteTool:
    """Write side-effect tool for confirmation tests."""

    definition = ToolDefinition(
        name="write_thing",
        description="Pretends to write.",
        input_schema={"type": "object", "additionalProperties": True},
        side_effects=SideEffects.WRITE,
        requires_workspace=False,
    )

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        return ToolOutput(content=[TextBlock(text="wrote")], files_modified=["foo.txt"])

    async def cancel(self) -> bool:
        return True


def _tool_use(name: str, **input: object) -> ToolUseBlock:
    return ToolUseBlock(id=f"tu_{name}", name=name, input=input)


# ---- Registration -------------------------------------------------------


async def test_register_and_unregister(bus: EventBus, workspace: Path):
    d = ToolDispatcher(bus)
    d.register(_EchoTool)
    assert "echo" in {td.name for td in d.get_definitions_for_session()}
    d.unregister("echo")
    assert d.get_definitions_for_session() == []


async def test_get_definitions_for_session_accepts_session_arg(bus: EventBus):
    """Spec §3.4: the surface takes a session so worker-session filtering
    can land later without rewriting callers (per routing-engine.md §6.2.1).
    v1 ignores the arg and returns all tools; the signature must accept it.
    """
    from metis_core.sessions.store import Session

    d = ToolDispatcher(bus)
    d.register(_EchoTool)
    session = Session(id="ses_test", workspace_path="/tmp", active_model=None)
    assert {td.name for td in d.get_definitions_for_session(session)} == {"echo"}
    # And the same call with no arg still works (default None).
    assert {td.name for td in d.get_definitions_for_session()} == {"echo"}


async def test_duplicate_registration_rejected(bus: EventBus):
    d = ToolDispatcher(bus)
    d.register(_EchoTool)
    with pytest.raises(ToolRegistrationError):
        d.register(_EchoTool)


# ---- Canonical JSON Schema subset enforcement --------------------------


def _make_tool_class(name: str, schema: dict) -> type:
    """Build a minimal Tool class with the given name + input_schema."""

    class _Tool:
        definition = ToolDefinition(
            name=name,
            description=f"{name} test tool",
            input_schema=schema,
            side_effects=SideEffects.NONE,
            requires_workspace=False,
        )

        async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
            return ToolOutput(content=[TextBlock(text="ok")])

        async def cancel(self) -> bool:
            return True

    return _Tool


@pytest.mark.parametrize(
    ("keyword", "schema"),
    [
        (
            "oneOf",
            {
                "type": "object",
                "properties": {
                    "x": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
                },
            },
        ),
        (
            "$ref",
            {
                "type": "object",
                "properties": {"x": {"$ref": "#/definitions/Foo"}},
            },
        ),
        (
            "allOf",
            {
                "type": "object",
                "properties": {
                    "x": {"allOf": [{"type": "string"}, {"minLength": 1}]},
                },
            },
        ),
        (
            "anyOf",
            {
                "type": "object",
                "properties": {
                    "x": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
            },
        ),
        (
            "not",
            {
                "type": "object",
                "properties": {"x": {"not": {"type": "string"}}},
            },
        ),
    ],
)
async def test_register_rejects_disallowed_subset_keyword(
    bus: EventBus, keyword: str, schema: dict
):
    """tool-dispatcher.md §7.1: disallowed JSON Schema constructs must fail loudly."""
    d = ToolDispatcher(bus)
    tool_cls = _make_tool_class(f"bad_{keyword.lstrip('$')}", schema)
    with pytest.raises(ToolRegistrationError) as exc_info:
        d.register(tool_cls)
    assert "canonical subset" in str(exc_info.value)
    assert keyword in str(exc_info.value)


async def test_builtins_and_memory_tools_still_register(bus: EventBus):
    """The 5 file/shell builtins + 3 memory tools must register cleanly under
    the canonical-subset validator."""
    from metis_core.memory.tools import register_memory_tools
    from metis_core.tools.builtins import register_builtins

    d = ToolDispatcher(bus)
    register_builtins(d)
    register_memory_tools(d)
    names = {td.name for td in d.get_definitions_for_session()}
    assert {"read_file", "write_file", "patch_file", "list_dir", "shell"} <= names
    assert {"memory_add", "memory_replace", "memory_consolidate"} <= names


# ---- Happy path ---------------------------------------------------------


async def test_dispatch_happy_path(bus: EventBus, workspace: Path, event_log: list[Event]):
    d = ToolDispatcher(bus)
    d.register(_EchoTool)
    result = await d.dispatch(
        _tool_use("echo", message="hi"),
        session_id="sess_1",
        turn_id="01HZ_t1",
        workspace_path=str(workspace),
    )
    await bus.drain()
    await bus.stop()

    assert result.is_error is False
    assert isinstance(result.content[0], TextBlock)
    assert result.content[0].text == "echo: hi"
    types = [e.type for e in event_log]
    assert types == ["tool.called", "tool.completed"]
    called = event_log[0]
    assert called.payload["tool_name"] == "echo"
    assert called.payload["side_effects"] == "none"
    completed = event_log[1]
    assert completed.payload["success"] is True
    assert completed.payload["latency_ms"] >= 0


# ---- Lookup failure -----------------------------------------------------


async def test_dispatch_unknown_tool(bus: EventBus, workspace: Path, event_log: list[Event]):
    d = ToolDispatcher(bus)
    result = await d.dispatch(
        _tool_use("nope"),
        session_id="sess_1",
        turn_id="01HZ_t1",
        workspace_path=str(workspace),
    )
    await bus.drain()
    await bus.stop()

    assert result.is_error is True
    assert "not registered" in result.content[0].text
    types = [e.type for e in event_log]
    assert types == ["tool.failed"]
    assert event_log[0].payload["error_class"] == "not_found"


# ---- Schema validation --------------------------------------------------


async def test_dispatch_input_validation_failure(
    bus: EventBus, workspace: Path, event_log: list[Event]
):
    d = ToolDispatcher(bus)
    d.register(_EchoTool)
    result = await d.dispatch(
        _tool_use("echo"),  # missing required `message`
        session_id="sess_1",
        turn_id="01HZ_t1",
        workspace_path=str(workspace),
    )
    await bus.drain()
    await bus.stop()

    assert result.is_error is True
    types = [e.type for e in event_log]
    assert "tool.input_invalid" in types


# ---- Tool raises --------------------------------------------------------


async def test_dispatch_tool_raises_wraps_as_execution_error(
    bus: EventBus, workspace: Path, event_log: list[Event]
):
    d = ToolDispatcher(bus)
    d.register(_RaisingTool)
    result = await d.dispatch(
        _tool_use("raises"),
        session_id="sess_1",
        turn_id="01HZ_t1",
        workspace_path=str(workspace),
    )
    await bus.drain()
    await bus.stop()

    assert result.is_error is True
    failed = next(e for e in event_log if e.type == "tool.failed")
    assert failed.payload["error_class"] == "execution_error"


# ---- Timeout ------------------------------------------------------------


async def test_dispatch_timeout(bus: EventBus, workspace: Path, event_log: list[Event]):
    d = ToolDispatcher(bus, timeouts={SideEffects.NONE: 0.05})
    d.register(_SlowTool)
    result = await d.dispatch(
        _tool_use("slow", seconds=1.0),
        session_id="sess_1",
        turn_id="01HZ_t1",
        workspace_path=str(workspace),
    )
    await bus.drain()
    await bus.stop()

    assert result.is_error is True
    failed = next(e for e in event_log if e.type == "tool.failed")
    assert failed.payload["error_class"] == "timeout"


# ---- Confirmation -------------------------------------------------------


class _StubHandler:
    def __init__(self, decision: ConfirmationDecision) -> None:
        self.decision = decision
        self.calls: list[ConfirmationRequest] = []

    async def request(self, req: ConfirmationRequest) -> ConfirmationDecision:
        self.calls.append(req)
        return self.decision


async def test_confirmation_allow_proceeds(bus: EventBus, workspace: Path, event_log: list[Event]):
    handler = _StubHandler(ConfirmationDecision.ALLOW)
    d = ToolDispatcher(bus, confirmation_handler=handler)
    d.register(_WriteTool)
    result = await d.dispatch(
        _tool_use("write_thing"),
        session_id="sess_1",
        turn_id="01HZ_t1",
        workspace_path=str(workspace),
    )
    await bus.drain()
    await bus.stop()

    assert result.is_error is False
    assert len(handler.calls) == 1
    types = [e.type for e in event_log]
    assert "tool.confirmation_requested" in types
    assert "tool.confirmation_resolved" in types
    assert "tool.completed" in types


async def test_confirmation_deny_blocks_execution(
    bus: EventBus, workspace: Path, event_log: list[Event]
):
    handler = _StubHandler(ConfirmationDecision.DENY)
    d = ToolDispatcher(bus, confirmation_handler=handler)
    d.register(_WriteTool)
    result = await d.dispatch(
        _tool_use("write_thing"),
        session_id="sess_1",
        turn_id="01HZ_t1",
        workspace_path=str(workspace),
    )
    await bus.drain()
    await bus.stop()

    assert result.is_error is True
    failed = next(e for e in event_log if e.type == "tool.failed")
    assert failed.payload["error_class"] == "user_denied"
    types = [e.type for e in event_log]
    assert "tool.completed" not in types


async def test_policy_deny_mode_blocks_without_handler_call(
    bus: EventBus, workspace: Path, event_log: list[Event]
):
    policy = ConfirmationPolicy(
        per_tool={"write_thing": ConfirmationMode.DENY},
    )
    handler = _StubHandler(ConfirmationDecision.ALLOW)
    d = ToolDispatcher(bus, confirmation_policy=policy, confirmation_handler=handler)
    d.register(_WriteTool)
    result = await d.dispatch(
        _tool_use("write_thing"),
        session_id="sess_1",
        turn_id="01HZ_t1",
        workspace_path=str(workspace),
    )
    await bus.drain()
    await bus.stop()

    assert result.is_error is True
    assert handler.calls == []  # never consulted
    types = [e.type for e in event_log]
    assert "tool.confirmation_requested" not in types


async def test_auto_allow_default_for_read(bus: EventBus, workspace: Path, event_log: list[Event]):
    """READ side-effects default to auto — no confirmation event emitted."""

    class _ReadTool:
        definition = ToolDefinition(
            name="reader",
            description="reads",
            input_schema={"type": "object", "additionalProperties": True},
            side_effects=SideEffects.READ,
            requires_workspace=False,
        )

        async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
            return ToolOutput(content=[TextBlock(text="ok")])

        async def cancel(self) -> bool:
            return True

    d = ToolDispatcher(bus)
    d.register(_ReadTool)
    await d.dispatch(
        _tool_use("reader"),
        session_id="sess_1",
        turn_id="01HZ_t1",
        workspace_path=str(workspace),
    )
    await bus.drain()
    await bus.stop()
    types = [e.type for e in event_log]
    assert "tool.confirmation_requested" not in types


# ---- Cancellation -------------------------------------------------------


async def test_cancel_session_tools(bus: EventBus, workspace: Path):
    d = ToolDispatcher(bus)
    d.register(_SlowTool)
    task = asyncio.create_task(
        d.dispatch(
            _tool_use("slow", seconds=10.0),
            session_id="sess_cancel",
            turn_id="t",
            workspace_path=str(workspace),
        )
    )
    # Give dispatch a moment to enter execute()
    await asyncio.sleep(0.05)
    await d.cancel_session_tools("sess_cancel")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await bus.drain()
    await bus.stop()


# ---- Per-session concurrency cap (§4.1) ---------------------------------


class _GatedTool:
    """Tool that signals when it starts and waits for an external release."""

    _gate_counter = 0
    in_flight = 0
    peak_in_flight = 0

    def __init__(self) -> None:
        _GatedTool._gate_counter += 1

    definition = ToolDefinition(
        name="gated",
        description="Signals on entry, blocks until released.",
        input_schema={"type": "object", "additionalProperties": True},
        side_effects=SideEffects.NONE,
        requires_workspace=False,
    )

    # Class-wide synchronization so all factory-produced instances share state.
    release_event: asyncio.Event | None = None
    entered_event: asyncio.Event | None = None

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        _GatedTool.in_flight += 1
        _GatedTool.peak_in_flight = max(_GatedTool.peak_in_flight, _GatedTool.in_flight)
        try:
            if _GatedTool.entered_event is not None:
                _GatedTool.entered_event.set()
            if _GatedTool.release_event is not None:
                await _GatedTool.release_event.wait()
            return ToolOutput(content=[TextBlock(text="done")])
        finally:
            _GatedTool.in_flight -= 1

    async def cancel(self) -> bool:
        return True


async def test_concurrency_cap_default_is_four(bus: EventBus, workspace: Path):
    """tool-dispatcher.md §4.1: per-session cap defaults to 4 concurrent calls."""
    _GatedTool.in_flight = 0
    _GatedTool.peak_in_flight = 0
    _GatedTool.release_event = asyncio.Event()
    _GatedTool.entered_event = None  # not used here

    d = ToolDispatcher(bus)
    d.register(_GatedTool)

    # Six dispatches on the same session; expect at most 4 to run concurrently.
    tasks = [
        asyncio.create_task(
            d.dispatch(
                ToolUseBlock(id=f"tu_{i}", name="gated", input={}),
                session_id="sess_cap",
                turn_id="t",
                workspace_path=str(workspace),
            )
        )
        for i in range(6)
    ]
    # Let the first wave enter execute.
    for _ in range(20):
        await asyncio.sleep(0.01)
        if _GatedTool.in_flight >= 4:
            break
    assert _GatedTool.in_flight == 4, (
        f"expected exactly 4 concurrent tools under default cap, saw {_GatedTool.in_flight}"
    )
    _GatedTool.release_event.set()
    await asyncio.gather(*tasks)
    assert _GatedTool.peak_in_flight == 4
    await bus.drain()
    await bus.stop()


async def test_concurrency_cap_configurable(bus: EventBus, workspace: Path):
    """The cap is constructor-configurable."""
    _GatedTool.in_flight = 0
    _GatedTool.peak_in_flight = 0
    _GatedTool.release_event = asyncio.Event()
    _GatedTool.entered_event = None

    d = ToolDispatcher(bus, concurrency_cap_per_session=2)
    d.register(_GatedTool)

    tasks = [
        asyncio.create_task(
            d.dispatch(
                ToolUseBlock(id=f"tu_{i}", name="gated", input={}),
                session_id="sess_cap_2",
                turn_id="t",
                workspace_path=str(workspace),
            )
        )
        for i in range(5)
    ]
    for _ in range(20):
        await asyncio.sleep(0.01)
        if _GatedTool.in_flight >= 2:
            break
    assert _GatedTool.in_flight == 2
    _GatedTool.release_event.set()
    await asyncio.gather(*tasks)
    assert _GatedTool.peak_in_flight == 2
    await bus.drain()
    await bus.stop()


async def test_concurrency_cap_is_per_session(bus: EventBus, workspace: Path):
    """The cap applies per session, not globally — two sessions of cap=1 each
    can still both run concurrently."""
    _GatedTool.in_flight = 0
    _GatedTool.peak_in_flight = 0
    _GatedTool.release_event = asyncio.Event()
    _GatedTool.entered_event = None

    d = ToolDispatcher(bus, concurrency_cap_per_session=1)
    d.register(_GatedTool)

    t1 = asyncio.create_task(
        d.dispatch(
            ToolUseBlock(id="tu_a", name="gated", input={}),
            session_id="sess_a",
            turn_id="t",
            workspace_path=str(workspace),
        )
    )
    t2 = asyncio.create_task(
        d.dispatch(
            ToolUseBlock(id="tu_b", name="gated", input={}),
            session_id="sess_b",
            turn_id="t",
            workspace_path=str(workspace),
        )
    )
    for _ in range(20):
        await asyncio.sleep(0.01)
        if _GatedTool.in_flight >= 2:
            break
    assert _GatedTool.in_flight == 2  # both sessions running, cap is per-session
    _GatedTool.release_event.set()
    await asyncio.gather(t1, t2)
    await bus.drain()
    await bus.stop()


def test_concurrency_cap_rejects_invalid_value(bus: EventBus):
    with pytest.raises(ValueError):
        ToolDispatcher(bus, concurrency_cap_per_session=0)


# ---- Workspace escape pre-check (§9.2) ----------------------------------


class _WorkspaceReadTool:
    """Read-side tool that actually touches the workspace API."""

    definition = ToolDefinition(
        name="ws_read",
        description="Read a workspace file.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        side_effects=SideEffects.READ,
        requires_workspace=True,
    )

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        text = context.workspace_files.read(input["path"])
        return ToolOutput(content=[TextBlock(text=text)])

    async def cancel(self) -> bool:
        return True


async def test_workspace_escape_emits_no_tool_called(
    bus: EventBus, workspace: Path, event_log: list[Event]
):
    """tool-dispatcher.md §9.2: escape rejection emits tool.failed *without*
    a preceding tool.called."""
    (workspace / "ok.txt").write_text("hi")
    d = ToolDispatcher(bus)
    d.register(_WorkspaceReadTool)

    result = await d.dispatch(
        _tool_use("ws_read", path="../../etc/passwd"),
        session_id="sess_escape",
        turn_id="t",
        workspace_path=str(workspace),
    )
    await bus.drain()
    await bus.stop()

    assert result.is_error is True
    types = [e.type for e in event_log]
    assert "tool.called" not in types, f"escape rejection must not emit tool.called; got {types}"
    failed = next(e for e in event_log if e.type == "tool.failed")
    assert failed.payload["error_class"] == "permission_denied"


async def test_workspace_path_inside_root_still_emits_tool_called(
    bus: EventBus, workspace: Path, event_log: list[Event]
):
    """In-scope paths should still emit tool.called → tool.completed."""
    (workspace / "ok.txt").write_text("hi")
    d = ToolDispatcher(bus)
    d.register(_WorkspaceReadTool)

    result = await d.dispatch(
        _tool_use("ws_read", path="ok.txt"),
        session_id="sess_ok",
        turn_id="t",
        workspace_path=str(workspace),
    )
    await bus.drain()
    await bus.stop()

    assert result.is_error is False
    types = [e.type for e in event_log]
    assert types == ["tool.called", "tool.completed"]


# ---- confirmation_request_id is a ULID (§ event-bus catalog) ------------


async def test_confirmation_request_id_is_ulid(
    bus: EventBus, workspace: Path, event_log: list[Event]
):
    """The event-bus catalog declares confirmation_request_id as a ULID;
    the impl must mint a fresh ULID rather than re-using the tool_use_id."""
    handler = _StubHandler(ConfirmationDecision.ALLOW)
    d = ToolDispatcher(bus, confirmation_handler=handler)
    d.register(_WriteTool)

    await d.dispatch(
        _tool_use("write_thing"),
        session_id="sess_conf",
        turn_id="t",
        workspace_path=str(workspace),
    )
    await bus.drain()
    await bus.stop()

    req_event = next(e for e in event_log if e.type == "tool.confirmation_requested")
    res_event = next(e for e in event_log if e.type == "tool.confirmation_resolved")
    req_id = req_event.payload["confirmation_request_id"]
    res_id = res_event.payload["confirmation_request_id"]

    assert req_id == res_id
    # ULIDs are Crockford base32, 26 chars, no `conf_` prefix.
    assert not req_id.startswith("conf_"), "confirmation_request_id should be a bare ULID"
    assert len(req_id) == 26, f"expected 26-char ULID, got {len(req_id)}: {req_id!r}"
    # Verify it parses back as a ULID.
    from ulid import ULID

    ULID.from_str(req_id)
