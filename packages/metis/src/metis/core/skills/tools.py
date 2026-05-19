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

context-assembler.md v3 §5.2 adds the **activation registry**: a
per-session bookkeeping object that tracks both pre-activated skills
(bodies inlined into the stable prefix as v2 §5.1 padding,
`load_reason="always"`) and explicit activations (bodies returned by
`skill_load`, `load_reason="on_demand"`). When the registry is present
on `ToolContext.skill_activations`, `skill_load` consults it to:

- Return a short pointer (not the body) for pre-activated skills, with
  `metadata["already_preloaded"] = True`. No event fires; the
  pre-activation event already covered it.
- Return a pointer for already-explicitly-loaded skills (no body
  re-injection, no budget increment, no event).
- Enforce the §5.2.4 budget caps before returning the body. Exhaustion
  surfaces as `ToolExecutionError` → `tool.failed`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from metis.core.canonical.content import TextBlock
from metis.core.canonical.tools import SideEffects, ToolDefinition
from metis.core.events.envelope import Actor
from metis.core.events.payloads import SkillLoaded, make_event
from metis.core.skills.activation import (
    SkillActivationRegistry,
    SkillBudgetExceededError,
)
from metis.core.skills.store import Skill, SkillStore
from metis.core.tools.errors import ToolExecutionError
from metis.core.tools.protocol import ToolContext, ToolOutput


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
        registry: SkillActivationRegistry | None = getattr(context, "skill_activations", None)

        # Pre-activated path (§5.2.2): body is already in the stable
        # system prompt. Return a pointer instead of the body, with the
        # `already_preloaded` flag so the agent can disambiguate. No
        # `skill.loaded` event — pre-activation already emitted one at
        # session init; firing again would double-count.
        if registry is not None and registry.is_preloaded(skill.name):
            return ToolOutput(
                content=[TextBlock(text=_format_preloaded_pointer(skill))],
                metadata={
                    "skill_id": skill.name,
                    "skill_version": skill.version,
                    "source": skill.source,
                    "load_size_tokens": skill.estimated_body_tokens,
                    "already_preloaded": True,
                },
            )

        # No-op re-load path (§5.2.7 q4): a previously explicitly-loaded
        # skill is still in the message history, so re-injecting the
        # body would double-pay the tokens. Return a pointer; don't
        # increment the budget, don't emit a new event.
        if registry is not None and registry.is_activated(skill.name):
            return ToolOutput(
                content=[TextBlock(text=_format_already_loaded_pointer(skill))],
                metadata={
                    "skill_id": skill.name,
                    "skill_version": skill.version,
                    "source": skill.source,
                    "load_size_tokens": skill.estimated_body_tokens,
                    "already_loaded": True,
                },
            )

        # Budget check (§5.2.4). Exhaustion surfaces as
        # `ToolExecutionError` → `tool.failed` per §5.2.6 (no new event
        # type).
        if registry is not None:
            try:
                registry.check_can_activate(skill.name, skill.estimated_body_tokens)
            except SkillBudgetExceededError as exc:
                raise ToolExecutionError(
                    str(exc), tool_use_id=context.tool_use_id, underlying=exc
                ) from exc

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
        if registry is not None:
            registry.record_activation(skill.name, skill.estimated_body_tokens)
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


def _format_preloaded_pointer(skill: Skill) -> str:
    """Pointer returned when an agent calls `skill_load` for a skill whose
    body was already inlined into the stable system prompt as v2 §5.1
    padding (context-assembler.md v3 §5.2.2). The body is in the cached
    prefix, not in this tool_result — re-injecting it would double-pay
    the tokens."""
    return (
        f"# Skill: {skill.name} (source: {skill.source})\n\n"
        f"This skill's body is already loaded in the system prompt "
        f"(pre-activated at session start). Re-read the system prompt "
        f"section `### Skill: {skill.name}` for its operating instructions."
    )


def _format_already_loaded_pointer(skill: Skill) -> str:
    """Pointer returned when an agent re-calls `skill_load` for a skill
    that was already explicitly activated this session (context-assembler.md
    v3 §5.2.7 q4). The body is already in the message history; we don't
    re-inject it or increment the budget."""
    return (
        f"# Skill: {skill.name} (source: {skill.source})\n\n"
        f"This skill is already loaded in the conversation history "
        f"(activated earlier this session). Re-read the earlier "
        f'`tool_result` for `skill_load("{skill.name}")` for its '
        f"operating instructions."
    )


def register_skill_tools(dispatcher) -> None:
    """Register both skill tools on a ToolDispatcher."""
    dispatcher.register(SkillSearchTool)
    dispatcher.register(SkillLoadTool)
