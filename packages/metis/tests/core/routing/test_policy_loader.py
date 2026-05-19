"""routing/policy_loader.py: yaml parsing + validation per routing-engine §5.7."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from metis.core.routing.policy import (
    AllOf,
    AnyOf,
    EstimatedInputTokensGt,
    HasImages,
    MessageContainsAny,
    MessageMatches,
    Not,
    TeamBudgetRemainingLt,
    TimeOfDayBetween,
    WorkspacePathMatches,
)
from metis.core.routing.policy_loader import (
    PolicyValidationError,
    load_policy_file,
    parse_policy_text,
)


def _fake_registry(models: list[str]) -> MagicMock:
    reg = MagicMock()
    reg.resolve_alias = lambda m: m if m in models else None
    return reg


@pytest.fixture
def registry() -> MagicMock:
    return _fake_registry(
        [
            "anthropic:claude-haiku-4-5",
            "anthropic:claude-sonnet-4-6",
            "anthropic:claude-opus-4-7",
            "openai:gpt-5",
            "openai:gpt-5-mini",
        ]
    )


# ---- Happy path ---------------------------------------------------------


def test_parses_full_example(registry):
    raw = """
schema_version: 1
global_default: anthropic:claude-sonnet-4-6

tiers:
  fast: anthropic:claude-haiku-4-5
  balanced: anthropic:claude-sonnet-4-6
  deep: anthropic:claude-opus-4-7

pattern:
  cost_weight: 0.3
  min_confidence: 0.3
  min_sample_size: 5

rules:
  - name: fast for commits
    when:
      message_matches: "^/commit"
    use: anthropic:claude-haiku-4-5
  - name: deep for architecture
    when:
      any_of:
        - message_matches: "architecture"
        - message_contains_any: ["design review", "security review"]
    use: anthropic:claude-opus-4-7

workspaces:
  /tmp/myproject:
    default: openai:gpt-5
    tiers:
      fast: openai:gpt-5-mini
      balanced: openai:gpt-5
      deep: openai:gpt-5
    rules:
      - name: sql to gpt
        when:
          file_extensions_in_context: [".sql"]
        use: openai:gpt-5
"""
    policy = parse_policy_text(raw, registry, source_path="/test/routing.yaml")
    assert policy.schema_version == 1
    assert policy.global_default == "anthropic:claude-sonnet-4-6"
    assert policy.tiers.fast == "anthropic:claude-haiku-4-5"
    assert policy.pattern.cost_weight == 0.3
    assert len(policy.rules) == 2
    assert policy.rules[0].name == "fast for commits"
    assert isinstance(policy.rules[0].when, MessageMatches)
    assert isinstance(policy.rules[1].when, AnyOf)
    assert len(policy.workspaces) == 1
    ws = policy.workspaces[0]
    assert ws.workspace_path == "/tmp/myproject"
    assert ws.default == "openai:gpt-5"
    assert ws.tiers.deep == "openai:gpt-5"
    assert len(ws.rules) == 1
    assert ws.rules[0].scope == "workspace"


def test_synthetic_rule_name_when_omitted(registry):
    raw = """
rules:
  - when: { message_matches: "x" }
    use: anthropic:claude-haiku-4-5
  - when: { message_matches: "y" }
    use: anthropic:claude-haiku-4-5
"""
    policy = parse_policy_text(raw, registry)
    assert [r.name for r in policy.rules] == ["rule_0", "rule_1"]


def test_multiple_top_level_keys_implicit_all_of(registry):
    raw = """
rules:
  - name: combo
    when:
      message_matches: "do"
      estimated_input_tokens_gt: 1000
    use: anthropic:claude-haiku-4-5
"""
    policy = parse_policy_text(raw, registry)
    assert isinstance(policy.rules[0].when, AllOf)
    assert len(policy.rules[0].when.predicates) == 2


def test_not_and_nested_compound(registry):
    raw = """
rules:
  - name: nested
    when:
      not:
        any_of:
          - has_images: true
          - message_matches: "skip"
    use: anthropic:claude-haiku-4-5
"""
    policy = parse_policy_text(raw, registry)
    rule = policy.rules[0]
    assert isinstance(rule.when, Not)
    assert isinstance(rule.when.predicate, AnyOf)


def test_time_of_day_window_parsed(registry):
    raw = """
rules:
  - when: { time_of_day_between: ["22:00", "06:00"] }
    use: anthropic:claude-haiku-4-5
"""
    policy = parse_policy_text(raw, registry)
    when = policy.rules[0].when
    assert isinstance(when, TimeOfDayBetween)
    assert when.start_minutes == 22 * 60
    assert when.end_minutes == 6 * 60


def test_workspace_path_matches_compiles_regex(registry):
    raw = """
