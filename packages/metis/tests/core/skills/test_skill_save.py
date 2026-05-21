"""SkillSaveTool: agent-authored skills + the skill.created event.

Covers the Phase 2.5 skill-authoring path (skill-format.md §8.3) that
unblocks the skill curator (skill-curator.md §3): the tool composes a
SKILL.md, validates it against the agentskills.io frontmatter contract,
writes <workspace>/.metis/skills/<name>/SKILL.md, and emits
skill.created(source="auto_generated").
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from metis.core.canonical.content import ToolUseBlock
from metis.core.events.bus import EventBus, EventFilter, Subscription
from metis.core.events.envelope import Actor, Event, Sensitivity
from metis.core.events.payloads import PAYLOAD_REGISTRY, SkillCreated, make_event
from metis.core.skills.store import load_skills
from metis.core.tools.builtins import SkillSaveTool, register_builtins
from metis.core.tools.dispatcher import ToolDispatcher


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

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="log", fast_path=True))
    return events


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def dispatcher(bus: EventBus) -> ToolDispatcher:
    d = ToolDispatcher(bus)
    register_builtins(d)
    return d


def _tool_use(**input: object) -> ToolUseBlock:
    return ToolUseBlock(id="tu_skill_save_1", name="skill_save", input=input)


async def _save(
    dispatcher: ToolDispatcher,
    bus: EventBus,
    workspace: Path,
    *,
    is_worker: bool = False,
    **input: object,
):
    result = await dispatcher.dispatch(
        _tool_use(**input),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
        is_worker=is_worker,
    )
    await bus.drain()
    return result


# ---- Registration -------------------------------------------------------


async def test_skill_save_registered_by_register_builtins(dispatcher: ToolDispatcher):
    names = {d.name for d in dispatcher.get_definitions_for_session()}
    assert "skill_save" in names


def test_skill_save_definition_is_write_and_workspace_scoped():
    definition = SkillSaveTool.definition
    assert definition.side_effects.value == "write"
    assert definition.requires_workspace is True


# ---- Happy path ---------------------------------------------------------


async def test_skill_save_writes_valid_skill_md(bus, dispatcher, workspace, event_log):
    result = await _save(
        dispatcher,
        bus,
        workspace,
        name="run-migrations",
        description="run database migrations safely",
        body="1. Back up the DB.\n2. Run `alembic upgrade head`.\n",
    )
    await bus.stop()
    assert result.is_error is False

    skill_md = workspace / ".metis" / "skills" / "run-migrations" / "SKILL.md"
    assert skill_md.is_file()

    # The composed file round-trips through the on-disk loader.
    store = load_skills(global_dir=None, workspace_dir=workspace / ".metis" / "skills")
    skill = store.get("run-migrations")
    assert skill is not None
    assert skill.description == "run database migrations safely"
    assert "alembic upgrade head" in skill.body
    assert skill.source == "workspace"


async def test_skill_save_emits_skill_created(bus, dispatcher, workspace, event_log):
    await _save(
        dispatcher,
        bus,
        workspace,
        name="lint-fix",
        description="run the linter and apply autofixes",
        body="Run `ruff check --fix`.\n",
    )
    await bus.stop()

    created = [e for e in event_log if e.type == "skill.created"]
    assert len(created) == 1
    payload = created[0].payload
    assert payload["skill_id"] == "lint-fix"
    assert payload["source"] == "auto_generated"
    assert payload["size_tokens"] > 0
    assert len(payload["skill_version"]) == 16  # SHA-256(body)[:16]
    assert created[0].actor == Actor.SYSTEM


async def test_skill_save_success_text_names_the_skill(bus, dispatcher, workspace, event_log):
    result = await _save(
        dispatcher,
        bus,
        workspace,
        name="deploy",
        description="deploy the service to staging",
        body="Push the image, then `helm upgrade`.\n",
    )
    await bus.stop()
    assert result.is_error is False
    assert "deploy" in result.content[0].text


async def test_skill_save_with_metadata_roundtrips(bus, dispatcher, workspace, event_log):
    await _save(
        dispatcher,
        bus,
        workspace,
        name="release-notes",
        description="draft release notes from the changelog",
        body="Summarise the changelog into user-facing notes.\n",
        metadata={"author": "metis-agent", "version": "1"},
    )
    await bus.stop()
    store = load_skills(global_dir=None, workspace_dir=workspace / ".metis" / "skills")
    skill = store.get("release-notes")
    assert skill is not None
    assert skill.metadata == {"author": "metis-agent", "version": "1"}


async def test_saved_skill_version_matches_event(bus, dispatcher, workspace, event_log):
    await _save(
        dispatcher,
        bus,
        workspace,
        name="bench",
        description="run the savings benchmark suite",
        body="Run `uv run python scripts/benchmark.py`.\n",
    )
    await bus.stop()
    store = load_skills(global_dir=None, workspace_dir=workspace / ".metis" / "skills")
    created = next(e for e in event_log if e.type == "skill.created")
    assert store.get("bench").version == created.payload["skill_version"]


# ---- Validation: invalid frontmatter rejected ---------------------------


async def test_skill_save_rejects_invalid_name(bus, dispatcher, workspace, event_log):
    # Uppercase passes the schema's length bounds but fails the
    # agentskills.io name regex — exercises the reused parse_skill validator.
    result = await _save(
        dispatcher,
        bus,
        workspace,
        name="BadName",
        description="a skill with an invalid name",
        body="body\n",
    )
    await bus.stop()
    assert result.is_error is True
    failed = next(e for e in event_log if e.type == "tool.failed")
    assert failed.payload["error_class"] == "validation_error"
    # validation_error is user-visible — the agent sees what to fix.
    assert "validation failed" in result.content[0].text
    assert not (workspace / ".metis" / "skills").exists()


async def test_skill_save_rejects_consecutive_hyphens(bus, dispatcher, workspace, event_log):
    result = await _save(
        dispatcher,
        bus,
        workspace,
        name="bad--name",
        description="consecutive hyphens are not allowed",
        body="body\n",
    )
    await bus.stop()
    assert result.is_error is True


async def test_skill_save_rejects_blank_description(bus, dispatcher, workspace, event_log):
    # Whitespace-only passes the schema's minLength but fails parse_skill's
    # non-empty-after-strip check.
    result = await _save(
        dispatcher,
        bus,
        workspace,
        name="blank-desc",
        description="   ",
        body="body\n",
    )
    await bus.stop()
    assert result.is_error is True
    assert not (workspace / ".metis" / "skills").exists()


async def test_skill_save_rejects_non_string_metadata_value(bus, dispatcher, workspace, event_log):
    result = await _save(
        dispatcher,
        bus,
        workspace,
        name="bad-meta",
        description="metadata values must be strings",
        body="body\n",
        metadata={"nested": {"not": "a string"}},
    )
    await bus.stop()
    assert result.is_error is True


# ---- Creation-only: no overwrite ----------------------------------------


async def test_skill_save_rejects_duplicate_name(bus, dispatcher, workspace, event_log):
    await _save(
        dispatcher,
        bus,
        workspace,
        name="dup",
        description="first definition",
        body="first body\n",
    )
    result = await _save(
        dispatcher,
        bus,
        workspace,
        name="dup",
        description="second definition",
        body="second body\n",
    )
    await bus.stop()
    assert result.is_error is True
    assert "already exists" in result.content[0].text
    failed = next(e for e in event_log if e.type == "tool.failed")
    assert failed.payload["error_class"] == "validation_error"
    # The original is untouched.
    store = load_skills(global_dir=None, workspace_dir=workspace / ".metis" / "skills")
    assert store.get("dup").description == "first definition"
    # Exactly one skill.created — the duplicate emitted none.
    assert len([e for e in event_log if e.type == "skill.created"]) == 1


# ---- Worker isolation ---------------------------------------------------


async def test_skill_save_refuses_worker_context(bus, dispatcher, workspace, event_log):
    result = await _save(
        dispatcher,
        bus,
        workspace,
        is_worker=True,
        name="worker-skill",
        description="workers must not author skills",
        body="body\n",
    )
    await bus.stop()
    assert result.is_error is True
    failed = next(e for e in event_log if e.type == "tool.failed")
    assert failed.payload["error_class"] == "execution_error"
    assert not (workspace / ".metis" / "skills").exists()
    assert not [e for e in event_log if e.type == "skill.created"]


def test_skill_save_in_worker_forbidden_set():
    # The session manager filters skill_save out of the worker tool list
    # (manager.py _WORKER_FORBIDDEN_TOOLS); end-to-end coverage of the
    # filter lives in tests/core/workers/test_delegation.py.
    from metis.core.sessions.manager import _WORKER_FORBIDDEN_TOOLS

    assert "skill_save" in _WORKER_FORBIDDEN_TOOLS


# ---- skill.created event ------------------------------------------------


def test_skill_created_registered_pseudonymous():
    cls, sensitivity = PAYLOAD_REGISTRY["skill.created"]
    assert cls is SkillCreated
    assert sensitivity is Sensitivity.PSEUDONYMOUS


def test_skill_created_event_roundtrips():
    payload = SkillCreated(
        skill_id="git-bisect",
        skill_version="0123456789abcdef",
        source="auto_generated",
        size_tokens=128,
    )
    event = make_event(
        type="skill.created",
        session_id="s1",
        actor=Actor.SYSTEM,
        payload=payload,
        timestamp=datetime.now(UTC),
        turn_id="t1",
    )
    assert event.type == "skill.created"
    assert event.sensitivity is Sensitivity.PSEUDONYMOUS
    assert event.payload == {
        "skill_id": "git-bisect",
        "skill_version": "0123456789abcdef",
        "source": "auto_generated",
        "size_tokens": 128,
    }


@pytest.mark.parametrize("source", ["manual", "auto_generated", "imported", "curator_generated"])
def test_skill_created_accepts_all_catalog_sources(source: str):
    # The source enum is the union of the catalog's values and the curator's
    # "curator_generated" (skill-curator.md §8.5) so the curator task does
    # not need a follow-up catalog change.
    payload = SkillCreated(skill_id="x", skill_version="abc", source=source, size_tokens=1)
    event = make_event(
        type="skill.created",
        session_id="s",
        actor=Actor.SYSTEM,
        payload=payload,
        timestamp=datetime.now(UTC),
    )
    assert event.payload["source"] == source
