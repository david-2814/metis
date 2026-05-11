"""End-to-end tests for OpenAIAdapter using a mocked SDK client."""

from __future__ import annotations

import asyncio
import datetime
from types import SimpleNamespace

import httpx
import openai
import pytest

from metis.adapters.errors import AuthError, CancelledError, RateLimitError, ServerError
from metis.adapters.openai import OpenAIAdapter
from metis.adapters.protocol import CanonicalRequest, StopReason
from metis.adapters.retry import RetryPolicy
from metis.adapters.tool_id_map import ToolIdMap
from metis.canonical.content import TextBlock, ToolUseBlock
from metis.canonical.messages import Message, MessageMetadata, Role

# ---- SDK stubs ---------------------------------------------------------


class _FakeUsage:
    def __init__(self, prompt=10, completion=5, cached=0):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.prompt_tokens_details = SimpleNamespace(cached_tokens=cached)


def _fake_text_response(text: str = "hi"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=text, tool_calls=None),
            )
        ],
        usage=_FakeUsage(),
    )


def _fake_tool_call_response(
    *, call_id: str = "call_xyz", name: str = "read_file", args: str = '{"path":"x"}'
):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id=call_id,
                            type="function",
                            function=SimpleNamespace(name=name, arguments=args),
                        )
                    ],
                ),
            )
        ],
        usage=_FakeUsage(),
    )


class _FakeCompletions:
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


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeClient:
    def __init__(self, completions: _FakeCompletions | None = None) -> None:
        self.chat = _FakeChat(completions or _FakeCompletions())
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _user_request(text: str = "hi", *, model: str = "openai:gpt-5") -> CanonicalRequest:
    return CanonicalRequest(
        request_id="req_1",
        messages=[
            Message(
                id="01HZ",
                session_id="s",
                role=Role.USER,
                content=[TextBlock(text=text)],
                created_at=datetime.datetime.now(datetime.UTC),
                metadata=MessageMetadata(),
            )
        ],
        tools=[],
        system_prompt=None,
        model=model,
        max_output_tokens=128,
        tool_id_map=ToolIdMap(),
    )


def _make_status_error(status: int, body: dict | None = None, retry_after: str | None = None):
    """Construct an openai.APIStatusError with a fake httpx response."""
    headers = {"retry-after": retry_after} if retry_after else {}
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status_code=status, headers=headers, json=body or {}, request=request)
    return openai.APIStatusError(message=f"status {status}", response=response, body=body)


# ---- Happy path --------------------------------------------------------


async def test_complete_text_happy_path():
    fake = _FakeCompletions(responses=[_fake_text_response("hello back")])
    adapter = OpenAIAdapter(client=_FakeClient(fake))
    response = await adapter.complete(_user_request())
    assert response.provider == "openai"
    assert response.model == "openai:gpt-5"
    assert response.stop_reason == StopReason.END_TURN
    assert response.content == [TextBlock(text="hello back")]
    # Wire model prefix stripped.
    assert fake.calls[0]["model"] == "gpt-5"


async def test_complete_includes_max_completion_tokens():
    fake = _FakeCompletions(responses=[_fake_text_response()])
    adapter = OpenAIAdapter(client=_FakeClient(fake))
    req = _user_request()
    req.max_output_tokens = 999
    await adapter.complete(req)
    assert fake.calls[0]["max_completion_tokens"] == 999


async def test_complete_tool_call_records_id_mapping():
    fake = _FakeCompletions(
        responses=[_fake_tool_call_response(call_id="call_xyz", name="t", args="{}")]
    )
    adapter = OpenAIAdapter(client=_FakeClient(fake))
    req = _user_request()
    response = await adapter.complete(req)
    assert response.stop_reason == StopReason.TOOL_USE
    assert isinstance(response.content[0], ToolUseBlock)
    canonical_id = response.content[0].id
    # The request's tool_id_map should now contain the round-trip mapping.
    assert req.tool_id_map.to_canonical("call_xyz") == canonical_id
    assert req.tool_id_map.to_provider(canonical_id) == "call_xyz"


async def test_response_format_passed_when_output_schema_set():
    fake = _FakeCompletions(responses=[_fake_text_response()])
    adapter = OpenAIAdapter(client=_FakeClient(fake))
    req = _user_request()
    req.output_schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    await adapter.complete(req)
    rf = fake.calls[0]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"] == req.output_schema
    assert rf["json_schema"]["strict"] is True