rules:
  - when: { workspace_path_matches: "^/Users/" }
    use: anthropic:claude-haiku-4-5
"""
    policy = parse_policy_text(raw, registry)
    when = policy.rules[0].when
    assert isinstance(when, WorkspacePathMatches)
    assert when.pattern.search("/Users/me/code")


def test_message_contains_any_list(registry):
    raw = """
rules:
  - when: { message_contains_any: ["foo", "bar"] }
    use: anthropic:claude-haiku-4-5
"""
    policy = parse_policy_text(raw, registry)
    when = policy.rules[0].when
    assert isinstance(when, MessageContainsAny)
    assert when.substrings == ("foo", "bar")


def test_has_images_bool(registry):
    raw = """
rules:
  - when: { has_images: true }
    use: anthropic:claude-haiku-4-5
"""
    policy = parse_policy_text(raw, registry)
    when = policy.rules[0].when
    assert isinstance(when, HasImages)
    assert when.expected is True


def test_estimated_input_tokens_gt(registry):
    raw = """
rules:
  - when: { estimated_input_tokens_gt: 80000 }
    use: anthropic:claude-haiku-4-5
"""
    policy = parse_policy_text(raw, registry)
    when = policy.rules[0].when
    assert isinstance(when, EstimatedInputTokensGt)
    assert when.threshold == 80000


def test_team_budget_remaining_lt_parses(registry):
    """multi-user.md §6.1 — yaml parser accepts the new predicate key."""
    raw = """
rules:
  - name: "team-eng-headroom-soft-cap"
    when: { team_budget_remaining_lt: 10.0 }
    use: anthropic:claude-haiku-4-5
"""
    policy = parse_policy_text(raw, registry)
    when = policy.rules[0].when
    assert isinstance(when, TeamBudgetRemainingLt)
    assert when.threshold_usd == 10.0


# ---- Validation errors --------------------------------------------------


def _expect_error(raw: str, registry, *, match: str):
    with pytest.raises(PolicyValidationError) as exc:
        parse_policy_text(raw, registry)
    joined = "\n".join(exc.value.errors)
    assert match in joined, f"expected {match!r} in errors:\n{joined}"


def test_unsupported_schema_version(registry):
    _expect_error("schema_version: 99\n", registry, match="unsupported schema_version")


def test_unknown_global_default_model(registry):
    _expect_error(
        "global_default: bogus:model\n",
        registry,
        match="model 'bogus:model' is not registered",
    )


def test_partial_tier_map_rejected(registry):
    raw = """
tiers:
  fast: anthropic:claude-haiku-4-5
  balanced: anthropic:claude-sonnet-4-6
"""
    _expect_error(raw, registry, match="must define all three slots")


def test_unknown_tier_model(registry):
    raw = """
tiers:
  fast: anthropic:claude-haiku-4-5
  balanced: anthropic:claude-sonnet-4-6
  deep: bogus:model
"""
    _expect_error(raw, registry, match="not registered")


def test_invalid_regex(registry):
    raw = """
rules:
  - when: { message_matches: "[unterminated" }
    use: anthropic:claude-haiku-4-5
"""
    _expect_error(raw, registry, match="invalid regex")


def test_unknown_predicate_key(registry):
    raw = """
rules:
  - when: { quantum_entanglement: true }
    use: anthropic:claude-haiku-4-5
"""
    _expect_error(raw, registry, match="unknown predicate key")


def test_rule_missing_use(registry):
    raw = """
rules:
  - when: { message_matches: "x" }
"""
    _expect_error(raw, registry, match=".use: required")


def test_rule_missing_when(registry):
    raw = """
rules:
  - use: anthropic:claude-haiku-4-5
"""
    _expect_error(raw, registry, match=".when: required")


def test_unknown_use_model(registry):
    raw = """
rules:
  - when: { message_matches: "x" }
    use: bogus:model
"""
    _expect_error(raw, registry, match="not registered")


def test_duplicate_rule_name(registry):
    raw = """
rules:
  - name: dup
    when: { message_matches: "x" }
    use: anthropic:claude-haiku-4-5
  - name: dup
    when: { message_matches: "y" }
    use: anthropic:claude-haiku-4-5
