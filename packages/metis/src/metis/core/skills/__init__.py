"""Persisted skills.

Conforms to the agentskills.io frontmatter spec (six-field schema, name
must equal parent directory, SKILL.md body). Skills are loaded from two
locations and merged with workspace-overrides-global:

    ~/.metis/skills/                    (global user library)
    <workspace>/.metis/skills/          (workspace-pinned)

The runtime registers two tools the agent uses for progressive disclosure:

    skill_search(query)   -> top-N (name, description) pairs (~100 tokens/skill)
    skill_load(name)      -> SKILL.md body + emits `skill.loaded` event

See `docs/specs/event-bus-and-trace-catalog.md` §6.6 for the event payload.
"""

from metis.core.skills.activation import (
    HARD_CAP_CUMULATIVE_ACTIVATION_TOKENS,
    MAX_EXPLICIT_ACTIVATIONS_PER_SESSION,
    WARN_CUMULATIVE_ACTIVATION_TOKENS,
    SkillActivationRegistry,
    SkillBudgetExceededError,
)
from metis.core.skills.store import (
    BODY_TOKEN_WARN_THRESHOLD,
    Skill,
    SkillSource,
    SkillStore,
    SkillValidationError,
    load_skills,
)
from metis.core.skills.tools import (
    SkillLoadTool,
    SkillSearchTool,
    register_skill_tools,
)

__all__ = [
    "BODY_TOKEN_WARN_THRESHOLD",
    "HARD_CAP_CUMULATIVE_ACTIVATION_TOKENS",
    "MAX_EXPLICIT_ACTIVATIONS_PER_SESSION",
    "WARN_CUMULATIVE_ACTIVATION_TOKENS",
    "Skill",
    "SkillActivationRegistry",
    "SkillBudgetExceededError",
    "SkillLoadTool",
    "SkillSearchTool",
    "SkillSource",
    "SkillStore",
    "SkillValidationError",
    "load_skills",
    "register_skill_tools",
]