# ---- Error translation -------------------------------------------------


async def test_rate_limit_eventually_raises():
    fake = _FakeCompletions(
        errors=[
            _make_status_error(429, {"error": {"code": "rate_limit_exceeded"}}, retry_after="0"),
            _make_status_error(429, {"error": {"code": "rate_limit_exceeded"}}, retry_after="0"),
            _make_status_error(429, {"error": {"code": "rate_limit_exceeded"}}, retry_after="0"),
        ]
    )
    adapter = OpenAIAdapter(client=_FakeClient(fake), retry_policy=RetryPolicy(max_retries=2))
    with pytest.raises(RateLimitError):
        await adapter.complete(_user_request())


async def test_auth_error_not_retried():
    fake = _FakeCompletions(
        errors=[_make_status_error(401, {"error": {"code": "invalid_api_key"}})]
    )
    adapter = OpenAIAdapter(client=_FakeClient(fake))
    with pytest.raises(AuthError):
        await adapter.complete(_user_request())
    assert len(fake.calls) == 1


async def test_context_length_promoted_from_400():
    fake = _FakeCompletions(
        errors=[_make_status_error(400, {"error": {"code": "context_length_exceeded"}})]
    )
    adapter = OpenAIAdapter(client=_FakeClient(fake))
    from metis.adapters.errors import ContextOverflowError

    with pytest.raises(ContextOverflowError):
        await adapter.complete(_user_request())


async def test_server_error_retried_then_succeeds():
    fake = _FakeCompletions(
        errors=[_make_status_error(500, {"error": {"type": "server_error"}}), None],
        responses=[_fake_text_response("recovered")],
    )
    adapter = OpenAIAdapter(
        client=_FakeClient(fake),
        retry_policy=RetryPolicy(max_retries=2, base_backoff_seconds=0.0, jitter_factor=0.0),
    )
    response = await adapter.complete(_user_request())
    assert response.content[0].text == "recovered"
    assert len(fake.calls) == 2


async def test_server_error_exhausted():
    fake = _FakeCompletions(
        errors=[
            _make_status_error(500, {"error": {"type": "server_error"}}),
            _make_status_error(500, {"error": {"type": "server_error"}}),
            _make_status_error(500, {"error": {"type": "server_error"}}),
        ]
    )
    adapter = OpenAIAdapter(
        client=_FakeClient(fake),
        retry_policy=RetryPolicy(max_retries=2, base_backoff_seconds=0.0, jitter_factor=0.0),
    )
    with pytest.raises(ServerError):
        await adapter.complete(_user_request())
    assert len(fake.calls) == 3


# ---- Cancellation ------------------------------------------------------


async def test_cancel_mid_complete():
    async def slow_create(**kwargs):
        await asyncio.sleep(5.0)
        return _fake_text_response()

    fake = _FakeCompletions()
    fake.create = slow_create  # type: ignore[assignment]
    adapter = OpenAIAdapter(client=_FakeClient(fake))
    task = asyncio.create_task(adapter.complete(_user_request()))
    await asyncio.sleep(0.05)
    cancelled = await adapter.cancel("req_1")
    assert cancelled is True
    with pytest.raises(CancelledError):
        await task


async def test_cancel_unknown_request_returns_false():
    adapter = OpenAIAdapter(client=_FakeClient())
    assert await adapter.cancel("nope") is False


# ---- Capabilities ------------------------------------------------------


def test_capabilities_for_gpt5():
    adapter = OpenAIAdapter(client=_FakeClient())
    caps = adapter.capabilities_for("openai:gpt-5")
    assert caps.supports_tools is True
    assert caps.supports_structured_output is True
    assert caps.supports_system_messages_in_list is True  # not hoisted, unlike Anthropic
    assert caps.supports_thinking is False


def test_capabilities_unknown_model_raises():
    adapter = OpenAIAdapter(client=_FakeClient())
    with pytest.raises(ValueError):
        adapter.capabilities_for("openai:gpt-999")


# ---- Token estimation --------------------------------------------------


def test_estimate_input_tokens_positive():
    adapter = OpenAIAdapter(client=_FakeClient())
    est = adapter.estimate_input_tokens(
        [
            Message(
                id="01HZ",
                session_id="s",
                role=Role.USER,
                content=[TextBlock(text="hello there")],
                created_at=datetime.datetime.now(datetime.UTC),
            )
        ],
        [],
        "be helpful",
    )
    assert est > 0
