"""End-to-end tests for AnthropicAdapter using a mocked SDK client."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from metis.core.adapters.anthropic import AnthropicAdapter
from metis.core.adapters.errors import (
    AuthError,
    CancelledError,
    RateLimitError,
    ServerError,
)
from metis.core.adapters.protocol import CanonicalRequest, StopReason
from metis.core.adapters.retry import RetryPolicy
from metis.core.adapters.tool_id_map import ToolIdMap
from metis.core.canonical.content import TextBlock
from metis.core.canonical.messages import Message, MessageMetadata, Role

# ---- SDK stubs ---------------------------------------------------------


class _FakeUsage:
    def __init__(self, input_tokens=10, output_tokens=5, cached=0, cache_creation=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cached
        self.cache_creation_input_tokens = cache_creation


def _fake_text_response(text: str = "hi"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=_FakeUsage(),
    )


class _FakeMessagesClient:
    """Minimal stand-in for `anthropic.AsyncAnthropic.messages`."""

    def __init__(self, *, responses=None, errors=None):
        self.responses = list(responses or [])
        self.errors = list(errors or [])
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.errors:
            err = self.errors.pop(0)
            if err is not None:
                raise err
        if not self.responses:
            return _fake_text_response()
        return self.responses.pop(0)


class _FakeClient:
    def __init__(self, messages=None):
        self.messages = messages or _FakeMessagesClient()
        self.closed = False

    async def close(self):
        self.closed = True


def _user_request(text: str = "hi") -> CanonicalRequest:
    return CanonicalRequest(
        request_id="req_1",
        messages=[
            Message(
                id="01HZ",
                session_id="s",
                role=Role.USER,
                content=[TextBlock(text=text)],
                created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
                metadata=MessageMetadata(),
            )
        ],
        tools=[],
        system_prompt=None,
        model="anthropic:claude-sonnet-4-6",
        max_output_tokens=128,
        tool_id_map=ToolIdMap(),
    )


def _make_status_error(status: int, body: dict | None = None, retry_after: str | None = None):
    """Build an anthropic.APIStatusError with a fake response."""
    headers = {"retry-after": retry_after} if retry_after else {}
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(
        status_code=status,
        headers=headers,
        json=body or {},
        request=request,
    )
    return anthropic.APIStatusError(
        message=f"status {status}",
        response=response,
        body=body,
    )


# ---- Happy path --------------------------------------------------------


async def test_complete_happy_path():
    fake = _FakeMessagesClient(responses=[_fake_text_response("hello back")])
    client = _FakeClient(messages=fake)
    adapter = AnthropicAdapter(client=client)

    response = await adapter.complete(_user_request("hi"))

    assert response.request_id == "req_1"
    assert response.provider == "anthropic"
    assert response.model == "anthropic:claude-sonnet-4-6"
    assert response.stop_reason == StopReason.END_TURN
    assert len(response.content) == 1
    assert isinstance(response.content[0], TextBlock)
    assert response.content[0].text == "hello back"
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 5
    assert response.latency_ms >= 0

    # Wire model stripped of prefix.
    assert fake.calls[0]["model"] == "claude-sonnet-4-6"


async def test_complete_includes_tools_when_provided():
    from metis.core.canonical.tools import SideEffects, ToolDefinition

    fake = _FakeMessagesClient(responses=[_fake_text_response()])
    adapter = AnthropicAdapter(client=_FakeClient(messages=fake))
    request = _user_request()
    request.tools = [
        ToolDefinition(
            name="read_file",
            description="reads",
            input_schema={"type": "object"},
            side_effects=SideEffects.READ,
        )
    ]
    await adapter.complete(request)
    assert "tools" in fake.calls[0]
    assert fake.calls[0]["tools"][0]["name"] == "read_file"


async def test_complete_includes_system_when_set():
    fake = _FakeMessagesClient(responses=[_fake_text_response()])
    adapter = AnthropicAdapter(client=_FakeClient(messages=fake))
    request = _user_request()
    request.system_prompt = "you are helpful"
    await adapter.complete(request)
    # System is sent as a typed-block list (so the cache breakpoint can
    # ride on the last stable block, see context-assembler.md §3).
    system_blocks = fake.calls[0]["system"]
    assert system_blocks == [
        {
            "type": "text",
            "text": "you are helpful",
            "cache_control": {"type": "ephemeral"},
        }
    ]


# ---- Token usage mapping -----------------------------------------------


async def test_cache_read_mapped_to_cached_input():
    fake = _FakeMessagesClient(
        responses=[
            SimpleNamespace(
                content=[SimpleNamespace(type="text", text="x")],
                stop_reason="end_turn",
                usage=_FakeUsage(cached=100, cache_creation=50),
            )
        ]
    )
    adapter = AnthropicAdapter(client=_FakeClient(messages=fake))
    response = await adapter.complete(_user_request())
    assert response.usage.cached_input_tokens == 100
    assert response.usage.cache_creation_input_tokens == 50


# ---- Error translation -------------------------------------------------


async def test_complete_translates_rate_limit_with_retry_after():
    # All retries return rate_limit so final raise carries through.
    fake = _FakeMessagesClient(
        errors=[
            _make_status_error(429, {"error": {"type": "rate_limit_error"}}, retry_after="0"),
            _make_status_error(429, {"error": {"type": "rate_limit_error"}}, retry_after="0"),
            _make_status_error(429, {"error": {"type": "rate_limit_error"}}, retry_after="0"),
        ]
    )
    adapter = AnthropicAdapter(
        client=_FakeClient(messages=fake),
        retry_policy=RetryPolicy(max_retries=2),
    )
    with pytest.raises(RateLimitError):
        await adapter.complete(_user_request())


async def test_complete_translates_auth_error_not_retryable():
    fake = _FakeMessagesClient(
        errors=[_make_status_error(401, {"error": {"type": "authentication_error"}})]
    )
    adapter = AnthropicAdapter(client=_FakeClient(messages=fake))
    with pytest.raises(AuthError):
        await adapter.complete(_user_request())
    # Only one call — non-retryable.
    assert len(fake.calls) == 1


async def test_complete_translates_overloaded_529_as_rate_limit():
    fake = _FakeMessagesClient(
        errors=[
            _make_status_error(529, {"error": {"type": "overloaded_error"}}, retry_after="0"),
            None,  # second attempt succeeds
        ],
        responses=[_fake_text_response("ok")],
    )
    adapter = AnthropicAdapter(
        client=_FakeClient(messages=fake), retry_policy=RetryPolicy(max_retries=2)
    )
    response = await adapter.complete(_user_request())
    assert response.content[0].text == "ok"
    # 1 failure + 1 success = 2 calls
    assert len(fake.calls) == 2


async def test_complete_retries_server_error_then_succeeds():
    fake = _FakeMessagesClient(
        errors=[_make_status_error(500, {"error": {"type": "api_error"}}), None],
        responses=[_fake_text_response("recovered")],
    )
    adapter = AnthropicAdapter(
        client=_FakeClient(messages=fake), retry_policy=RetryPolicy(max_retries=2)
    )
    # Force backoff to zero for test speed.
    adapter._retry_policy = RetryPolicy(max_retries=2, base_backoff_seconds=0.0, jitter_factor=0.0)
    response = await adapter.complete(_user_request())
    assert response.content[0].text == "recovered"


async def test_complete_raises_server_error_after_exhausting_retries():
    fake = _FakeMessagesClient(
        errors=[
            _make_status_error(500, {"error": {"type": "api_error"}}),
            _make_status_error(500, {"error": {"type": "api_error"}}),
            _make_status_error(500, {"error": {"type": "api_error"}}),
        ]
    )
    adapter = AnthropicAdapter(
        client=_FakeClient(messages=fake),
        retry_policy=RetryPolicy(max_retries=2, base_backoff_seconds=0.0, jitter_factor=0.0),
    )
    with pytest.raises(ServerError):
        await adapter.complete(_user_request())
    assert len(fake.calls) == 3  # 1 + max_retries


# ---- Cancellation ------------------------------------------------------


async def test_cancel_during_complete_raises_cancelled_error():
    # Stub messages.create to sleep so we can cancel mid-flight.
    async def slow_create(**kwargs):
        await asyncio.sleep(5.0)
        return _fake_text_response()

    fake = _FakeMessagesClient()
    fake.create = slow_create  # type: ignore[assignment]
    adapter = AnthropicAdapter(client=_FakeClient(messages=fake))

    task = asyncio.create_task(adapter.complete(_user_request()))
    await asyncio.sleep(0.05)
    cancelled = await adapter.cancel("req_1")
    assert cancelled is True

    with pytest.raises(CancelledError):
        await task


async def test_cancel_returns_false_for_unknown_request():
    adapter = AnthropicAdapter(client=_FakeClient())
    assert await adapter.cancel("never_started") is False


# ---- Capabilities ------------------------------------------------------


def test_capabilities_for_known_model():
    adapter = AnthropicAdapter(client=_FakeClient())
    caps = adapter.capabilities_for("anthropic:claude-sonnet-4-6")
    assert caps.supports_tools is True
    assert caps.supports_images is True
    assert caps.supports_system_messages_in_list is False  # hoisted


def test_capabilities_for_unknown_model_raises():
    adapter = AnthropicAdapter(client=_FakeClient())
    with pytest.raises(ValueError):
        adapter.capabilities_for("unknown:model")


# ---- estimate_input_tokens --------------------------------------------


def test_estimate_input_tokens_is_positive():
    adapter = AnthropicAdapter(client=_FakeClient())
    msg = Message(
        id="01HZ",
        session_id="s",
        role=Role.USER,
        content=[TextBlock(text="hello world")],
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    est = adapter.estimate_input_tokens([msg], [], "be helpful")
    assert est > 0
    # 11 chars + 11 chars system + small constant; ~5-10 tokens
    assert est < 100


# ---- Prompt-cache breakpoint placement (context-assembler.md §3) -------


async def test_volatile_system_appended_after_stable_breakpoint():
    """Two-segment system prompt → stable block carries cache_control,
    volatile block trails it. The cache prefix covers tools + stable system.
    """
    fake = _FakeMessagesClient(responses=[_fake_text_response()])
    adapter = AnthropicAdapter(client=_FakeClient(messages=fake))
    request = _user_request()
    request.system_prompt = "stable instructions"
    request.system_prompt_volatile = "## MEMORY.md\nuser likes elixir"
    await adapter.complete(request)

    system_blocks = fake.calls[0]["system"]
    assert system_blocks == [
        {
            "type": "text",
            "text": "stable instructions",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "## MEMORY.md\nuser likes elixir"},
    ]


async def test_only_volatile_system_still_emits_just_the_volatile_block():
    """When stable text is empty, only the volatile block is sent — no
    cache_control marker on a volatile block (the spec forbids it)."""
    fake = _FakeMessagesClient(responses=[_fake_text_response()])
    adapter = AnthropicAdapter(client=_FakeClient(messages=fake))
    request = _user_request()
    request.system_prompt = None
    request.system_prompt_volatile = "memory only"
    await adapter.complete(request)

    system_blocks = fake.calls[0]["system"]
    assert system_blocks == [{"type": "text", "text": "memory only"}]


async def test_no_system_segments_omits_kwarg_entirely():
    fake = _FakeMessagesClient(responses=[_fake_text_response()])
    adapter = AnthropicAdapter(client=_FakeClient(messages=fake))
    request = _user_request()
    request.system_prompt = None
    request.system_prompt_volatile = None
    await adapter.complete(request)
    assert "system" not in fake.calls[0]


async def test_last_tool_carries_cache_control():
    """Cache_control on the last tool def caches the entire tools section
    in Anthropic's cache-prefix walk (context-assembler.md §3)."""
    from metis.core.canonical.tools import SideEffects, ToolDefinition

    fake = _FakeMessagesClient(responses=[_fake_text_response()])
    adapter = AnthropicAdapter(client=_FakeClient(messages=fake))
    request = _user_request()
    request.tools = [
        ToolDefinition(
            name="read_file",
            description="reads",
            input_schema={"type": "object"},
            side_effects=SideEffects.READ,
        ),
        ToolDefinition(
            name="write_file",
            description="writes",
            input_schema={"type": "object"},
            side_effects=SideEffects.WRITE,
        ),
    ]
    await adapter.complete(request)
    wire_tools = fake.calls[0]["tools"]
    # Earlier tools have no cache_control, last one does.
    assert "cache_control" not in wire_tools[0]
    assert wire_tools[-1]["cache_control"] == {"type": "ephemeral"}


