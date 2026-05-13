"""RemoteConfirmationHandler unit tests + dispatcher integration."""

from __future__ import annotations

import asyncio

import pytest
from metis_core.canonical.tools import SideEffects
from metis_core.tools.confirmation import ConfirmationDecision, ConfirmationRequest
from metis_server.confirmations import RemoteConfirmationHandler, _request_id_for


def _req(tool_use_id: str = "tu_1") -> ConfirmationRequest:
    return ConfirmationRequest(
        tool_use_id=tool_use_id,
        tool_name="write_file",
        side_effects=SideEffects.WRITE,
        input_summary="(test)",
    )


# ---- Plain resolve flow -------------------------------------------------


async def test_resolve_unblocks_request_with_allow():
    handler = RemoteConfirmationHandler()
    rid = _request_id_for("tu_1")
    task = asyncio.create_task(handler.request(_req("tu_1")))
    # Give the request a moment to register itself in the pending dict.
    for _ in range(50):
        if handler.is_pending(rid):
            break
        await asyncio.sleep(0.01)
    assert handler.is_pending(rid)
    assert handler.resolve(rid, decision=ConfirmationDecision.ALLOW) is True
    decision = await task
    assert decision == ConfirmationDecision.ALLOW


async def test_resolve_deny():
    handler = RemoteConfirmationHandler()
    task = asyncio.create_task(handler.request(_req("tu_2")))
    for _ in range(50):
        if handler.is_pending(_request_id_for("tu_2")):
            break
        await asyncio.sleep(0.01)
    handler.resolve(_request_id_for("tu_2"), decision=ConfirmationDecision.DENY)
    assert await task == ConfirmationDecision.DENY


# ---- Race / double-resolve ---------------------------------------------


async def test_double_resolve_second_returns_false():
    handler = RemoteConfirmationHandler()
    rid = _request_id_for("tu_3")
    task = asyncio.create_task(handler.request(_req("tu_3")))
    for _ in range(50):
        if handler.is_pending(rid):
            break
        await asyncio.sleep(0.01)
    first = handler.resolve(rid, decision=ConfirmationDecision.ALLOW)
    second = handler.resolve(rid, decision=ConfirmationDecision.DENY)
    assert first is True
    assert second is False
    # The first decision is what the dispatcher sees.
    assert await task == ConfirmationDecision.ALLOW


async def test_resolve_unknown_returns_false():
    handler = RemoteConfirmationHandler()
    assert handler.resolve("conf_nope", decision=ConfirmationDecision.ALLOW) is False


# ---- Cancellation cleanup ----------------------------------------------


async def test_request_cancellation_clears_pending():
    """If the dispatcher's wait_for times out and cancels the request, the
    handler should drop the pending entry so a re-issued confirmation can
    register fresh."""
    handler = RemoteConfirmationHandler()
    rid = _request_id_for("tu_cancel")
    task = asyncio.create_task(handler.request(_req("tu_cancel")))
    for _ in range(50):
        if handler.is_pending(rid):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Brief moment for the finally block.
    await asyncio.sleep(0.01)
    assert handler.is_pending(rid) is False


# ---- Multiple concurrent confirmations ---------------------------------


async def test_concurrent_independent_confirmations():
    handler = RemoteConfirmationHandler()
    task_a = asyncio.create_task(handler.request(_req("tu_a")))
    task_b = asyncio.create_task(handler.request(_req("tu_b")))
    for _ in range(50):
        if handler.is_pending(_request_id_for("tu_a")) and handler.is_pending(
            _request_id_for("tu_b")
        ):
            break
        await asyncio.sleep(0.01)
    handler.resolve(_request_id_for("tu_a"), decision=ConfirmationDecision.ALLOW)
    handler.resolve(_request_id_for("tu_b"), decision=ConfirmationDecision.DENY)
    assert await task_a == ConfirmationDecision.ALLOW
    assert await task_b == ConfirmationDecision.DENY


async def test_pending_request_ids_lists_outstanding():
    handler = RemoteConfirmationHandler()
    task = asyncio.create_task(handler.request(_req("tu_list")))
    for _ in range(50):
        if handler.is_pending(_request_id_for("tu_list")):
            break
        await asyncio.sleep(0.01)
    assert _request_id_for("tu_list") in handler.pending_request_ids()
    handler.resolve(_request_id_for("tu_list"), decision=ConfirmationDecision.ALLOW)
    await task
    assert _request_id_for("tu_list") not in handler.pending_request_ids()


