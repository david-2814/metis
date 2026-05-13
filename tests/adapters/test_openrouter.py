"""Tests for the OpenRouter adapter and catalog parsing."""

from __future__ import annotations

import asyncio
import datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx
import openai
import pytest

from metis.adapters.errors import AuthError, CancelledError, RateLimitError
from metis.adapters.openrouter import (
    OpenRouterAdapter,
    _parse_capabilities,
    _parse_pricing,
    _wire_model_name,
)
from metis.adapters.protocol import CanonicalRequest, StopReason
from metis.adapters.retry import RetryPolicy
from metis.adapters.tool_id_map import ToolIdMap
from metis.canonical.content import TextBlock, ToolUseBlock
from metis.canonical.messages import Message, MessageMetadata, Role

# ---- SDK stubs (same shape as openai test) ----------------------------


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


def _fake_tool_call_response(call_id: str = "call_or_abc"):
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
                            function=SimpleNamespace(name="t", arguments="{}"),
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

    async def close(self):
        return


def _user_request(model: str = "openrouter:anthropic/claude-sonnet-4") -> CanonicalRequest:
    return CanonicalRequest(
        request_id="req_1",
        messages=[
            Message(
                id="01HZ",
                session_id="s",
                role=Role.USER,
                content=[TextBlock(text="hi")],
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


def _make_status_error(status: int, body: dict | None = None):
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(status_code=status, json=body or {}, request=request)
    return openai.APIStatusError(message=f"status {status}", response=response, body=body)


# ---- Model id stripping -------------------------------------------------


def test_wire_model_name_strips_openrouter_prefix():
    assert _wire_model_name("openrouter:anthropic/claude-sonnet-4") == "anthropic/claude-sonnet-4"
    assert _wire_model_name("anthropic/claude-sonnet-4") == "anthropic/claude-sonnet-4"


# ---- Catalog parsing ---------------------------------------------------


def test_parse_capabilities_text_model():
    entry = {
        "id": "deepseek/deepseek-v3",
        "context_length": 64_000,
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        "supported_parameters": ["max_tokens", "temperature", "tools"],
        "top_provider": {"context_length": 64_000, "max_completion_tokens": 8192},
    }
    caps = _parse_capabilities(entry)
    assert caps.supports_images is False
    assert caps.supports_tools is True
    assert caps.supports_structured_output is False
    assert caps.max_context_tokens == 64_000
    assert caps.max_output_tokens == 8192


def test_parse_capabilities_multimodal():
    entry = {
        "id": "anthropic/claude-sonnet-4",
        "context_length": 200_000,
        "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
        "supported_parameters": ["tools", "response_format"],
        "top_provider": {"context_length": 200_000, "max_completion_tokens": 8192},
    }
    caps = _parse_capabilities(entry)
    assert caps.supports_images is True
    assert caps.supports_structured_output is True
    assert "image/png" in caps.accepted_image_media_types


def test_parse_capabilities_falls_back_when_top_provider_missing():
    entry = {
        "id": "tiny/foo",
        "architecture": {"input_modalities": ["text"]},
        "supported_parameters": [],
    }
    caps = _parse_capabilities(entry)
    # Defaults kick in when fields are missing.
    assert caps.max_context_tokens > 0
    assert caps.max_output_tokens > 0


def test_parse_pricing_converts_per_token_to_per_mtok():
    entry = {
        "pricing": {
            "prompt": "0.000003",  # $3 / Mtok
            "completion": "0.000015",  # $15 / Mtok
        }
    }
    p = _parse_pricing(entry)
    assert p is not None
    assert p.input_per_mtok == Decimal("3")
    assert p.output_per_mtok == Decimal("15")


def test_parse_pricing_with_cached_read():
    entry = {
        "pricing": {
            "prompt": "0.000003",
            "completion": "0.000015",
            "input_cache_read": "0.0000003",  # $0.30 / Mtok
        }
    }
    p = _parse_pricing(entry)
    assert p is not None
    assert p.cached_read_per_mtok == Decimal("0.3")


def test_parse_pricing_returns_none_when_missing():
    assert _parse_pricing({"pricing": {}}) is None
    assert _parse_pricing({}) is None
    assert _parse_pricing({"pricing": {"prompt": "0.000001"}}) is None  # completion missing


# ---- fetch_catalog -----------------------------------------------------


async def test_fetch_catalog_populates_capabilities_and_pricing():
    catalog_payload = {
        "data": [
            {
                "id": "anthropic/claude-sonnet-4",
                "context_length": 200_000,
                "architecture": {
                    "input_modalities": ["text", "image"],
                    "output_modalities": ["text"],
                },
                "supported_parameters": ["tools", "response_format"],
                "top_provider": {"context_length": 200_000, "max_completion_tokens": 8192},
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            },
            {
                "id": "deepseek/deepseek-v3",
                "context_length": 64_000,
                "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                "supported_parameters": ["tools"],
                "top_provider": {"context_length": 64_000, "max_completion_tokens": 4096},
                "pricing": {"prompt": "0.0000003", "completion": "0.0000015"},
            },
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/models"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json=catalog_payload)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        adapter = OpenRouterAdapter(api_key="test-key", client=_FakeClient())
        result = await adapter.fetch_catalog(http_client=http_client)

    assert "openrouter:anthropic/claude-sonnet-4" in result.capabilities
    assert "openrouter:deepseek/deepseek-v3" in result.capabilities
    sonnet_caps = result.capabilities["openrouter:anthropic/claude-sonnet-4"]
    assert sonnet_caps.supports_images is True
    deepseek_caps = result.capabilities["openrouter:deepseek/deepseek-v3"]
    assert deepseek_caps.supports_images is False

    assert result.pricing["openrouter:anthropic/claude-sonnet-4"].input_per_mtok == Decimal("3")
    assert result.pricing["openrouter:deepseek/deepseek-v3"].input_per_mtok == Decimal("0.3")
    assert result.version.startswith("openrouter-")


async def test_fetch_catalog_skips_malformed_entries():
    catalog_payload = {
        "data": [
            {"id": "good/model", "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
            {"pricing": {"prompt": "0.000001"}},  # missing id
            {"id": "broken/pricing", "pricing": {"prompt": "not-a-number"}},
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=catalog_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        adapter = OpenRouterAdapter(api_key="k", client=_FakeClient())
        result = await adapter.fetch_catalog(http_client=http_client)

    # Only "good/model" should have pricing; capabilities entry exists for both
    # capability-parseable models (broken pricing doesn't kill capabilities).
    assert "openrouter:good/model" in result.pricing
    assert "openrouter:broken/pricing" not in result.pricing


# ---- complete() --------------------------------------------------------


async def test_complete_text_happy_path():
    fake = _FakeCompletions(responses=[_fake_text_response("hello back")])
    adapter = OpenRouterAdapter(api_key="k", client=_FakeClient(fake))
    response = await adapter.complete(_user_request())
    assert response.provider == "openrouter"
    assert response.stop_reason == StopReason.END_TURN
    assert response.content == [TextBlock(text="hello back")]
    # Wire model preserves the provider/model form (only `openrouter:` stripped).
    assert fake.calls[0]["model"] == "anthropic/claude-sonnet-4"


async def test_complete_tool_call_records_id_mapping():
    fake = _FakeCompletions(responses=[_fake_tool_call_response(call_id="call_or_xyz")])
    adapter = OpenRouterAdapter(api_key="k", client=_FakeClient(fake))
    req = _user_request()
    response = await adapter.complete(req)
    assert response.stop_reason == StopReason.TOOL_USE
    assert isinstance(response.content[0], ToolUseBlock)
    canonical_id = response.content[0].id
    assert req.tool_id_map.to_canonical("call_or_xyz") == canonical_id
    assert req.tool_id_map.to_provider(canonical_id) == "call_or_xyz"


async def test_complete_translates_rate_limit():
    fake = _FakeCompletions(
        errors=[
            _make_status_error(429, {"error": {"code": "rate_limit_exceeded"}}),
            _make_status_error(429, {"error": {"code": "rate_limit_exceeded"}}),
            _make_status_error(429, {"error": {"code": "rate_limit_exceeded"}}),
        ]
    )
    adapter = OpenRouterAdapter(
        api_key="k",
        client=_FakeClient(fake),
        retry_policy=RetryPolicy(max_retries=2, base_backoff_seconds=0.0, jitter_factor=0.0),
    )
    with pytest.raises(RateLimitError):
        await adapter.complete(_user_request())


async def test_complete_auth_error_not_retried():
    fake = _FakeCompletions(
        errors=[_make_status_error(401, {"error": {"code": "invalid_api_key"}})]
    )
    adapter = OpenRouterAdapter(api_key="k", client=_FakeClient(fake))
    with pytest.raises(AuthError):
        await adapter.complete(_user_request())
    assert len(fake.calls) == 1


# ---- Cancellation ------------------------------------------------------


async def test_cancel_mid_complete():
    async def slow_create(**kwargs):
        await asyncio.sleep(5.0)
        return _fake_text_response()

    fake = _FakeCompletions()
    fake.create = slow_create  # type: ignore[assignment]
    adapter = OpenRouterAdapter(api_key="k", client=_FakeClient(fake))
    task = asyncio.create_task(adapter.complete(_user_request()))
    await asyncio.sleep(0.05)
    cancelled = await adapter.cancel("req_1")
    assert cancelled is True
    with pytest.raises(CancelledError):
        await task


# ---- Capabilities --------------------------------------------------------


async def test_capabilities_for_unknown_model_raises_before_catalog():
    adapter = OpenRouterAdapter(api_key="k", client=_FakeClient())
    with pytest.raises(ValueError):
        adapter.capabilities_for("openrouter:any/model")


async def test_capabilities_available_after_catalog_fetch():
    catalog_payload = {
        "data": [
            {
                "id": "x/y",
                "context_length": 1024,
                "architecture": {"input_modalities": ["text"]},
                "supported_parameters": ["tools"],
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            }
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=catalog_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        adapter = OpenRouterAdapter(api_key="k", client=_FakeClient())
        await adapter.fetch_catalog(http_client=http_client)
        caps = adapter.capabilities_for("openrouter:x/y")
        assert caps.supports_tools is True


# ---- Error metadata surfacing ----------------------------------------
#
# OpenRouter is an aggregator: when an upstream inference provider rejects
# a request, OpenRouter returns a generic 400 ("Provider returned error")
# and stashes the real reason in `error.metadata`. The adapter's
# `_provider_message` helper composes a richer string from those fields so
# the surfaced error tells the user *what* failed and *who* rejected it.


def _composed_message(body: dict) -> str:
    from metis.adapters.openrouter import _provider_message

    return _provider_message(body)


def test_provider_message_plain_no_metadata():
    """No metadata → behaves identically to the original implementation
    (just the top-level message)."""
    body = {"error": {"message": "Bad Request"}}
    assert _composed_message(body) == "Bad Request"


def test_provider_message_appends_provider_name_only():
    """`provider_name` without a raw upstream body → still says who rejected."""
    body = {
        "error": {
            "message": "Provider returned error",
            "metadata": {"provider_name": "Fireworks"},
        }
    }
    assert _composed_message(body) == "Provider returned error (via Fireworks)"


def test_provider_message_extracts_raw_json_string():
    """`metadata.raw` is a JSON-encoded string; we parse it and pull the
    inner upstream message."""
    raw_inner = (
        '{"error":{"message":"Function calling not supported",'
        '"type":"invalid_request_error"}}'
    )
    body = {
        "error": {
            "message": "Provider returned error",
            "metadata": {"raw": raw_inner, "provider_name": "Fireworks"},
        }
    }
    msg = _composed_message(body)
    assert msg == (
        "Provider returned error (Fireworks: Function calling not supported)"
    )


def test_provider_message_handles_raw_as_dict():
    """Some upstream providers pass `raw` through as a dict, not a string."""
    body = {
        "error": {
            "message": "Provider returned error",
            "metadata": {
                "raw": {"error": {"message": "Context length exceeded"}},
                "provider_name": "Together",
            },
        }
    }
    assert _composed_message(body) == (
        "Provider returned error (Together: Context length exceeded)"
    )


def test_provider_message_raw_unparseable_string_used_verbatim():
    """If `raw` isn't valid JSON, surface it as the upstream blurb anyway —
    better than dropping it on the floor."""
    body = {
        "error": {
            "message": "Provider returned error",
            "metadata": {
                "raw": "rate limit: please slow down",
                "provider_name": "DeepInfra",
            },
        }
    }
    assert _composed_message(body) == (
        "Provider returned error (DeepInfra: rate limit: please slow down)"
    )


def test_provider_message_raw_only_no_provider_name():
    """Upstream message but no provider name attribution."""
    body = {
        "error": {
            "message": "Provider returned error",
            "metadata": {
                "raw": '{"error":{"message":"Tokens limit exceeded"}}',
            },
        }
    }
    assert _composed_message(body) == (
        "Provider returned error (upstream: Tokens limit exceeded)"
    )


def test_provider_message_metadata_present_but_empty():
    """An empty metadata dict shouldn't add a malformed suffix."""
    body = {
        "error": {
            "message": "Provider returned error",
            "metadata": {},
        }
    }
    assert _composed_message(body) == "Provider returned error"


def test_provider_message_flat_top_level_message_in_raw():
    """Some upstream providers don't nest under `error`; they put `message`
    at the top level. Handle both shapes."""
    body = {
        "error": {
            "message": "Provider returned error",
            "metadata": {
                "raw": '{"message":"Service unavailable"}',
                "provider_name": "ProviderX",
            },
        }
    }
    assert _composed_message(body) == (
        "Provider returned error (ProviderX: Service unavailable)"
    )


def test_provider_message_none_body():
    from metis.adapters.openrouter import _provider_message

    assert _provider_message(None) == ""
    assert _provider_message({}) == ""
    assert _provider_message({"error": "not a dict"}) == ""


def test_provider_message_atlas_cloud_msg_field():
    """AtlasCloud (and similar Asian providers) use top-level `msg`, not
    `message` and not nested under `error`. The request_id is also surfaced
    so users can quote it in support tickets."""
    body = {
        "error": {
            "message": "Provider returned error",
            "metadata": {
                "raw": (
                    '{"code":400,"msg":"bad request",'
                    '"request_id":"5f083b10-a412-48b2-9ba1-171c6f13727d"}'
                ),
                "provider_name": "AtlasCloud",
            },
        }
    }
    assert _composed_message(body) == (
        "Provider returned error "
        "(AtlasCloud: bad request [req: 5f083b10-a412-48b2-9ba1-171c6f13727d])"
    )


def test_provider_message_nested_error_msg_field():
    """Same `msg` convention but nested under `error`."""
    body = {
        "error": {
            "message": "Provider returned error",
            "metadata": {
                "raw": '{"error":{"code":500,"msg":"internal failure"}}',
                "provider_name": "SomeProvider",
            },
        }
    }
    assert _composed_message(body) == (
        "Provider returned error (SomeProvider: internal failure)"
    )


def test_provider_message_fastapi_detail_field():
    """FastAPI / Starlette services often use `detail` instead of `message`."""
    body = {
        "error": {
            "message": "Provider returned error",
            "metadata": {
                "raw": '{"detail":"validation failed: field x missing"}',
                "provider_name": "CustomProvider",
            },
        }
    }
    assert _composed_message(body) == (
        "Provider returned error (CustomProvider: validation failed: field x missing)"
    )


def test_provider_message_priority_message_wins_over_msg():
    """When BOTH `message` and `msg` are present, `message` (the canonical
    field) wins — it's the OpenAI-shape convention most providers follow."""
    body = {
        "error": {
            "message": "Provider returned error",
            "metadata": {
                "raw": (
                    '{"error":{"message":"canonical message","msg":"asian-style msg"}}'
                ),
                "provider_name": "MixedConventions",
            },
        }
    }
    assert _composed_message(body) == (
        "Provider returned error (MixedConventions: canonical message)"
    )


def test_provider_message_no_recognized_field_falls_back_to_raw():
    """When none of the known fields exist, surface the raw body verbatim
    rather than dropping it — at least the user has SOMETHING to debug."""
    body = {
        "error": {
            "message": "Provider returned error",
            "metadata": {
                "raw": '{"weird_field":"some weird value","status":418}',
                "provider_name": "Weird",
            },
        }
    }
    msg = _composed_message(body)
    # Falls back to the full raw blurb since nothing was extractable.
    assert "Weird" in msg
    assert "weird_field" in msg


# ---- request_id surfacing --------------------------------------------


def test_provider_message_includes_request_id_when_present():
    """The AtlasCloud-style body carries a request_id we want surfaced for
    support tickets — appears in brackets after the message."""
    body = {
        "error": {
            "message": "Provider returned error",
            "metadata": {
                "raw": (
                    '{"code":400,"msg":"bad request",'
                    '"request_id":"5f083b10-a412-48b2-9ba1-171c6f13727d"}'
                ),
                "provider_name": "AtlasCloud",
            },
        }
    }
    msg = _composed_message(body)
    assert "AtlasCloud: bad request [req: 5f083b10-a412-48b2-9ba1-171c6f13727d]" in msg


def test_provider_message_request_id_no_message_still_surfaces():
    """If the upstream has no message but does have a request_id, surface
    the request_id alone — useful for opaque rejections."""
    body = {
        "error": {
            "message": "Provider returned error",
            "metadata": {
                "raw": '{"request_id":"abc-123"}',
                "provider_name": "Provider",
            },
        }
    }
    msg = _composed_message(body)
    assert "[req: abc-123]" in msg
    assert "Provider" in msg


def test_provider_message_camel_case_request_id():
    """Some providers use `requestId` instead of `request_id`."""
    body = {
        "error": {
            "message": "Provider returned error",
            "metadata": {
                "raw": '{"msg":"forbidden","requestId":"camel-case-id"}',
                "provider_name": "P",
            },
        }
    }
    msg = _composed_message(body)
    assert "[req: camel-case-id]" in msg


def test_provider_message_no_request_id_no_brackets():
    """No request_id in the body → no `[req: ...]` appended."""
    body = {
        "error": {
            "message": "Provider returned error",
            "metadata": {
                "raw": '{"error":{"message":"context too long"}}',
                "provider_name": "Together",
            },
        }
    }
    msg = _composed_message(body)
    assert "[req:" not in msg
    assert msg == "Provider returned error (Together: context too long)"


def test_provider_message_request_id_in_nested_error():
    """request_id nested under `error.request_id` rather than top-level."""
    body = {
        "error": {
            "message": "Provider returned error",
            "metadata": {
                "raw": (
                    '{"error":{"message":"validation failed","request_id":"nested-id"}}'
                ),
                "provider_name": "P",
            },
        }
    }
    msg = _composed_message(body)
    assert "[req: nested-id]" in msg
    assert "validation failed" in msg