"""
    _expect_error(raw, registry, match="duplicate name 'dup'")


def test_pattern_cost_weight_out_of_range(registry):
    _expect_error("pattern: { cost_weight: 1.5 }\n", registry, match="cost_weight must be in")


def test_pattern_cost_weight_default_is_zero_point_zero_five(registry):
    # Policy file with no `pattern` block at all → dataclass default applies.
    # The default was lowered from 0.3 → 0.1 on 2026-05-14 per the §A3-rev
    # benchmark finding, then from 0.1 → 0.05 on 2026-05-15 per the §A3-rev5
    # finding (routing-engine.md §5.5 "Default rationale").
    policy = parse_policy_text("schema_version: 1\n", registry)
    assert policy.pattern.cost_weight == 0.05

    # Same default when `pattern:` is present but `cost_weight` is omitted.
    policy_partial = parse_policy_text(
        "schema_version: 1\npattern: { min_confidence: 0.4 }\n", registry
    )
    assert policy_partial.pattern.cost_weight == 0.05
    assert policy_partial.pattern.min_confidence == 0.4


def test_pattern_cost_weight_explicit_override_preserves_old_defaults(registry):
    # The 0.3 → 0.1 → 0.05 default migration is opt-out: a workspace that
    # depended on either prior default restates `cost_weight: 0.3` (or 0.1)
    # in routing.yaml and gets that behavior back. This guarantees explicit
    # overrides keep working across both default migrations.
    policy_legacy = parse_policy_text(
        "schema_version: 1\npattern: { cost_weight: 0.3 }\n", registry
    )
    assert policy_legacy.pattern.cost_weight == 0.3
    policy_intermediate = parse_policy_text(
        "schema_version: 1\npattern: { cost_weight: 0.1 }\n", registry
    )
    assert policy_intermediate.pattern.cost_weight == 0.1


def test_pattern_min_confidence_default_is_zero_point_zero_five(registry):
    # Policy file with no `pattern` block at all → dataclass default applies.
    # The default was lowered from 0.3 → 0.05 on 2026-05-14 per the §A3-rev2
    # benchmark finding (routing-engine.md §5.5 "Default rationale" /
    # benchmarks/RESULTS.md §A3-rev2 finding). The gate scales down to match
    # the smaller score gaps the post-Wave-8 `cost_weight=0.1` produces.
    policy = parse_policy_text("schema_version: 1\n", registry)
    assert policy.pattern.min_confidence == 0.05

    # Same default when `pattern:` is present but `min_confidence` is omitted.
    policy_partial = parse_policy_text(
        "schema_version: 1\npattern: { cost_weight: 0.2 }\n", registry
    )
    assert policy_partial.pattern.min_confidence == 0.05
    assert policy_partial.pattern.cost_weight == 0.2


def test_pattern_min_confidence_explicit_override_preserves_old_default(registry):
    # The 0.3 → 0.05 default change is opt-out: a workspace that depended on
    # the tighter pre-2026-05-14 gate restates `min_confidence: 0.3` in
    # routing.yaml and gets the old behavior back. This guarantees explicit
    # overrides keep working after the default migration; the per-rule
    # override path is untouched.
    policy = parse_policy_text("schema_version: 1\npattern: { min_confidence: 0.3 }\n", registry)
    assert policy.pattern.min_confidence == 0.3


def test_pattern_min_sample_size_zero(registry):
    _expect_error(
        "pattern: { min_sample_size: 0 }\n", registry, match="min_sample_size must be int >= 1"
    )


def test_time_of_day_invalid_hhmm(registry):
    raw = """
rules:
  - when: { time_of_day_between: ["25:00", "06:00"] }
    use: anthropic:claude-haiku-4-5
"""
    _expect_error(raw, registry, match="HH:MM")


def test_message_contains_any_empty_list_rejected(registry):
    raw = """
rules:
  - when: { message_contains_any: [] }
    use: anthropic:claude-haiku-4-5
"""
    _expect_error(raw, registry, match="must contain at least one string")


def test_all_errors_accumulated(registry):
    """Multiple unrelated errors should all surface, not just the first."""
    raw = """
schema_version: 99
global_default: bogus:model
rules:
  - when: { message_matches: "[bad" }
    use: anthropic:claude-haiku-4-5
"""
    with pytest.raises(PolicyValidationError) as exc:
        parse_policy_text(raw, registry)
    assert len(exc.value.errors) >= 3


def test_yaml_syntax_error_surfaces(registry):
    _expect_error("{invalid: yaml: structure", registry, match="yaml parse error")


def test_empty_yaml_is_valid_empty_policy(registry):
    policy = parse_policy_text("", registry)
    assert policy.rules == ()
    assert policy.workspaces == ()
    assert policy.global_default is None


# ---- load_policy_file -----------------------------------------------------


def test_load_policy_file_reads_disk(tmp_path: Path, registry):
    path = tmp_path / "routing.yaml"
    path.write_text("global_default: anthropic:claude-sonnet-4-6\n")
    policy = load_policy_file(path, registry)
    assert policy.global_default == "anthropic:claude-sonnet-4-6"
    assert policy.source_path == str(path)


def test_load_policy_file_missing_file_raises(tmp_path: Path, registry):
    with pytest.raises(PolicyValidationError) as exc:
        load_policy_file(tmp_path / "missing.yaml", registry)
    assert "could not read" in exc.value.errors[0]
