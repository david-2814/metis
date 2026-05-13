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
    assert "echo" in {td.name for td in d.get_definitions()}
    d.unregister("echo")
    assert d.get_definitions() == []


async def test_duplicate_registration_rejected(bus: EventBus):
    d = ToolDispatcher(bus)
    d.register(_EchoTool)
    with pytest.raises(ToolRegistrationError):
        d.register(_EchoTool)


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