async def test_single_tool_carries_cache_control():
    from metis.core.canonical.tools import SideEffects, ToolDefinition

    fake = _FakeMessagesClient(responses=[_fake_text_response()])
    adapter = AnthropicAdapter(client=_FakeClient(messages=fake))
    request = _user_request()
    request.tools = [
        ToolDefinition(
            name="read_file",
            description="reads",
            input_schema={"type": "object"},
            side_effects=SideEffects.READ,
        )
    ]
    await adapter.complete(request)
    assert fake.calls[0]["tools"][0]["cache_control"] == {"type": "ephemeral"}


# ---- file_ref ImageBlock resolution (KNOWN_ISSUES carryover) -----------


async def test_file_ref_image_resolved_through_workspace_file_api(tmp_path):
    """Previously the adapter stuffed the workspace-relative path string
    into the base64 `data` field, sending garbled bytes to Anthropic. The
    fix reads via WorkspaceFileAPI, base64-encodes, and infers media type
    from the extension."""
    import base64
    from datetime import UTC, datetime

    from metis.core.canonical.content import ImageBlock, ImageSource

    image_bytes = b"\x89PNG\r\n\x1a\nfake-png-bytes"
    (tmp_path / "shot.png").write_bytes(image_bytes)

    fake = _FakeMessagesClient(responses=[_fake_text_response()])
    adapter = AnthropicAdapter(client=_FakeClient(messages=fake))
    request = CanonicalRequest(
        request_id="req_img",
        messages=[
            Message(
                id="01HZ",
                session_id="s",
                role=Role.USER,
                content=[
                    TextBlock(text="here"),
                    ImageBlock(
                        source=ImageSource(kind="file_ref", data="shot.png"),
                        media_type="",  # adapter must infer
                    ),
                ],
                created_at=datetime.now(UTC),
            )
        ],
        tools=[],
        system_prompt=None,
        model="anthropic:claude-sonnet-4-6",
        max_output_tokens=128,
        workspace_path=str(tmp_path),
    )
    await adapter.complete(request)

    wire_messages = fake.calls[0]["messages"]
    image_block = wire_messages[0]["content"][1]
    assert image_block["type"] == "image"
    assert image_block["source"]["type"] == "base64"
    assert image_block["source"]["media_type"] == "image/png"  # inferred from .png
    decoded = base64.b64decode(image_block["source"]["data"])
    assert decoded == image_bytes


