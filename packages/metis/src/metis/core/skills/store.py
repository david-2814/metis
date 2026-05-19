"""Persisted skills loader.

Conforms to the agentskills.io frontmatter spec. Each skill is a
**directory** containing `SKILL.md`:

    ~/.metis/skills/
    ├── pdf-processing/
    │   ├── SKILL.md
    │   ├── scripts/...
    │   └── references/...
    └── code-review/
        └── SKILL.md

Frontmatter (YAML) — six fields per spec:

    name           required  1-64 chars, ^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$,
                             no consecutive hyphens, MUST equal parent dir name
    description    required  1-1024 chars, non-empty
    license        optional  string
    compatibility  optional  string, <=500 chars
    metadata       optional  string -> string map (version/author live here)
    allowed-tools  optional  space-separated list of tools (experimental; parsed
                             but not enforced in v1)

Skills are loaded from two locations and merged with workspace-overrides-global:

    ~/.metis/skills/               (global, user library)
    <workspace>/.metis/skills/     (workspace-pinned)

Progressive disclosure (agentskills.io discovery → activation → execution):
v1 implements discovery (frontmatter only, ~100 tokens/skill) plus on-demand
activation via `skill_load` tool (loads SKILL.md body, warn if >5000 tokens).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml

logger = logging.getLogger(__name__)

# Body recommended cap per spec (used to emit a warning at load time).
BODY_TOKEN_WARN_THRESHOLD = 5000
# Rough char-to-token estimator used for budget warnings.
CHARS_PER_TOKEN_ESTIMATE = 4

_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
SKILL_BODY_FILENAME = "SKILL.md"

SkillSource = Literal["global", "workspace"]


class SkillSourceEnum(StrEnum):
    GLOBAL = "global"
    WORKSPACE = "workspace"


@dataclass(frozen=True)
class Skill:
    """One loaded skill. Discovery fields (name, description, etc.) plus the
    body, which is loaded into memory at startup but the spec's "activation"
    semantics happen only when the agent calls `skill_load`."""

    name: str
    description: str
    body: str
    source: SkillSource  # "global" or "workspace"
    skill_dir: Path
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] | None = None
    allowed_tools: tuple[str, ...] = ()

    @property
    def version(self) -> str:
        """SHA-256 of the body — stable across runs, changes when body changes.
        Matches the `skill_version` field in `skill.loaded` event payload."""
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()[:16]

    @property
    def estimated_body_tokens(self) -> int:
        return max(1, len(self.body) // CHARS_PER_TOKEN_ESTIMATE)

    @property
    def over_body_token_warn(self) -> bool:
        return self.estimated_body_tokens > BODY_TOKEN_WARN_THRESHOLD


class SkillValidationError(ValueError):
    """A SKILL.md failed to validate. Carries the path so callers can log
    which file was bad."""

    def __init__(self, path: Path, errors: list[str]) -> None:
        super().__init__(f"{path}: " + "; ".join(errors))
        self.path = path
        self.errors = errors


class SkillStore:
    """In-memory index of loaded skills with workspace-overrides-global merge.

    Built once at startup via `load_skills(global_dir, workspace_dir)`.
    Reload-on-change is deferred to a later phase; agents holding a store
    reference always see the snapshot from initialization time.
    """

    def __init__(self, skills: dict[str, Skill]) -> None:
        self._by_name: dict[str, Skill] = dict(skills)

    @classmethod
    def empty(cls) -> SkillStore:
        return cls({})

    def list_skills(self) -> list[Skill]:
        return sorted(self._by_name.values(), key=lambda s: s.name)

    def get(self, name: str) -> Skill | None:
        return self._by_name.get(name)

    def search(self, query: str, *, limit: int = 10) -> list[Skill]:
        """Cheap case-insensitive substring match against name + description.

        Phase 2 keeps this dumb. A real semantic index lands in 2.5 alongside
        the pattern store.
        """
        q = query.strip().lower()
        if not q:
            return self.list_skills()[:limit]
        out: list[tuple[int, Skill]] = []
        for skill in self._by_name.values():
            score = 0
            if q in skill.name.lower():
                score += 2
            if q in skill.description.lower():
                score += 1
            if score > 0:
                out.append((score, skill))
        out.sort(key=lambda t: (-t[0], t[1].name))
        return [s for _, s in out[:limit]]

    def discovery_index(self) -> list[tuple[str, str]]:
        """The (name, description) pairs for system-prompt injection."""
        return [(s.name, s.description) for s in self.list_skills()]

    def __len__(self) -> int:
        return len(self._by_name)

    def __contains__(self, name: object) -> bool:
        return name in self._by_name


def load_skills(
    *,
    global_dir: Path | str | None,
    workspace_dir: Path | str | None,
) -> SkillStore:
    """Enumerate skill directories under both roots, parse + validate each,
    apply workspace-overrides-global merge. Returns a populated SkillStore.

    Malformed skills are logged and skipped — one bad file should not break
    the runtime. The full list of error paths is logged at WARNING.
    """
    global_skills = _load_dir(global_dir, source="global") if global_dir else {}
    workspace_skills = _load_dir(workspace_dir, source="workspace") if workspace_dir else {}
    merged: dict[str, Skill] = dict(global_skills)
    # Workspace overrides global on name collision (spec).
    for name, skill in workspace_skills.items():
        if name in merged:
            logger.info("skill %r in workspace overrides global definition", name)
        merged[name] = skill
    return SkillStore(merged)


def _load_dir(root: Path | str, *, source: SkillSource) -> dict[str, Skill]:
    """Enumerate subdirectories of `root`, parse SKILL.md in each."""
    root_path = Path(root).expanduser()
    if not root_path.is_dir():
        return {}
    skills: dict[str, Skill] = {}
    for entry in sorted(root_path.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / SKILL_BODY_FILENAME
        if not skill_md.is_file():
            continue
        try:
            skill = _load_skill(entry, source=source)
        except SkillValidationError as exc:
            logger.warning("skipping invalid skill at %s: %s", exc.path, "; ".join(exc.errors))
            continue
        if skill.over_body_token_warn:
            logger.warning(
                "skill %r body is ~%d tokens (recommendation: <=%d); "
                "split into separate files under references/ if possible",
                skill.name,
                skill.estimated_body_tokens,
                BODY_TOKEN_WARN_THRESHOLD,
            )
        skills[skill.name] = skill
    return skills


def _load_skill(skill_dir: Path, *, source: SkillSource) -> Skill:
    """Parse + validate the SKILL.md in `skill_dir`."""
    body_path = skill_dir / SKILL_BODY_FILENAME
    raw = body_path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(raw, body_path)
    errors: list[str] = []

    name = frontmatter.get("name")
    description = frontmatter.get("description")
    license_value = frontmatter.get("license")
    compatibility = frontmatter.get("compatibility")
    metadata = frontmatter.get("metadata")
    allowed_tools_raw = frontmatter.get("allowed-tools")

    # name
    if not isinstance(name, str):
        errors.append("name: required string")
    elif not _NAME_RE.fullmatch(name):
        errors.append(
            "name: must match ^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$ (lowercase letters/digits/hyphens, "
            "no leading/trailing/consecutive hyphens)"
        )
    elif len(name) > 64:
        errors.append("name: must be 1-64 chars")
    elif "--" in name:
        errors.append("name: must not contain consecutive hyphens")
    elif name != skill_dir.name:
        errors.append(
            f"name: must equal parent directory name (frontmatter={name!r}, dir={skill_dir.name!r})"
        )

    # description
    if not isinstance(description, str):
        errors.append("description: required string")
    elif not description.strip():
        errors.append("description: must be non-empty")
    elif len(description) > 1024:
        errors.append(f"description: must be 1-1024 chars (got {len(description)})")

    # license (optional, string)
    if license_value is not None and not isinstance(license_value, str):
        errors.append("license: must be a string")

    # compatibility (optional, string, <=500 chars)
    if compatibility is not None:
        if not isinstance(compatibility, str):
            errors.append("compatibility: must be a string")
        elif len(compatibility) > 500:
            errors.append(f"compatibility: must be <=500 chars (got {len(compatibility)})")

    # metadata (optional, string -> string map)
    metadata_validated: dict[str, str] | None = None
    if metadata is not None:
        if not isinstance(metadata, dict):
            errors.append("metadata: must be a mapping")
        else:
            metadata_validated = {}
            for k, v in metadata.items():
                if not isinstance(k, str):
                    errors.append(f"metadata: keys must be strings, got {k!r}")
                    continue
                if not isinstance(v, str):
                    # Coerce simple scalars (int, float, bool) to string for ergonomics.
                    if isinstance(v, int | float | bool):
                        metadata_validated[k] = str(v)
                    else:
                        errors.append(
                            f"metadata[{k!r}]: values must be strings (got {type(v).__name__})"
                        )
                        continue
                else:
                    metadata_validated[k] = v

    # allowed-tools (optional, space-separated string per spec; experimental)
    allowed_tools_parsed: tuple[str, ...] = ()
    if allowed_tools_raw is not None:
        if not isinstance(allowed_tools_raw, str):
            errors.append("allowed-tools: must be a space-separated string")
        else:
            allowed_tools_parsed = tuple(t for t in allowed_tools_raw.split() if t)

    if errors:
        raise SkillValidationError(body_path, errors)

    return Skill(
        name=name,
        description=description.strip(),
        body=body,
        source=source,
        skill_dir=skill_dir,
        license=license_value,
        compatibility=compatibility,
        metadata=metadata_validated,
        allowed_tools=allowed_tools_parsed,
    )


def _split_frontmatter(raw: str, path: Path) -> tuple[dict[str, Any], str]:
    """Split `---\\n<yaml>\\n---\\n<body>` from a SKILL.md file.

    Frontmatter is required. A missing or malformed delimiter is a validation
    error caught here.
    """
    if not raw.startswith("---"):
        raise SkillValidationError(path, ["missing YAML frontmatter (file must start with `---`)"])
    # Strip the opening delimiter and split on the closing one.
    parts = raw.split("\n---", 1)
    if len(parts) != 2:
        raise SkillValidationError(path, ["missing closing `---` for YAML frontmatter"])
    yaml_block = parts[0][3:]  # drop leading "---"
    body = parts[1].lstrip("\n")
    try:
        data = yaml.safe_load(yaml_block) or {}
    except yaml.YAMLError as exc:
        raise SkillValidationError(path, [f"yaml parse error: {exc}"]) from exc
    if not isinstance(data, dict):
        raise SkillValidationError(path, ["frontmatter must be a YAML mapping"])
    return data, body
