"""SkillSearchTool + SkillLoadTool: dispatch flow + skill.loaded event."""

from __future__ import annotations

from pathlib import Path

import pytest

from metis.canonical.content import ToolUseBlock
from metis.events.bus import EventBus, EventFilter, Subscription
from metis.events.envelope import Event
from metis.skills.store import SkillStore, load_skills
from metis.skills.tools import register_skill_tools
from metis.tools.dispatcher import ToolDispatcher


@pytest.fixture
async def bus() -> EventBus:
    b = EventBus()
    b.start()
    return b


@pytest.fixture
async def event_log(bus: EventBus) -> list[Event]:
    events: list[Event] = []

    async def handler(e: Event) -> None:
        events.append(e)

    bus.subscribe(
        Subscription(filter=EventFilter(), handler=handler, name="log", fast_path=True)
    )
    return events


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def skill_dir(tmp_path: Path) -> Path:
    """Build a small skill library on disk + return the root."""
    root = tmp_path / "skills"
    for name, desc, body in [
        ("git-commit", "write semantic git commit messages", "Use conventional commits.\n"),
        ("code-review", "review code for bugs and clarity", "Look for clarity issues.\n"),
        ("pdf-extract", "extract text from PDFs", "Use pypdf.\n"),
    ]:
        d = root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\n{body}",
            encoding="utf-8",
        )
    return root


@pytest.fixture
def skills(skill_dir: Path) -> SkillStore:
    return load_skills(global_dir=skill_dir, workspace_dir=None)


@pytest.fixture
def dispatcher(bus: EventBus) -> ToolDispatcher:
    d = ToolDispatcher(bus)
    register_skill_tools(d)
    return d


def _tool_use(tool: str, **input: object) -> ToolUseBlock:
    return ToolUseBlock(id=f"tu_{tool}_1", name=tool, input=input)


# ---- Tool registration --------------------------------------------------


async def test_skill_tools_registered(dispatcher: ToolDispatcher):
    names = {d.name for d in dispatcher.get_definitions()}
    assert names == {"skill_search", "skill_load"}


# ---- skill_search -------------------------------------------------------


async def test_search_returns_matches(
    bus, workspace, skills, dispatcher, event_log
):
    result = await dispatcher.dispatch(
        _tool_use("skill_search", query="commit"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
        skills=skills,
    )
    await bus.drain()
    await bus.stop()
    assert result.is_error is False
    text = result.content[0].text
    assert "git-commit" in text


async def test_search_no_match(bus, workspace, skills, dispatcher):
    result = await dispatcher.dispatch(
        _tool_use("skill_search", query="does-not-exist"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
        skills=skills,
    )
    await bus.drain()
    await bus.stop()
    assert result.is_error is False
    assert "No skills matched" in result.content[0].text


async def test_search_without_skills_configured(bus, workspace, dispatcher, event_log):
    result = await dispatcher.dispatch(
        _tool_use("skill_search", query="anything"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
        skills=None,
    )
    await bus.drain()
    await bus.stop()
    assert result.is_error is True
    failed = next(e for e in event_log if e.type == "tool.failed")
    assert failed.payload["error_class"] == "execution_error"


async def test_search_respects_limit(bus, workspace, skills, dispatcher):
    result = await dispatcher.dispatch(
        _tool_use("skill_search", query="", limit=2),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
        skills=skills,
    )
    await bus.drain()
    await bus.stop()
    # Empty query returns all (sorted) — limit caps at 2.
    text = result.content[0].text
    assert text.count("\n-") == 2


# ---- skill_load ---------------------------------------------------------


async def test_load_returns_body(bus, workspace, skills, dispatcher, event_log):
    result = await dispatcher.dispatch(
        _tool_use("skill_load", name="git-commit"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
        skills=skills,
    )
    await bus.drain()
    await bus.stop()
    assert result.is_error is False
    text = result.content[0].text
    assert "Skill: git-commit" in text
    assert "Use conventional commits." in text


async def test_load_unknown_skill_returns_error(
    bus, workspace, skills, dispatcher, event_log
):
    result = await dispatcher.dispatch(
        _tool_use("skill_load", name="does-not-exist"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
        skills=skills,
    )
    await bus.drain()
    await bus.stop()
    assert result.is_error is True
    failed = next(e for e in event_log if e.type == "tool.failed")
    assert failed.payload["error_class"] == "execution_error"


async def test_load_emits_skill_loaded_event(
    bus, workspace, skills, dispatcher, event_log
):
    await dispatcher.dispatch(
        _tool_use("skill_load", name="pdf-extract"),
        session_id="sess_loaded",
        turn_id="turn_loaded",
        workspace_path=str(workspace),
        skills=skills,
    )
    await bus.drain()
    await bus.stop()
    loaded = [e for e in event_log if e.type == "skill.loaded"]
    assert len(loaded) == 1
    payload = loaded[0].payload
    assert payload["skill_id"] == "pdf-extract"
    assert payload["load_reason"] == "on_demand"
    assert payload["source"] == "global"
    assert payload["load_size_tokens"] >= 1
    assert payload["triggered_by_tool_use_id"] == "tu_skill_load_1"
    assert payload["skill_version"]  # non-empty hash


async def test_load_workspace_skill_source_recorded(
    bus, workspace, tmp_path, dispatcher, event_log
):
    """A workspace-sourced skill should report source='workspace' in the
    emitted event."""
    workspace_dir = tmp_path / "ws-skills"
    d = workspace_dir / "ws-only"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: ws-only\ndescription: workspace-pinned\n---\nbody\n",
        encoding="utf-8",
    )
    store = load_skills(global_dir=None, workspace_dir=workspace_dir)

    await dispatcher.dispatch(
        _tool_use("skill_load", name="ws-only"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
        skills=store,
    )
    await bus.drain()
    await bus.stop()
    loaded = next(e for e in event_log if e.type == "skill.loaded")
    assert loaded.payload["source"] == "workspace"


async def test_load_without_skills_configured(bus, workspace, dispatcher):
    result = await dispatcher.dispatch(
        _tool_use("skill_load", name="anything"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
        skills=None,
    )
    await bus.drain()
    await bus.stop()
    assert result.is_error is True
