"""Parse + validate a routing.yaml file per routing-engine.md §5.

Public entry points:

- `load_policy_file(path, registry)` — read yaml from disk, validate against
  the model registry, return `RoutingPolicy`. Raises `PolicyValidationError`
  with the full list of errors on failure.
- `parse_policy(raw_dict, registry, source_path=None)` — same but from an
  in-memory dict (used by tests + `POST /routing/check`).

Validation rules (§5.7):
1. yaml is well-formed.
2. `schema_version` is supported.
3. Every `use` and `default` references a registered model.
4. Every tier maps to a registered model.
5. Workspace `tiers` define all three slots or are absent (partial maps rejected).
6. Every predicate key + value type is valid.
7. Every regex compiles.
8. Rule names are unique within their scope.
9. `pattern.cost_weight` in [0,1]; `min_confidence` in [0,1]; `min_sample_size` >= 1.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

from metis.core.routing.policy import (
    AllOf,
    AnyOf,
    CostTodayExceedsUsd,
    EstimatedInputTokensGt,
    EstimatedInputTokensLt,
    FileExtensionsInContext,
    HasImages,
    HasToolCallsInHistory,
    MessageContainsAny,
    MessageMatches,
    Not,
    PatternConfig,
    Predicate,
    RoutingPolicy,
    Rule,
    SkillsMatchingMessageIncludes,
    TeamBudgetRemainingLt,
    TierMap,
    TimeOfDayBetween,
    WorkspacePathMatches,
    WorkspaceScope,
)
from metis.core.routing.registry import ModelRegistry

SUPPORTED_SCHEMA_VERSIONS = {1}


class PolicyValidationError(Exception):
    """Raised when routing.yaml fails to load or validate.

    Carries the full list of errors so `/rules check` can render them all.
    """

    def __init__(self, errors: list[str], source: str | None = None) -> None:
        joined = "; ".join(errors) or "policy validation failed"
        super().__init__(joined)
        self.errors = errors
        self.source = source


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def load_policy_file(
    path: str | Path,
    registry: ModelRegistry,
) -> RoutingPolicy:
    """Read a routing.yaml from disk, validate, and return the parsed policy."""
    src = str(Path(path).expanduser())
    try:
        raw = Path(src).read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyValidationError([f"could not read {src}: {exc}"], source=src) from exc
    return parse_policy_text(raw, registry, source_path=src)


def parse_policy_text(
    raw_yaml: str,
    registry: ModelRegistry,
    *,
    source_path: str | None = None,
) -> RoutingPolicy:
    try:
        data = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError as exc:
        raise PolicyValidationError([f"yaml parse error: {exc}"], source=source_path) from exc
    if not isinstance(data, dict):
        raise PolicyValidationError(["routing policy root must be a mapping"], source=source_path)
    # Content-derived version surfaces via GET /sessions/{id} so the SPA can
    # tell when the active routing.yaml has changed. Truncated sha256 of the
    # raw yaml — opaque, stable across reloads of identical content, and
    # changes whenever any rule, tier, or default is edited.
    version = hashlib.sha256(raw_yaml.encode("utf-8")).hexdigest()[:12]
    return parse_policy(data, registry, source_path=source_path, version=version)


def parse_policy(
    raw: dict[str, Any],
    registry: ModelRegistry,
    *,
    source_path: str | None = None,
    version: str | None = None,
) -> RoutingPolicy:
    errors: list[str] = []

    schema_version = raw.get("schema_version", 1)
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(
            f"unsupported schema_version {schema_version!r} (supported: "
            f"{sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )

    global_default = raw.get("global_default")
    if global_default is not None:
        _check_model_ref("global_default", global_default, registry, errors)

    tiers = _parse_tiers(raw.get("tiers"), "tiers", registry, errors, allow_partial=False)
    pattern = _parse_pattern(raw.get("pattern"), "pattern", errors)
    rules = _parse_rules(raw.get("rules") or [], scope="global", registry=registry, errors=errors)
    workspaces = _parse_workspaces(raw.get("workspaces") or {}, registry=registry, errors=errors)

    if errors:
        raise PolicyValidationError(errors, source=source_path)

    return RoutingPolicy(
        schema_version=int(schema_version),
        global_default=global_default,
        tiers=tiers,
        pattern=pattern,
        rules=tuple(rules),
        workspaces=tuple(workspaces),
        source_path=source_path,
        version=version,
    )


# ---------------------------------------------------------------------------
# Section parsers (collect errors; never raise)
# ---------------------------------------------------------------------------


def _parse_tiers(
    raw: Any,
    field: str,
    registry: ModelRegistry,
    errors: list[str],
    *,
    allow_partial: bool,
) -> TierMap | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append(f"{field}: must be a mapping")
        return None
    keys = set(raw.keys())
    required = {"fast", "balanced", "deep"}
    extra = keys - required
    if extra:
        errors.append(f"{field}: unknown keys {sorted(extra)} (allowed: fast, balanced, deep)")
    missing = required - keys
    if missing and not allow_partial:
        errors.append(
            f"{field}: must define all three slots (fast, balanced, deep); missing {sorted(missing)}"
        )
        return None
    if missing and allow_partial:
        # Reserved for future per-workspace partial overrides; currently rejected.
        errors.append(f"{field}: partial tier maps are not allowed (must define all three slots)")
        return None
    for slot, model in raw.items():
        _check_model_ref(f"{field}.{slot}", model, registry, errors)
    return TierMap(fast=raw["fast"], balanced=raw["balanced"], deep=raw["deep"])


def _parse_pattern(raw: Any, field: str, errors: list[str]) -> PatternConfig:
    if raw is None:
        return PatternConfig()
    if not isinstance(raw, dict):
        errors.append(f"{field}: must be a mapping")
        return PatternConfig()
    # Fall through to the PatternConfig dataclass defaults so the single
    # source of truth for `cost_weight` / `min_confidence` / `min_sample_size`
    # defaults is `policy.PatternConfig` (see its docstring for the rationale
    # behind the 2026-05-14 cost_weight 0.3 → 0.1 and min_confidence 0.3 →
    # 0.05 migrations).
    defaults = PatternConfig()
    cost_weight = raw.get("cost_weight", defaults.cost_weight)
    min_conf = raw.get("min_confidence", defaults.min_confidence)
    min_samples = raw.get("min_sample_size", defaults.min_sample_size)
    fingerprint_version = raw.get("fingerprint_version", defaults.fingerprint_version)
    embedding_provider = raw.get("embedding_provider", defaults.embedding_provider)
    embedding_alpha = raw.get("embedding_alpha", defaults.embedding_alpha)
    if not isinstance(cost_weight, int | float) or not (0.0 <= cost_weight <= 1.0):
        errors.append(f"{field}.cost_weight must be in [0.0, 1.0] (got {cost_weight!r})")
    if not isinstance(min_conf, int | float) or not (0.0 <= min_conf <= 1.0):
        errors.append(f"{field}.min_confidence must be in [0.0, 1.0] (got {min_conf!r})")
    if not isinstance(min_samples, int) or min_samples < 1:
        errors.append(f"{field}.min_sample_size must be int >= 1 (got {min_samples!r})")
    if fingerprint_version not in ("v1", "v2"):
        errors.append(
            f"{field}.fingerprint_version must be 'v1' or 'v2' (got {fingerprint_version!r})"
        )
    if embedding_provider is not None and not isinstance(embedding_provider, str):
        errors.append(f"{field}.embedding_provider must be a string (got {embedding_provider!r})")
    if not isinstance(embedding_alpha, int | float) or not (0.0 <= embedding_alpha <= 1.0):
        errors.append(f"{field}.embedding_alpha must be in [0.0, 1.0] (got {embedding_alpha!r})")
    if fingerprint_version == "v2" and not embedding_provider:
        errors.append(f"{field}.fingerprint_version='v2' requires {field}.embedding_provider")
    # If we detected a v2-without-provider error above, fall back to v1 in
    # the constructed config so PatternConfig.__post_init__ doesn't raise
    # before the caller sees the aggregated errors via PolicyValidationError.
    safe_fingerprint_version = fingerprint_version if fingerprint_version in ("v1", "v2") else "v1"
    safe_embedding_provider = (
        embedding_provider if isinstance(embedding_provider, str) and embedding_provider else None
    )
    if safe_fingerprint_version == "v2" and safe_embedding_provider is None:
        safe_fingerprint_version = "v1"
    return PatternConfig(
        cost_weight=float(cost_weight),
        min_confidence=float(min_conf),
        min_sample_size=int(min_samples) if isinstance(min_samples, int) else 5,
        fingerprint_version=safe_fingerprint_version,
        embedding_provider=safe_embedding_provider,
        embedding_alpha=float(embedding_alpha) if isinstance(embedding_alpha, int | float) else 0.6,
    )


def _parse_rules(
    raw: Any,
    *,
    scope: str,
    registry: ModelRegistry,
    errors: list[str],
) -> list[Rule]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append(f"{scope}.rules: must be a list")
        return []
    rules: list[Rule] = []
    seen_names: set[str] = set()
    for idx, item in enumerate(raw):
        prefix = f"{scope}.rules[{idx}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}: must be a mapping")
            continue
        name = item.get("name") or f"rule_{idx}"
        if not isinstance(name, str):
            errors.append(f"{prefix}.name: must be a string")
            continue
        if name in seen_names:
            errors.append(f"{prefix}.name: duplicate name {name!r}")
        seen_names.add(name)

        use = item.get("use")
        if use is None:
            errors.append(f"{prefix}.use: required")
        elif not isinstance(use, str):
            errors.append(f"{prefix}.use: must be a string")
        else:
            _check_model_ref(f"{prefix}.use", use, registry, errors)

        when_raw = item.get("when")
        if when_raw is None:
            errors.append(f"{prefix}.when: required")
            continue
        when = _parse_predicate(when_raw, f"{prefix}.when", errors)
        if when is None or use is None or not isinstance(use, str):
            continue
        rules.append(Rule(name=name, when=when, use=use, scope=scope))  # type: ignore[arg-type]
    return rules


def _parse_workspaces(
    raw: Any,
    *,
    registry: ModelRegistry,
    errors: list[str],
) -> list[WorkspaceScope]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        errors.append("workspaces: must be a mapping of path → config")
        return []
    workspaces: list[WorkspaceScope] = []
    for path, cfg in raw.items():
        if not isinstance(path, str) or not path:
            errors.append(f"workspaces: key must be a non-empty string, got {path!r}")
            continue
        expanded = str(Path(path).expanduser())
        if not isinstance(cfg, dict):
            errors.append(f"workspaces.{path}: must be a mapping")
            continue
        default = cfg.get("default")
        if default is not None:
            _check_model_ref(f"workspaces.{path}.default", default, registry, errors)
        tiers = _parse_tiers(
            cfg.get("tiers"),
            f"workspaces.{path}.tiers",
            registry,
            errors,
            allow_partial=False,
        )
        pattern = cfg.get("pattern")
        if pattern is not None:
            parsed_pattern = _parse_pattern(pattern, f"workspaces.{path}.pattern", errors)
        else:
            parsed_pattern = None
        rules = _parse_rules(
            cfg.get("rules") or [],
            scope=f"workspaces.{path}",
            registry=registry,
            errors=errors,
        )
        # Re-tag scope as "workspace" on the parsed rules (the parser tagged
        # them with the long scope name for error reporting).
        rules = [Rule(name=r.name, when=r.when, use=r.use, scope="workspace") for r in rules]
        workspaces.append(
            WorkspaceScope(
                workspace_path=expanded,
                default=default,
                tiers=tiers,
                pattern=parsed_pattern,
                rules=tuple(rules),
            )
        )
    return workspaces


# ---------------------------------------------------------------------------
# Predicate parser
# ---------------------------------------------------------------------------


_LEAF_PREDICATES = {
    "message_matches",
    "message_contains_any",
    "estimated_input_tokens_gt",
    "estimated_input_tokens_lt",
    "has_images",
    "has_tool_calls_in_history",
    "skills_matching_message_includes",
    "file_extensions_in_context",
    "workspace_path_matches",
    "time_of_day_between",
    "cost_today_exceeds_usd",
    "team_budget_remaining_lt",
}
_COMPOUND = {"any_of", "all_of", "not"}
_ALL_KEYS = _LEAF_PREDICATES | _COMPOUND


def _parse_predicate(raw: Any, prefix: str, errors: list[str]) -> Predicate | None:
    if not isinstance(raw, dict):
        errors.append(f"{prefix}: must be a mapping of predicate keys")
        return None
    keys = list(raw.keys())
    if not keys:
        errors.append(f"{prefix}: must have at least one predicate key")
        return None
    unknown = [k for k in keys if k not in _ALL_KEYS]
    if unknown:
        errors.append(f"{prefix}: unknown predicate key(s) {unknown}")
        return None

    if len(keys) > 1:
        # Implicit all_of (routing-engine §5.3.2 "A `when` block with
        # multiple top-level keys is implicitly `all_of`").
        children: list[Predicate] = []
        for k in keys:
            child = _parse_predicate({k: raw[k]}, f"{prefix}.{k}", errors)
            if child is not None:
                children.append(child)
        return AllOf(predicates=tuple(children))

    key = keys[0]
    value = raw[key]
    if key == "message_matches":
        return _parse_regex(value, prefix, errors, factory=MessageMatches)
    if key == "message_contains_any":
        return _parse_string_list(value, prefix, errors, factory=MessageContainsAny)
    if key == "estimated_input_tokens_gt":
        return _parse_int(value, prefix, errors, factory=EstimatedInputTokensGt)
    if key == "estimated_input_tokens_lt":
        return _parse_int(value, prefix, errors, factory=EstimatedInputTokensLt)
    if key == "has_images":
        return _parse_bool(value, prefix, errors, factory=HasImages)
    if key == "has_tool_calls_in_history":
        return _parse_bool(value, prefix, errors, factory=HasToolCallsInHistory)
    if key == "skills_matching_message_includes":
        return _parse_string_list(value, prefix, errors, factory=SkillsMatchingMessageIncludes)
    if key == "file_extensions_in_context":
        return _parse_string_list(value, prefix, errors, factory=FileExtensionsInContext)
    if key == "workspace_path_matches":
        return _parse_regex(value, prefix, errors, factory=WorkspacePathMatches)
    if key == "time_of_day_between":
        return _parse_time_window(value, prefix, errors)
    if key == "cost_today_exceeds_usd":
        return _parse_float(value, prefix, errors, factory=CostTodayExceedsUsd)
    if key == "team_budget_remaining_lt":
        return _parse_float(value, prefix, errors, factory=TeamBudgetRemainingLt)
    if key == "any_of":
        return _parse_list_predicate(value, prefix, errors, factory=AnyOf)
    if key == "all_of":
        return _parse_list_predicate(value, prefix, errors, factory=AllOf)
    if key == "not":
        inner = _parse_predicate(value, f"{prefix}.not", errors)
        return Not(predicate=inner) if inner is not None else None
    errors.append(f"{prefix}: unexpected key {key!r}")
    return None


def _parse_regex(value: Any, prefix: str, errors: list[str], *, factory):
    if not isinstance(value, str):
        errors.append(f"{prefix}: must be a regex string")
        return None
    try:
        return factory(pattern=re.compile(value))
    except re.error as exc:
        errors.append(f"{prefix}: invalid regex: {exc}")
        return None


def _parse_string_list(value: Any, prefix: str, errors: list[str], *, factory):
    if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
        errors.append(f"{prefix}: must be a list of strings")
        return None
    if not value:
        errors.append(f"{prefix}: must contain at least one string")
        return None
    if factory is FileExtensionsInContext:
        return factory(extensions=tuple(value))
    if factory is SkillsMatchingMessageIncludes:
        return factory(skill_names=tuple(value))
    return factory(substrings=tuple(value))


def _parse_int(value: Any, prefix: str, errors: list[str], *, factory):
    if not isinstance(value, int) or isinstance(value, bool):
        errors.append(f"{prefix}: must be an integer")
        return None
    return factory(threshold=value)


def _parse_float(value: Any, prefix: str, errors: list[str], *, factory):
    if not isinstance(value, int | float) or isinstance(value, bool):
        errors.append(f"{prefix}: must be a number")
        return None
    return factory(threshold_usd=float(value))


def _parse_bool(value: Any, prefix: str, errors: list[str], *, factory):
    if not isinstance(value, bool):
        errors.append(f"{prefix}: must be a boolean")
        return None
    return factory(expected=value)


def _parse_time_window(value: Any, prefix: str, errors: list[str]) -> TimeOfDayBetween | None:
    if not isinstance(value, list) or len(value) != 2 or not all(isinstance(s, str) for s in value):
        errors.append(f"{prefix}: must be a 2-element list of HH:MM strings")
        return None
    try:
        start = _hhmm_to_minutes(value[0])
        end = _hhmm_to_minutes(value[1])
    except ValueError as exc:
        errors.append(f"{prefix}: {exc}")
        return None
    return TimeOfDayBetween(start_minutes=start, end_minutes=end)


def _parse_list_predicate(value: Any, prefix: str, errors: list[str], *, factory):
    if not isinstance(value, list) or not value:
        errors.append(f"{prefix}: must be a non-empty list of predicates")
        return None
    children: list[Predicate] = []
    for i, item in enumerate(value):
        child = _parse_predicate(item, f"{prefix}[{i}]", errors)
        if child is not None:
            children.append(child)
    if not children:
        return None
    return factory(predicates=tuple(children))


def _hhmm_to_minutes(s: str) -> int:
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid HH:MM string: {s!r}")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"invalid HH:MM string: {s!r}") from exc
    if not (0 <= h <= 24 and 0 <= m < 60):
        raise ValueError(f"HH:MM out of range: {s!r}")
    return h * 60 + m


def _check_model_ref(field: str, model: Any, registry: ModelRegistry, errors: list[str]) -> None:
    if not isinstance(model, str) or not model:
        errors.append(f"{field}: must be a non-empty string")
        return
    if registry.resolve_alias(model) is None:
        errors.append(f"{field}: model {model!r} is not registered")
