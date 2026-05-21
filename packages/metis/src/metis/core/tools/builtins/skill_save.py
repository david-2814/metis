"""`skill_save` built-in tool — the agent authors a workspace skill.

See `docs/specs/skill-format.md` §8.3. The agent supplies a structured
(name, description, body, metadata) input; the tool composes a SKILL.md,
validates it against the agentskills.io frontmatter contract (reusing the
loader's validator), writes `<workspace>/.metis/skills/<name>/SKILL.md`, and
emits `skill.created` with `source="auto_generated"`.

This is the Phase 2.5 prerequisite that unblocks the skill curator
(`skill-curator.md` §3): the curator may only act on skills whose
`skill.created.source` is agent- or curator-generated, so an agent-authoring
path has to exist before the curator has anything to maintain.

`skill_save` is **planner-only**. The session manager filters it out of the
worker tool list (`manager.py` `_WORKER_FORBIDDEN_TOOLS`) alongside
`delegate` and the memory tools (delegation.md §5.6); `execute()` also
refuses defensively when `context.is_worker` is set. Workers run focused
sub-tasks — they don't author durable skills.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from metis.core.canonical.content import TextBlock
from metis.core.canonical.tools import SideEffects, ToolDefinition
from metis.core.events.envelope import Actor
from metis.core.events.payloads import SkillCreated, make_event
from metis.core.skills.store import SKILL_BODY_FILENAME, SkillValidationError, parse_skill
from metis.core.tools.errors import ToolExecutionError, ToolValidationError
from metis.core.tools.protocol import ToolContext, ToolOutput


class SkillSaveTool:
    """Author a new skill into the workspace skill library.

    Emits exactly one `skill.created(source="auto_generated")` per
    successful save. `SideEffects.WRITE` — a confirmation handler may gate
    the write. Creation only: `skill_save` refuses to overwrite an existing
    skill (editing is the curator's `edit` action, skill-curator.md §4.1).
    """

    definition = ToolDefinition(
        name="skill_save",
        description=(
            "Author a new skill into this workspace's skill library. Provide "
            "a short kebab-case `name`, a one-line `description` of what the "
            "skill does and when to use it, and the `body` — the Markdown "
            "operating instructions. The skill is written to "
            ".metis/skills/<name>/SKILL.md and becomes loadable via "
            "`skill_load` in future sessions. Use this for durable, reusable "
            "procedures worth keeping; not for one-off task notes (that is "
            "what memory is for). Fails if a skill with that name already "
            "exists."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": (
                        "Skill identifier: lowercase letters, digits, and "
                        "single hyphens (e.g. 'run-migrations'). Becomes the "
                        "directory name."
                    ),
                },
                "description": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1024,
                    "description": (
                        "One or two sentences: what the skill does and when "
                        "to apply it. This is what future sessions see in the "
                        "skill discovery index."
                    ),
                },
                "body": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The SKILL.md Markdown body — the operating "
                        "instructions. Keep it focused; ~5000 tokens is the "
                        "recommended ceiling."
                    ),
                },
                "metadata": {
                    "type": "object",
                    "description": (
                        "Optional string-valued metadata (e.g. author, "
                        "version). Conventional agentskills.io metadata keys; "
                        "no schema is imposed."
                    ),
                },
            },
            "required": ["name", "description", "body"],
            "additionalProperties": False,
        },
        side_effects=SideEffects.WRITE,
        requires_workspace=True,
    )

    async def cancel(self) -> bool:
        return True

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        # Defensive worker refusal. The session manager already filters
        # `skill_save` out of the worker tool list (manager.py
        # `_WORKER_FORBIDDEN_TOOLS`); this guards any path that reaches the
        # dispatcher directly. Workers don't author skills.
        if context.is_worker:
            raise ToolExecutionError(
                "workers cannot author skills (skill_save is planner-only)",
                tool_use_id=context.tool_use_id,
            )
        workspace = context.workspace_files
        if workspace is None:
            raise ToolExecutionError(
                "skill_save requires a workspace",
                tool_use_id=context.tool_use_id,
            )

        name = input["name"]
        metadata = input.get("metadata") or None
        raw = _compose_skill_md(name, input["description"], input["body"], metadata)

        # Validate the composed SKILL.md against the agentskills.io
        # frontmatter contract by reusing the loader's validator. The skill
        # directory is derived from `name`, so the name==directory invariant
        # (skill-format.md §4.1) holds by construction; the regex / length /
        # hyphen checks still run and reject a malformed name before
        # anything touches the filesystem.
        skill_dir = Path(workspace.workspace_root) / ".metis" / "skills" / name
        try:
            skill = parse_skill(raw, skill_dir=skill_dir, source="workspace")
        except SkillValidationError as exc:
            # validation_error is user-visible: surface the agentskills.io
            # failure so the agent can correct the frontmatter and retry.
            raise ToolValidationError(
                "skill validation failed: " + "; ".join(exc.errors),
                tool_use_id=context.tool_use_id,
                validation_errors=exc.errors,
            ) from exc

        rel_path = f".metis/skills/{name}/{SKILL_BODY_FILENAME}"
        if workspace.exists(rel_path):
            raise ToolValidationError(
                f"a skill named {name!r} already exists in this workspace; "
                f"pick a different name (skill_save only creates new skills — "
                f"editing an existing skill is not supported)",
                tool_use_id=context.tool_use_id,
            )

        workspace.write(rel_path, raw)

        # Emit skill.created per event-bus-and-trace-catalog §6.6. Structural
        # metadata only — the body never reaches the bus.
        bus = context.bus
        if bus is not None:
            bus.emit(
                make_event(
                    type="skill.created",
                    session_id=context.session_id,
                    turn_id=context.turn_id,
                    actor=Actor.SYSTEM,
                    payload=SkillCreated(
                        skill_id=skill.name,
                        skill_version=skill.version,
                        source="auto_generated",
                        size_tokens=skill.estimated_body_tokens,
                    ),
                    timestamp=datetime.now(UTC),
                )
            )

        return ToolOutput(
            content=[
                TextBlock(
                    text=(
                        f"Saved skill {skill.name!r} to {rel_path} "
                        f"(~{skill.estimated_body_tokens} body tokens, "
                        f"version {skill.version}). It will appear in the "
                        f"skill discovery index for new sessions in this "
                        f"workspace."
                    )
                )
            ],
            files_modified=[rel_path],
            metadata={
                "skill_id": skill.name,
                "skill_version": skill.version,
                "source": "auto_generated",
                "size_tokens": skill.estimated_body_tokens,
            },
        )


def _compose_skill_md(
    name: str,
    description: str,
    body: str,
    metadata: dict | None,
) -> str:
    """Assemble a SKILL.md document from the structured tool input.

    Frontmatter key order is fixed (name, description, then optional
    metadata) so the composed document — and therefore `Skill.version`,
    which is `SHA-256(body)` — is deterministic for identical inputs.
    """
    frontmatter: dict[str, object] = {"name": name, "description": description}
    if metadata:
        frontmatter["metadata"] = metadata
    yaml_block = yaml.safe_dump(
        frontmatter,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    return f"---\n{yaml_block}---\n\n{body.strip()}\n"


__all__ = ["SkillSaveTool"]
