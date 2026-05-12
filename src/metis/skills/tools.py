"""Skill tools: skill_search + skill_load.

Two-tool flow per agentskills.io progressive disclosure:
1. `skill_search(query)` returns top-N (name, description) pairs (discovery
   stage, ~100 tokens/skill).
2. `skill_load(name)` returns the full SKILL.md body (activation stage,
   <=5000 tokens recommended) and emits `skill.loaded` with
   `load_reason="on_demand"`.

The store is passed in via `ToolContext` (we add a `skills` field there,
analogous to the `memory` field). The dispatcher's bridge in
`SessionManager` will populate it per-session.
"""

from __future__ import annotations

from datetime import UTC, datetime

from metis.canonical.content import TextBlock
from metis.canonical.tools import SideEffects, ToolDefinition
from metis.events.envelope import Actor
from metis.events.payloads import SkillLoaded, make_event
from metis.skills.store import Skill, SkillStore
from metis.tools.errors import ToolExecutionError
from metis.tools.protocol import ToolContext, ToolOutput


class _SkillToolBase:
    async def cancel(self) -> bool:
        return True

    @staticmethod
    def _require_skills(context: ToolContext) -> SkillStore:
        store = getattr(context, "skills", None)
        if store is None:
            raise ToolExecutionError(
                "skills are not configured for this session",
                tool_use_id=context.tool_use_id,
            )
        return store


class SkillSearchTool(_SkillToolBase):
    """Discovery: list skills whose name or description matches `query`.

    Returns a compact (name, description, source) list — never the body.
    Use `skill_load` to activate one once you've decided it's relevant.
    """

    definition = ToolDefinition(
        name="skill_search",
        description=(
            "Search the configured skills index by substring. Returns up to "
            "`limit` matches as compact name + description pairs. Use "
            "`skill_load(name)` to read a skill's body once you've decided "
            "it's relevant."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        side_effects=SideEffects.READ,
        requires_workspace=False,
    )

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        store = self._require_skills(context)
        results = store.search(input["query"], limit=int(input.get("limit", 10)))
        text = _format_search_results(input["query"], results)
        return ToolOutput(
            content=[TextBlock(text=text)],
            metadata={
                "query": input["query"],
                "result_count": len(results),
                "result_names": [s.name for s in results],
            },
        )


class SkillLoadTool(_SkillToolBase):
    """Activation: return the SKILL.md body for `name` and emit
    `skill.loaded` so traces show which skill was activated."""

    definition = ToolDefinition(
        name="skill_load",
        description=(
            "Load the full body of a skill by name. Use after `skill_search` "
            "has identified a relevant skill. The body is the operating "
            "instructions for that skill — read it before acting on it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 64},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        side_effects=SideEffects.READ,
        requires_workspace=False,
    )

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        store = self._require_skills(context)
        name = input["name"]
        skill = store.get(name)
        if skill is None:
            raise ToolExecutionError(
                f"no skill registered with name {name!r}",
                tool_use_id=context.tool_use_id,
            )
        # Emit skill.loaded per event-bus-and-trace-catalog §6.6.
        bus = getattr(context, "bus", None)
        if bus is not None:
            bus.emit(
                make_event(
                    type="skill.loaded",
                    session_id=context.session_id,
                    turn_id=context.turn_id,
                    actor=Actor.SYSTEM,
                    payload=SkillLoaded(
                        skill_id=skill.name,
                        skill_version=skill.version,
                        load_reason="on_demand",
                        load_size_tokens=skill.estimated_body_tokens,
                        source=skill.source,
                        triggered_by_tool_use_id=context.tool_use_id,
                    ),
                    timestamp=datetime.now(UTC),
                )
            )
        return ToolOutput(
            content=[TextBlock(text=_format_skill_body(skill))],
            metadata={
                "skill_id": skill.name,
                "skill_version": skill.version,
                "source": skill.source,
                "load_size_tokens": skill.estimated_body_tokens,
            },
        )


def _format_search_results(query: str, results: list[Skill]) -> str:
    if not results:
        return f"No skills matched {query!r}."
    lines = [f"Skills matching {query!r} ({len(results)}):"]
    for s in results:
        lines.append(f"- {s.name} [{s.source}] — {s.description}")
    return "\n".join(lines)


def _format_skill_body(skill: Skill) -> str:
    """Return the body wrapped with light header context the agent benefits
    from seeing (name + source + body)."""
    header = f"# Skill: {skill.name} (source: {skill.source})\n\n"
    return header + skill.body


def register_skill_tools(dispatcher) -> None:
    """Register both skill tools on a ToolDispatcher."""
    dispatcher.register(SkillSearchTool)
    dispatcher.register(SkillLoadTool)
