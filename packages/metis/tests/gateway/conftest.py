"""Shared fixtures for gateway tests.

Builds a real `GatewayRuntime` wired against a scripted adapter so unit tests
don't make live API calls.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from metis.core.adapters.protocol import (
    CanonicalRequest,
    CanonicalResponse,
    StopReason,
    TokenUsage,
)
from metis.core.adapters.streaming import (
    MessageComplete,
    MessageStart,
    StreamingEvent,
    TextDelta,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)
from metis.core.canonical.capabilities import AdapterCapabilities
from metis.core.canonical.content import ContentBlock, TextBlock, ToolUseBlock
from metis.core.events.bus import EventBus
from metis.core.pricing import DEFAULT_PRICE_TABLE
from metis.core.routing import EMPTY_POLICY, ModelRegistry, RoutingEngine
from metis.core.trace.store import TraceStore
from metis.gateway.auth import GatewayKey, Keystore
from metis.gateway.quotas import QuotaTracker
from metis.gateway.runtime import GatewayRuntime


class ScriptedAdapter:
    """Records every request and returns the next scripted response.

    Supports cancellation: when the test pushes a `_Pause` sentinel onto the
    response queue, `complete()` will await an asyncio.Event before returning
    so the test can race a disconnect against the in-flight call.
    """

    name = "anthropic"

    def __init__(self) -> None:
        self.requests: list[CanonicalRequest] = []
        self._responses: list = []
        self._stream_responses: list = []
        import asyncio

        self._asyncio = asyncio
        self._in_flight: dict[str, asyncio.Task] = {}
        self.cancel_calls: list[str] = []

    def push_response(
        self,
        text: str = "ok",
        *,
        input_tokens: int = 10,
        output_tokens: int = 5,
    ) -> None:
        self._responses.append(
            _ScheduledResponse(text=text, input_tokens=input_tokens, output_tokens=output_tokens)
        )

    def push_blocks_response(
        self,
        blocks: list[ContentBlock],
        *,
        stop_reason: StopReason = StopReason.END_TURN,
        input_tokens: int = 10,
        output_tokens: int = 5,
    ) -> None:
        """Queue a non-streaming response with arbitrary canonical content
        blocks. Used by tests that exercise the lossless block round-trip
        (thinking, tool_use, redacted_thinking, etc.)."""
        self._responses.append(
            _ScheduledBlocksResponse(
                blocks=list(blocks),
                stop_reason=stop_reason,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )

    def push_pause(self) -> _Pause:
        pause = _Pause(event=self._asyncio.Event())
        self._responses.append(pause)
        return pause

    def push_stream_response(
        self,
        text_deltas: list[str] | None = None,
        *,
        tool_calls: list[dict] | None = None,
        input_tokens: int = 10,
        output_tokens: int = 5,
        stop_reason: StopReason = StopReason.END_TURN,
    ) -> None:
        """Queue a scripted streaming response.

        `tool_calls` is a list of `{"id": str, "name": str, "arg_chunks":
        list[str], "final_input": dict}` dicts; one tool_use is streamed per
        entry."""
        self._stream_responses.append(
            _ScheduledStream(
                text_deltas=list(text_deltas or []),
                tool_calls=list(tool_calls or []),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                stop_reason=stop_reason,
            )
        )

    def push_stream_pause(self) -> _Pause:
        """Insert a pause sentinel into the stream queue.

        When the adapter's `stream()` reaches this entry it awaits the event,
        letting the test race a disconnect against an in-flight stream."""
        pause = _Pause(event=self._asyncio.Event())
        self._stream_responses.append(pause)
        return pause

    def capabilities_for(self, model: str) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_thinking=False,
            supports_images=True,
            supports_tools=True,
            supports_system_prompt=True,
            supports_structured_output=True,
            supports_streaming=True,
            supports_streaming_tool_calls=True,
            supports_parallel_tool_calls=True,
            supports_prompt_caching=True,
            supports_system_messages_in_list=False,
            max_context_tokens=200_000,
            max_output_tokens=8192,
        )

    def estimate_input_tokens(self, messages, tools, system_prompt) -> int:
        return 100

    async def complete(self, request: CanonicalRequest) -> CanonicalResponse:
        self.requests.append(request)
        task = self._asyncio.current_task()
        if task is not None:
            self._in_flight[request.request_id] = task
        try:
            if not self._responses:
                raise AssertionError("scripted adapter ran out of responses")
            entry = self._responses.pop(0)
            if isinstance(entry, _Pause):
                await entry.event.wait()
                if not self._responses:
                    raise AssertionError("paused adapter has no follow-up response")
                entry = self._responses.pop(0)
            if isinstance(entry, _ScheduledBlocksResponse):
                return CanonicalResponse(
                    request_id=request.request_id,
                    model=request.model,
                    provider=self.name,
                    content=entry.blocks,
                    stop_reason=entry.stop_reason,
                    usage=TokenUsage(
                        input_tokens=entry.input_tokens,
                        output_tokens=entry.output_tokens,
                    ),
                    latency_ms=42,
                )
            assert isinstance(entry, _ScheduledResponse)
            return CanonicalResponse(
                request_id=request.request_id,
                model=request.model,
                provider=self.name,
                content=[TextBlock(text=entry.text)],
                stop_reason=StopReason.END_TURN,
                usage=TokenUsage(
                    input_tokens=entry.input_tokens,
                    output_tokens=entry.output_tokens,
                ),
                latency_ms=42,
            )
        finally:
            self._in_flight.pop(request.request_id, None)

    async def stream(self, request: CanonicalRequest) -> AsyncIterator[StreamingEvent]:
        self.requests.append(request)
        task = self._asyncio.current_task()
        if task is not None:
            self._in_flight[request.request_id] = task
        try:
            if not self._stream_responses:
                raise AssertionError("scripted adapter has no streaming responses queued")
            entry = self._stream_responses.pop(0)
            if isinstance(entry, _Pause):
                await entry.event.wait()
                if not self._stream_responses:
                    raise AssertionError("paused stream has no follow-up scheduled")
                entry = self._stream_responses.pop(0)
            assert isinstance(entry, _ScheduledStream)
            from metis.core.canonical.ids import new_message_id

            message_id = new_message_id()
            yield MessageStart(message_id=message_id, model=request.model)
            block_index = 0
            final_content: list = []
            if entry.text_deltas:
                joined = ""
                for chunk in entry.text_deltas:
                    yield TextDelta(
                        message_id=message_id,
                        content_block_index=block_index,
                        text=chunk,
                    )
                    joined += chunk
                final_content.append(TextBlock(text=joined))
                block_index += 1
            for spec in entry.tool_calls:
                yield ToolUseStart(
                    message_id=message_id,
                    content_block_index=block_index,
                    tool_use_id=spec["id"],
                    tool_name=spec["name"],
                )
                for arg_chunk in spec.get("arg_chunks", []):
                    yield ToolUseInputDelta(
                        message_id=message_id,
                        content_block_index=block_index,
                        tool_use_id=spec["id"],
                        partial_json=arg_chunk,
                    )
                yield ToolUseEnd(
                    message_id=message_id,
                    content_block_index=block_index,
                    tool_use_id=spec["id"],
                    final_input=spec.get("final_input", {}),
                )
                final_content.append(
                    ToolUseBlock(
                        id=spec["id"],
                        name=spec["name"],
                        input=spec.get("final_input", {}),
                    )
                )
                block_index += 1
            yield MessageComplete(
                message_id=message_id,
                stop_reason=entry.stop_reason,
                final_content=final_content,
                usage=TokenUsage(
                    input_tokens=entry.input_tokens,
                    output_tokens=entry.output_tokens,
                ),
                latency_ms=21,
            )
        finally:
            self._in_flight.pop(request.request_id, None)

    async def cancel(self, request_id: str) -> bool:
        self.cancel_calls.append(request_id)
        task = self._in_flight.get(request_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def close(self) -> None:
        return


class _Pause:
    def __init__(self, *, event) -> None:
        self.event = event


class _ScheduledResponse:
    def __init__(self, *, text: str, input_tokens: int, output_tokens: int) -> None:
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _ScheduledBlocksResponse:
    def __init__(
        self,
        *,
        blocks: list,
        stop_reason: StopReason,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        self.blocks = blocks
        self.stop_reason = stop_reason
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _ScheduledStream:
    def __init__(
        self,
        *,
        text_deltas: list[str],
        tool_calls: list[dict],
        input_tokens: int,
        output_tokens: int,
        stop_reason: StopReason,
    ) -> None:
        self.text_deltas = text_deltas
        self.tool_calls = tool_calls
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.stop_reason = stop_reason


@pytest.fixture
def scripted_adapter() -> ScriptedAdapter:
    return ScriptedAdapter()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")
    return tmp_path


@pytest.fixture
def bearer_token() -> str:
    return "gw_test_token_001"


@pytest.fixture
def keystore(bearer_token: str, workspace: Path) -> Keystore:
    secret_hash = hashlib.sha256(bearer_token.encode("utf-8")).hexdigest()
    key = GatewayKey(
        key_id="gk_test_001",
        secret_hash=secret_hash,
        name="test",
        workspace_path=str(workspace),
    )
    return Keystore([key])


@pytest.fixture
def revoked_bearer_token() -> str:
    return "gw_revoked_token_001"


@pytest.fixture
async def revoked_runtime(
    tmp_path: Path,
    scripted_adapter: ScriptedAdapter,
    revoked_bearer_token: str,
    workspace: Path,
) -> GatewayRuntime:
    """Runtime with a single keystore entry whose `status == 'revoked'`.

    Exercises the gateway.md §11 auth path: requests authenticating with
    this key resolve to a GatewayKey but `is_active` returns False, so the
    middleware short-circuits with the documented `key_revoked` body
    before any harness/routing call.
    """
    secret_hash = hashlib.sha256(revoked_bearer_token.encode("utf-8")).hexdigest()
    revoked_at = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    key = GatewayKey(
        key_id="gk_revoked_001",
        secret_hash=secret_hash,
        name="revoked-test",
        workspace_path=str(workspace),
        status="revoked",
        revoked_at=revoked_at,
    )
    keystore = Keystore([key])
    bus = EventBus()
    bus.start()
    db_file = tmp_path / "revoked-gateway.db"
    trace = TraceStore(db_file)
    trace.attach_to(bus)
    registry = ModelRegistry()
    registry.register(
        model_id="anthropic:claude-haiku-4-5",
        adapter=scripted_adapter,
        aliases=["haiku"],
    )
    registry.register(
        model_id="anthropic:claude-sonnet-4-6",
        adapter=scripted_adapter,
        aliases=["sonnet"],
    )
    routing = RoutingEngine(registry=registry, bus=bus, policy=EMPTY_POLICY)
    rt = GatewayRuntime(
        bus=bus,
        trace=trace,
        registry=registry,
        routing=routing,
        pricing=DEFAULT_PRICE_TABLE,
        keystore=keystore,
        adapters=[scripted_adapter],
        db_file=db_file,
        global_default_model="anthropic:claude-sonnet-4-6",
    )
    yield rt
    await bus.drain()
    await bus.stop()
    trace.close()


@pytest.fixture
async def revoked_client(revoked_runtime: GatewayRuntime):
    """httpx client bound to the revoked-keystore app for §11 tests."""
    import httpx
    from metis.gateway.app import build_app

    app = build_app(revoked_runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture
async def runtime(
    tmp_path: Path,
    scripted_adapter: ScriptedAdapter,
    keystore: Keystore,
) -> GatewayRuntime:
    bus = EventBus()
    bus.start()
    db_file = tmp_path / "gateway.db"
    trace = TraceStore(db_file)
    trace.attach_to(bus)

    registry = ModelRegistry()
    registry.register(
        model_id="anthropic:claude-sonnet-4-6",
        adapter=scripted_adapter,
        aliases=["sonnet"],
    )
    registry.register(
        model_id="anthropic:claude-haiku-4-5",
        adapter=scripted_adapter,
        aliases=["haiku"],
    )
    routing = RoutingEngine(registry=registry, bus=bus, policy=EMPTY_POLICY)
    quota_tracker = QuotaTracker(db_file)
    rt = GatewayRuntime(
        bus=bus,
        trace=trace,
        registry=registry,
        routing=routing,
        pricing=DEFAULT_PRICE_TABLE,
        keystore=keystore,
        adapters=[scripted_adapter],
        db_file=db_file,
        global_default_model="anthropic:claude-sonnet-4-6",
        quota_tracker=quota_tracker,
    )
    yield rt
    await bus.drain()
    await bus.stop()
    trace.close()
    quota_tracker.close()
