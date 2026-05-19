"""SessionManager + SkillStore: discovery index injected into system prompt."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from metis.core.adapters.protocol import CanonicalRequest, StopReason, TokenUsage
from metis.core.adapters.streaming import MessageComplete, MessageStart, TextDelta
from metis.core.canonical.capabilities import AdapterCapabilities
from metis.core.canonical.content import TextBlock
from metis.core.canonical.ids import new_message_id
from metis.core.events.bus import EventBus
from metis.core.pricing import DEFAULT_PRICE_TABLE
from metis.core.routing import ModelRegistry, RoutingEngine
from metis.core.sessions import InMemorySessionStore, SessionManager
from metis.core.skills import register_skill_tools
from metis.core.skills.store import load_skills
from metis.core.tools.dispatcher import ToolDispatcher


@dataclass
class _Scripted:
    content: list
    stop_reason: StopReason


class _RecordingAdapter:
    name = "anthropic"

    def __init__(self, responses: list[_Scripted]) -> None:
        self._responses = list(responses)
        self.requests: list[CanonicalRequest] = []

    def capabilities_for(self, model: str) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_thinking=False,
            supports_images=True,
            supports_tools=True,
            supports_system_prompt=True,
            supports_structured_output=False,
            supports_streaming=True,
            supports_streaming_tool_calls=True,
            supports_parallel_tool_calls=True,
            supports_prompt_caching=True,
            supports_system_messages_in_list=False,
            max_context_tokens=200_000,
            max_output_tokens=8192,
        )

    async def stream(self, request: CanonicalRequest):
        self.requests.append(request)
        scripted = self._responses.pop(0)
        message_id = new_message_id()
        yield MessageStart(message_id=message_id, model=request.model)
        for idx, block in enumerate(scripted.content):
            if isinstance(block, TextBlock):
                yield TextDelta(message_id=message_id, content_block_index=idx, text=block.text)
        yield MessageComplete(
            message_id=message_id,
            stop_reason=scripted.stop_reason,
            final_content=scripted.content,
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            latency_ms=10,
        )

    async def cancel(self, request_id: str) -> bool:
        return False

    async def close(self) -> None:
        return

    def estimate_input_tokens(self, messages, tools, system_prompt) -> int:
        return 100


@pytest.fixture
async def bus() -> EventBus:
    b = EventBus()
    b.start()
    return b


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def skill_dir(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    for name, desc in [
        ("alpha", "first skill description"),
        ("beta", "second skill description"),
    ]:
        d = root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\nbody\n", encoding="utf-8"
        )
    return root


def _build_manager(
    bus: EventBus,
    adapter: _RecordingAdapter,
    *,
    skill_store_factory=None,
):
    registry = ModelRegistry()
    registry.register(
        model_id="anthropic:claude-sonnet-4-6",
        adapter=adapter,
        aliases=["sonnet"],
    )
    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)
    register_skill_tools(dispatcher)
    manager = SessionManager(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=InMemorySessionStore(),
        pricing=DEFAULT_PRICE_TABLE,
        skill_store_factory=skill_store_factory,
    )
    return manager


async def test_no_skills_factory_means_no_skills_section(bus, workspace):
    adapter = _RecordingAdapter(
        [_Scripted(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN)]
    )
    manager = _build_manager(bus, adapter, skill_store_factory=None)
    session = manager.create_session(workspace_path=str(workspace))
    assert manager.skills_for(session.id) is None
    await manager.submit_turn(session.id, "hi")
    await bus.drain()
    await bus.stop()
    assert "Available skills" not in adapter.requests[0].system_prompt


async def test_discovery_injected_into_system_prompt(bus, workspace, skill_dir):
    """When skills are configured, each turn's system prompt should list
    `- <name>: <description>` for every loaded skill."""
    adapter = _RecordingAdapter(
        [_Scripted(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN)]
    )
    manager = _build_manager(
        bus,
        adapter,
        skill_store_factory=lambda ws: load_skills(global_dir=skill_dir, workspace_dir=None),
    )
    session = manager.create_session(workspace_path=str(workspace))
    await manager.submit_turn(session.id, "hello")
    await bus.drain()
    await bus.stop()
    sp = adapter.requests[0].system_prompt
    assert "Available skills" in sp
    # v3 §5.2.2: short bodies are inlined as v2 §5.1 padding and the
    # discovery line gains a `[preloaded]` annotation. Allow either form
    # — we're testing that the description text reaches the prompt, not
    # the exact annotation state.
    assert ("alpha: first skill description" in sp) or (
        "alpha [preloaded]: first skill description" in sp
    )
    assert ("beta: second skill description" in sp) or (
        "beta [preloaded]: second skill description" in sp
    )


async def test_empty_skill_store_does_not_add_section(bus, workspace, tmp_path):
    """When skill_store_factory returns an empty store, no skills section
    should be injected."""
    adapter = _RecordingAdapter(
        [_Scripted(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN)]
    )
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    manager = _build_manager(
        bus,
        adapter,
        skill_store_factory=lambda ws: load_skills(global_dir=empty_dir, workspace_dir=None),
    )
    session = manager.create_session(workspace_path=str(workspace))
    await manager.submit_turn(session.id, "hello")
    await bus.drain()
    await bus.stop()
    assert "Available skills" not in adapter.requests[0].system_prompt


async def test_skills_factory_uses_workspace_path(bus, workspace, tmp_path):
    """The factory receives the workspace_path so per-session skills can
    pull from <workspace>/.metis/skills."""
    captured: list[str] = []

    def factory(ws):
        captured.append(ws)
        return load_skills(global_dir=None, workspace_dir=None)

    adapter = _RecordingAdapter(
        [_Scripted(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN)]
    )
    manager = _build_manager(bus, adapter, skill_store_factory=factory)
    manager.create_session(workspace_path=str(workspace))
    await bus.drain()
    await bus.stop()
    assert captured == [str(workspace)]