async def test_file_ref_image_uses_explicit_media_type_when_set(tmp_path):
    """When ImageBlock.media_type is set, it overrides the extension-based
    inference."""
    from datetime import UTC, datetime

    from metis.core.canonical.content import ImageBlock, ImageSource

    (tmp_path / "shot.bin").write_bytes(b"\xff\xd8\xff\xe0jpegdata")

    fake = _FakeMessagesClient(responses=[_fake_text_response()])
    adapter = AnthropicAdapter(client=_FakeClient(messages=fake))
    request = CanonicalRequest(
        request_id="req_img2",
        messages=[
            Message(
                id="01HZ",
                session_id="s",
                role=Role.USER,
                content=[
                    ImageBlock(
                        source=ImageSource(kind="file_ref", data="shot.bin"),
                        media_type="image/jpeg",
                    ),
                ],
                created_at=datetime.now(UTC),
            )
        ],
        tools=[],
        system_prompt=None,
        model="anthropic:claude-sonnet-4-6",
        max_output_tokens=128,
        workspace_path=str(tmp_path),
    )
    await adapter.complete(request)
    image_block = fake.calls[0]["messages"][0]["content"][0]
    assert image_block["source"]["media_type"] == "image/jpeg"


async def test_file_ref_image_dropped_when_no_workspace_path(tmp_path):
    """Without workspace context, file_ref images are dropped with a WARN
    rather than sent as garbled payload (canonical-format §7.3)."""
    from datetime import UTC, datetime

    from metis.core.canonical.content import ImageBlock, ImageSource

    fake = _FakeMessagesClient(responses=[_fake_text_response()])
    adapter = AnthropicAdapter(client=_FakeClient(messages=fake))
    request = CanonicalRequest(
        request_id="req_img3",
        messages=[
            Message(
                id="01HZ",
                session_id="s",
                role=Role.USER,
                content=[
                    TextBlock(text="surrounding text"),
                    ImageBlock(
                        source=ImageSource(kind="file_ref", data="missing.png"),
                        media_type="image/png",
                    ),
                ],
                created_at=datetime.now(UTC),
            )
        ],
        tools=[],
        system_prompt=None,
        model="anthropic:claude-sonnet-4-6",
        max_output_tokens=128,
        # workspace_path intentionally omitted
    )
    await adapter.complete(request)
    # Image dropped; only the surrounding text survives.
    contents = fake.calls[0]["messages"][0]["content"]
    assert all(c["type"] != "image" for c in contents)
    assert any(c["type"] == "text" and c["text"] == "surrounding text" for c in contents)