# ---- Scope passed through ----------------------------------------------


async def test_scope_recorded():
    handler = RemoteConfirmationHandler()
    rid = _request_id_for("tu_scope")
    task = asyncio.create_task(handler.request(_req("tu_scope")))
    for _ in range(50):
        if handler.is_pending(rid):
            break
        await asyncio.sleep(0.01)
    handler.resolve(rid, decision=ConfirmationDecision.ALLOW, scope="session")
    await task
    # Internal state cleaned up after resolve completes the await — no
    # observable scope getter post-resolve in v1. The scope is carried via
    # tool.confirmation_resolved event by the dispatcher; tested elsewhere.


# ---- End-to-end with ToolDispatcher ------------------------------------


async def test_dispatcher_blocks_until_resolved():
    """A WRITE-side-effect tool dispatched through the dispatcher with a
    RemoteConfirmationHandler installed should block until resolve() fires,
    then proceed to execute."""
    from metis_core.canonical.content import TextBlock, ToolUseBlock
    from metis_core.canonical.tools import ToolDefinition
    from metis_core.events.bus import EventBus
    from metis_core.tools.dispatcher import ToolDispatcher
    from metis_core.tools.protocol import ToolContext, ToolOutput

    class _WriteTool:
        definition = ToolDefinition(
            name="writer",
            description="writes",
            input_schema={"type": "object", "additionalProperties": True},
            side_effects=SideEffects.WRITE,
            requires_workspace=False,
        )

        async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
            return ToolOutput(content=[TextBlock(text="ok")])

        async def cancel(self) -> bool:
            return True

    bus = EventBus()
    bus.start()
    handler = RemoteConfirmationHandler()
    dispatcher = ToolDispatcher(bus, confirmation_handler=handler)
    dispatcher.register(_WriteTool)

    tool_use = ToolUseBlock(id="tu_disp", name="writer", input={})
    dispatch_task = asyncio.create_task(
        dispatcher.dispatch(
            tool_use,
            session_id="s",
            turn_id="t",
            workspace_path="/tmp",
        )
    )

    # Wait for the dispatch to reach the confirmation step.
    for _ in range(50):
        if handler.is_pending(_request_id_for("tu_disp")):
            break
        await asyncio.sleep(0.01)
    assert handler.is_pending(_request_id_for("tu_disp"))

    # Resolve allow → dispatch proceeds → tool executes.
    handler.resolve(_request_id_for("tu_disp"), decision=ConfirmationDecision.ALLOW)
    result = await dispatch_task
    await bus.drain()
    await bus.stop()
    assert result.is_error is False
    assert result.content[0].text == "ok"


async def test_dispatcher_denial_short_circuits():
    """Resolving DENY should cause the dispatcher to return is_error=True
    without executing the tool."""
    from metis_core.canonical.content import TextBlock, ToolUseBlock
    from metis_core.canonical.tools import ToolDefinition
    from metis_core.events.bus import EventBus
    from metis_core.tools.dispatcher import ToolDispatcher
    from metis_core.tools.protocol import ToolContext, ToolOutput

    executed = []

    class _WriteTool:
        definition = ToolDefinition(
            name="writer",
            description="writes",
            input_schema={"type": "object", "additionalProperties": True},
            side_effects=SideEffects.WRITE,
            requires_workspace=False,
        )

        async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
            executed.append(True)
            return ToolOutput(content=[TextBlock(text="should not run")])

        async def cancel(self) -> bool:
            return True

    bus = EventBus()
    bus.start()
    handler = RemoteConfirmationHandler()
    dispatcher = ToolDispatcher(bus, confirmation_handler=handler)
    dispatcher.register(_WriteTool)

    task = asyncio.create_task(
        dispatcher.dispatch(
            ToolUseBlock(id="tu_deny", name="writer", input={}),
            session_id="s",
            turn_id="t",
            workspace_path="/tmp",
        )
    )
    for _ in range(50):
        if handler.is_pending(_request_id_for("tu_deny")):
            break
        await asyncio.sleep(0.01)
    handler.resolve(_request_id_for("tu_deny"), decision=ConfirmationDecision.DENY)
    result = await task
    await bus.drain()
    await bus.stop()
    assert result.is_error is True
    assert executed == []
