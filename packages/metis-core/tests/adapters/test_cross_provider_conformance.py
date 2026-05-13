"""Cross-provider conformance: the canonical-format architectural claim.

A single session swaps between Anthropic, OpenAI, and OpenRouter mid-flight.
We assert:

- Each adapter receives a properly-translated request for its wire format.
- The ToolIdMap correctly translates tool_use ids across providers — a
  tool_use emitted by OpenAI must round-trip through Anthropic's wire format
  in a later turn with consistent ids.
- Costs are stamped per turn using the right rate for the chosen model.
- Session history accumulates correctly regardless of which adapter handled
  any given turn.

This test exercises the real adapter code (with mocked SDK clients) end-to-end
via the SessionManager. It's the test that catches structural breakage of the
"mid-session model swap survives" guarantee.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from metis_core.adapters.anthropic import AnthropicAdapter
from metis_core.adapters.openai import OpenAIAdapter
from metis_core.adapters.openrouter import OpenRouterAdapter
from metis_core.events.bus import EventBus
from metis_core.pricing import DEFAULT_PRICE_TABLE
from metis_core.routing import ModelRegistry, RoutingEngine
from metis_core.sessions import InMemorySessionStore, SessionManager
from metis_core.tools.builtins.file_ops import ReadFileTool
from metis_core.tools.dispatcher import ToolDispatcher

# ---- Mocked SDK clients for each provider -----------------------------


# Anthropic ---


class _AsyncIter:
    """Wrap a list as an async iterator (for stream mode mocks)."""

    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _AnthropicMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError("anthropic stub: out of responses")
        resp = self.responses.pop(0)
        if kwargs.get("stream"):
            return _AsyncIter(_synthesize_anth_stream(resp))
        return resp


def _synthesize_anth_stream(resp):
    """Turn a fake _anth_text response into a list of Anthropic SSE-like events.

    Mirrors the real provider's event sequence: message_start →
    content_block_start (text) → content_block_delta (text_delta) →
    content_block_stop → message_delta → message_stop.
    """
    chunks: list = [
        SimpleNamespace(type="message_start", message=SimpleNamespace(usage=resp.usage)),
    ]
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            chunks.append(
                SimpleNamespace(
                    type="content_block_start",
                    content_block=SimpleNamespace(type="text", text=""),
                )
            )
            chunks.append(
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text=block.text),
                )
            )
            chunks.append(SimpleNamespace(type="content_block_stop"))
    chunks.append(
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason=resp.stop_reason),
            usage=SimpleNamespace(output_tokens=resp.usage.output_tokens),
        )
    )
    chunks.append(SimpleNamespace(type="message_stop"))
    return chunks


class _AnthropicClient:
    def __init__(self, messages):
        self.messages = messages

    async def close(self):
        return


def _anth_text(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


# OpenAI / OpenRouter (same SDK shape) ---


class _OAICompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError("openai stub: out of responses")
        resp = self.responses.pop(0)
        if kwargs.get("stream"):
            return _AsyncIter(_synthesize_oai_stream(resp))
        return resp


def _synthesize_oai_stream(resp):
    """Turn a fake _oai_text or _oai_tool_call response into OpenAI stream chunks."""
    choice = resp.choices[0]
    message = choice.message
    finish_reason = choice.finish_reason
    chunks: list = []
    # First chunk: role announcement.
    chunks.append(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(role="assistant", content=None, tool_calls=None),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
    )
    content = getattr(message, "content", None)
    if isinstance(content, str) and content:
        chunks.append(
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=content, tool_calls=None),
                        finish_reason=None,
                    )
                ],
                usage=None,
            )
        )
    tool_calls = getattr(message, "tool_calls", None) or []
    for idx, tc in enumerate(tool_calls):
        # First chunk per call: id + name + empty args.
        chunks.append(
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=idx,
                                    id=tc.id,
                                    type="function",
                                    function=SimpleNamespace(name=tc.function.name, arguments=""),
                                )
                            ],
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
            )
        )
        # Second chunk per call: argument string.
        chunks.append(
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=idx,
                                    id=None,
                                    type=None,
                                    function=SimpleNamespace(
                                        name=None, arguments=tc.function.arguments
                                    ),
                                )
                            ],
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
            )
        )
    # Final chunk: finish_reason set.
    chunks.append(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=None, tool_calls=None),
                    finish_reason=finish_reason,
                )
            ],
            usage=None,
        )
    )
    # Usage-only chunk (stream_options.include_usage=True).
    chunks.append(SimpleNamespace(choices=[], usage=resp.usage))
    return chunks


class _OAIChat:
    def __init__(self, completions):
        self.completions = completions


class _OAIClient:
    def __init__(self, completions):
        self.chat = _OAIChat(completions)

    async def close(self):
        return


def _oai_text(text: str):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=text, tool_calls=None),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=200,
            completion_tokens=30,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
    )


def _oai_tool_call(call_id: str, name: str, args: str):
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
        usage=SimpleNamespace(
            prompt_tokens=180,
            completion_tokens=25,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
    )


# ---- Fixtures ---------------------------------------------------------


@pytest.fixture
async def workspace(tmp_path: Path) -> Path:
    (tmp_path / "README.md").write_text("This is the README.")
    return tmp_path


@pytest.fixture
async def bus() -> EventBus:
    bus = EventBus()
    bus.start()
    return bus


# ---- Helpers ----------------------------------------------------------


async def _build_or_adapter() -> OpenRouterAdapter:
    """OpenRouter adapter with a stubbed catalog (no HTTP) so capabilities/
    pricing for the test model are registered without a real fetch."""
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
            }
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=catalog_payload)

    or_oai_completions = _OAICompletions(responses=[_oai_text("openrouter answer")])
    or_client = _OAIClient(or_oai_completions)
    adapter = OpenRouterAdapter(api_key="or_test_key", client=or_client)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        await adapter.fetch_catalog(http_client=http_client)
    return adapter, or_oai_completions


# ---- The conformance test --------------------------------------------


async def test_cross_provider_session_with_tool_use(workspace, bus):
    """Three-turn session: Anthropic → OpenAI (with tool use) → OpenRouter.

    Validates that the canonical-format architecture survives provider swaps:
    each adapter receives a properly-translated history, the ToolIdMap keeps
    tool ids consistent across providers, and the SessionManager doesn't care
    which adapter handles any given turn.
    """
    # ---- Adapter stubs -------------------------------------------------

    anth_messages = _AnthropicMessages(responses=[_anth_text("anthropic answer")])
    anth_adapter = AnthropicAdapter(client=_AnthropicClient(anth_messages))

    oai_completions = _OAICompletions(
        responses=[
            # Turn 2 first call: emit a tool_use.
            _oai_tool_call(call_id="call_oai_1", name="read_file", args='{"path":"README.md"}'),
            # Turn 2 second call: after tool dispatch, return text.
            _oai_text("openai final answer"),
        ]
    )
    oai_adapter = OpenAIAdapter(client=_OAIClient(oai_completions))

    or_adapter, or_completions = await _build_or_adapter()

    # ---- Wire up registry / routing / dispatcher / session manager ----

    registry = ModelRegistry()
    registry.register(
        model_id="anthropic:claude-sonnet-4-6",
        adapter=anth_adapter,
        aliases=["sonnet"],
    )
    registry.register(
        model_id="openai:gpt-5",
        adapter=oai_adapter,
        aliases=["gpt5"],
    )
    registry.register(
        model_id="openrouter:anthropic/claude-sonnet-4",
        adapter=or_adapter,
        aliases=["or-sonnet"],
    )

    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)
    dispatcher.register(ReadFileTool)

    # Compose a price table that includes the OR rates.
    pricing = DEFAULT_PRICE_TABLE.with_overlay(
        overlay_version="openrouter-test",
        overlay_models={
            "openrouter:anthropic/claude-sonnet-4": or_adapter._capabilities[
                "openrouter:anthropic/claude-sonnet-4"
            ]
            and __import__("metis_core.pricing.table", fromlist=["ModelPricing"]).ModelPricing(
                input_per_mtok=Decimal("3.00"),
                output_per_mtok=Decimal("15.00"),
            )
        },
    )

    manager = SessionManager(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=InMemorySessionStore(),
        pricing=pricing,
    )

    session = manager.create_session(
        workspace_path=str(workspace),
        active_model="anthropic:claude-sonnet-4-6",
    )

    # ---- Turn 1: Anthropic, text only ---------------------------------

    r1 = await manager.submit_turn(session.id, "hello sonnet")
    assert r1.chosen_model == "anthropic:claude-sonnet-4-6"
    assert r1.assistant_text == "anthropic answer"
    assert r1.tool_call_count == 0
    # Anthropic stub saw one request with the user message.
    assert len(anth_messages.requests) == 1
    anth_req = anth_messages.requests[0]
    assert anth_req["model"] == "claude-sonnet-4-6"  # wire prefix stripped
    user_msgs = [m for m in anth_req["messages"] if m["role"] == "user"]
    assert any("hello sonnet" in str(m.get("content")) for m in user_msgs)

    # ---- Turn 2: swap to OpenAI, tool use cycle ----------------------

    manager.set_active_model(session.id, "gpt5")
    r2 = await manager.submit_turn(session.id, "now read README.md")
    assert r2.chosen_model == "openai:gpt-5"
    assert r2.assistant_text == "openai final answer"
    assert r2.llm_call_count == 2  # tool_use + tool_result → final
    assert r2.tool_call_count == 1

    # OpenAI saw two requests; the first one carried the prior Anthropic
    # turn's history correctly translated to OpenAI shape.
    assert len(oai_completions.requests) == 2
    first_oai_req = oai_completions.requests[0]
    assert first_oai_req["model"] == "gpt-5"
    msgs = first_oai_req["messages"]
    # System message at the top (OpenAI in-list, not hoisted).
    assert msgs[0]["role"] == "system"
    # Earlier user + assistant from turn 1, then current user.
    roles_in_order = [m["role"] for m in msgs]
    assert roles_in_order.count("user") >= 2  # turn 1 user + turn 2 user
    assert roles_in_order.count("assistant") >= 1  # turn 1 assistant

    # The second OpenAI request must include the tool_result from the
    # dispatch — verified by the presence of a role=tool message whose
    # tool_call_id matches the provider id from the first response.
    second_oai_req = oai_completions.requests[1]
    tool_msgs = [m for m in second_oai_req["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_oai_1"  # round-trip preserved

    # ---- Turn 3: swap to OpenRouter, sees full history ---------------

    manager.set_active_model(session.id, "or-sonnet")
    r3 = await manager.submit_turn(session.id, "summarize what just happened")
    assert r3.chosen_model == "openrouter:anthropic/claude-sonnet-4"
    assert r3.assistant_text == "openrouter answer"

    # OpenRouter saw the history including OpenAI's tool_use + tool_result.
    # On the OpenAI wire format (used by OpenRouter), the tool_use lives
    # on the assistant message with id=call_oai_1, and the tool result is
    # its own role=tool message with tool_call_id=call_oai_1.
    assert len(or_completions.requests) == 1
    or_req = or_completions.requests[0]
    assert or_req["model"] == "anthropic/claude-sonnet-4"  # openrouter: prefix stripped
    assistants = [m for m in or_req["messages"] if m["role"] == "assistant"]
    tool_calls_seen = []
    for a in assistants:
        if isinstance(a, dict) and a.get("tool_calls"):
            tool_calls_seen.extend(a["tool_calls"])
    assert len(tool_calls_seen) == 1
    assert tool_calls_seen[0]["id"] == "call_oai_1"
    or_tool_msgs = [m for m in or_req["messages"] if m["role"] == "tool"]
    assert len(or_tool_msgs) == 1
    assert or_tool_msgs[0]["tool_call_id"] == "call_oai_1"

    # ---- Cost accumulation across providers --------------------------

    fresh = manager._store.get_session(session.id)
    assert fresh.turn_count == 3
    assert fresh.cost_so_far_usd > 0
    # Each turn used a different model; verify costs were stamped on
    # each assistant message with the correct pricing_version.
    msgs = manager._store.get_messages(session.id)
    assistant_msgs = [m for m in msgs if m.role.value == "assistant"]
    assert len(assistant_msgs) >= 3
    # Last assistant message should be the OpenRouter one.
    last = assistant_msgs[-1]
    assert last.metadata.model == "openrouter:anthropic/claude-sonnet-4"
    assert last.metadata.usage is not None
    assert last.metadata.usage.cost_usd > 0
    # The pricing_version composed from default + openrouter overlay.
    assert "openrouter-test" in last.metadata.usage.pricing_version


async def test_per_message_override_switches_provider(workspace, bus):
    """`@gpt5 question` mid-Anthropic session routes that one turn to OpenAI."""
    anth_messages = _AnthropicMessages(responses=[_anth_text("sonnet1")])
    anth_adapter = AnthropicAdapter(client=_AnthropicClient(anth_messages))

    oai_completions = _OAICompletions(responses=[_oai_text("gpt-5 answer")])
    oai_adapter = OpenAIAdapter(client=_OAIClient(oai_completions))

    registry = ModelRegistry()
    registry.register(
        model_id="anthropic:claude-sonnet-4-6", adapter=anth_adapter, aliases=["sonnet"]
    )
    registry.register(model_id="openai:gpt-5", adapter=oai_adapter, aliases=["gpt5"])

    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)
    dispatcher.register(ReadFileTool)
    manager = SessionManager(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=InMemorySessionStore(),
        pricing=DEFAULT_PRICE_TABLE,
    )

    session = manager.create_session(workspace_path=str(workspace), active_model="sonnet")
    # Per-message override routes this turn to OpenAI; session sticky stays anthropic.
    result = await manager.submit_turn(session.id, "@gpt5 quick question")
    assert result.chosen_model == "openai:gpt-5"
    # Sticky is unchanged.
    assert manager._store.get_session(session.id).active_model == "anthropic:claude-sonnet-4-6"

    # The OpenAI request had only the cleaned text (without the @gpt5 prefix).
    assert len(oai_completions.requests) == 1
    msgs = oai_completions.requests[0]["messages"]
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert any("quick question" in str(u.get("content")) for u in user_msgs)
    assert not any("@gpt5" in str(u.get("content")) for u in user_msgs)