async def test_file_ref_image_dropped_on_workspace_escape(tmp_path):
    """`..` path escape is rejected by WorkspaceFileAPI; image is dropped
    rather than reaching the wire (workspace path security is load-bearing,
    AGENTS.md "Implementation conventions")."""
    from datetime import UTC, datetime

    from metis.core.canonical.content import ImageBlock, ImageSource

    fake = _FakeMessagesClient(responses=[_fake_text_response()])
    adapter = AnthropicAdapter(client=_FakeClient(messages=fake))
    request = CanonicalRequest(
        request_id="req_img4",
        messages=[
            Message(
                id="01HZ",
                session_id="s",
                role=Role.USER,
                content=[
                    TextBlock(text="text"),
                    ImageBlock(
                        source=ImageSource(kind="file_ref", data="../../etc/passwd"),
                        media_type="image/png",
                    ),
                ],
                created_at=datetime.now(UTC),
            )
        ],
        tools=[],
        system_prompt=None,
        model="anthropic:claude-sonnet-4-6",
        max_output_tokens=128,
        workspace_path=str(tmp_path),
    )
    await adapter.complete(request)
    contents = fake.calls[0]["messages"][0]["content"]
    assert all(c["type"] != "image" for c in contents)


async def test_file_ref_image_dropped_when_file_missing(tmp_path):
    from datetime import UTC, datetime

    from metis.core.canonical.content import ImageBlock, ImageSource

    fake = _FakeMessagesClient(responses=[_fake_text_response()])
    adapter = AnthropicAdapter(client=_FakeClient(messages=fake))
    request = CanonicalRequest(
        request_id="req_img5",
        messages=[
            Message(
                id="01HZ",
                session_id="s",
                role=Role.USER,
                content=[
                    TextBlock(text="text"),
                    ImageBlock(
                        source=ImageSource(kind="file_ref", data="does-not-exist.png"),
                        media_type="image/png",
                    ),
                ],
                created_at=datetime.now(UTC),
            )
        ],
        tools=[],
        system_prompt=None,
        model="anthropic:claude-sonnet-4-6",
        max_output_tokens=128,
        workspace_path=str(tmp_path),
    )
    await adapter.complete(request)
    contents = fake.calls[0]["messages"][0]["content"]
    assert all(c["type"] != "image" for c in contents)


def test_media_type_inference_from_extension():
    from metis.core.adapters.anthropic import _media_type_from_path

    assert _media_type_from_path("foo.png") == "image/png"
    assert _media_type_from_path("foo.PNG") == "image/png"
    assert _media_type_from_path("dir/sub/foo.jpg") == "image/jpeg"
    assert _media_type_from_path("foo.jpeg") == "image/jpeg"
    assert _media_type_from_path("foo.gif") == "image/gif"
    assert _media_type_from_path("foo.webp") == "image/webp"
    # Unknown extension defaults to image/png.
    assert _media_type_from_path("foo.bmp") == "image/png"
    assert _media_type_from_path("foo") == "image/png"
