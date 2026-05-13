"""SkillStore: directory enumeration, frontmatter validation, merge semantics."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from metis_core.skills.store import (
    BODY_TOKEN_WARN_THRESHOLD,
    SkillStore,
    SkillValidationError,
    _load_skill,
    load_skills,
)


def _write_skill(
    base: Path,
    name: str,
    *,
    description: str = "do the thing when you need to do the thing",
    body: str = "Body content here.\n",
    extra_frontmatter: str = "",
) -> Path:
    """Helper: create a `<base>/<name>/SKILL.md` with valid minimal frontmatter."""
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name}\ndescription: {description}\n"
    if extra_frontmatter:
        fm += extra_frontmatter
    fm += "---\n"
    (skill_dir / "SKILL.md").write_text(fm + body, encoding="utf-8")
    return skill_dir


# ---- Happy path -----------------------------------------------------------


def test_loads_single_valid_skill(tmp_path: Path):
    _write_skill(tmp_path, "code-review")
    store = load_skills(global_dir=tmp_path, workspace_dir=None)
    assert len(store) == 1
    skill = store.get("code-review")
    assert skill is not None
    assert skill.name == "code-review"
    assert "do the thing" in skill.description
    assert skill.source == "global"


def test_loads_multiple_skills_sorted(tmp_path: Path):
    _write_skill(tmp_path, "alpha")
    _write_skill(tmp_path, "beta")
    _write_skill(tmp_path, "charlie")
    store = load_skills(global_dir=tmp_path, workspace_dir=None)
    assert [s.name for s in store.list_skills()] == ["alpha", "beta", "charlie"]


def test_optional_fields_parsed(tmp_path: Path):
    extra = (
        "license: MIT\n"
        "compatibility: requires Python 3.13\n"
        "metadata:\n"
        '  version: "1.2"\n'
        "  author: David\n"
        "allowed-tools: bash python\n"
    )
    _write_skill(tmp_path, "rich-skill", extra_frontmatter=extra)
    store = load_skills(global_dir=tmp_path, workspace_dir=None)
    s = store.get("rich-skill")
    assert s.license == "MIT"
    assert s.compatibility == "requires Python 3.13"
    assert s.metadata == {"version": "1.2", "author": "David"}
    assert s.allowed_tools == ("bash", "python")


def test_metadata_scalar_coercion(tmp_path: Path):
    """metadata supports str->str; bare scalar values get string-coerced for
    ergonomics. Note: pyyaml uses YAML 1.1 boolean keywords (yes/no/on/off),
    so don't write those as bare keys without quoting."""
    _write_skill(
        tmp_path,
        "coerce-test",
        extra_frontmatter="metadata:\n  count: 5\n  enabled: true\n",
    )
    store = load_skills(global_dir=tmp_path, workspace_dir=None)
    s = store.get("coerce-test")
    assert s.metadata == {"count": "5", "enabled": "True"}


def test_skill_version_is_stable_hash(tmp_path: Path):
    _write_skill(tmp_path, "v-test", body="some body\n")
    store_a = load_skills(global_dir=tmp_path, workspace_dir=None)
    v_a = store_a.get("v-test").version
    store_b = load_skills(global_dir=tmp_path, workspace_dir=None)
    assert store_b.get("v-test").version == v_a
    # Change the body → version changes.
    _write_skill(tmp_path, "v-test", body="different body\n")
    store_c = load_skills(global_dir=tmp_path, workspace_dir=None)
    assert store_c.get("v-test").version != v_a


# ---- Validation -----------------------------------------------------------


def test_name_must_match_directory(tmp_path: Path):
    skill_dir = tmp_path / "actual-dir-name"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: different-name\ndescription: x\n---\nbody\n", encoding="utf-8"
    )
    with pytest.raises(SkillValidationError) as exc:
        _load_skill(skill_dir, source="global")
    assert any("equal parent directory name" in e for e in exc.value.errors)


def test_name_must_be_lowercase_hyphen_digits(tmp_path: Path):
    skill_dir = tmp_path / "BadName"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: BadName\ndescription: x\n---\n", encoding="utf-8"
    )
    with pytest.raises(SkillValidationError) as exc:
        _load_skill(skill_dir, source="global")
    assert any("lowercase letters/digits/hyphens" in e for e in exc.value.errors)


def test_name_no_consecutive_hyphens(tmp_path: Path):
    skill_dir = tmp_path / "double--hyphen"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: double--hyphen\ndescription: x\n---\n", encoding="utf-8"
    )
    with pytest.raises(SkillValidationError):
        _load_skill(skill_dir, source="global")


def test_name_no_leading_or_trailing_hyphen(tmp_path: Path):
    for bad in ("-leading", "trailing-"):
        skill_dir = tmp_path / bad
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {bad}\ndescription: x\n---\n", encoding="utf-8"
        )
        with pytest.raises(SkillValidationError):
            _load_skill(skill_dir, source="global")


def test_name_too_long_rejected(tmp_path: Path):
    long_name = "a" * 65  # 65 chars > 64 max
    skill_dir = tmp_path / long_name
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {long_name}\ndescription: x\n---\n", encoding="utf-8"
    )
    with pytest.raises(SkillValidationError):
        _load_skill(skill_dir, source="global")


def test_description_required(tmp_path: Path):
    skill_dir = tmp_path / "no-desc"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: no-desc\n---\nbody\n", encoding="utf-8")
    with pytest.raises(SkillValidationError) as exc:
        _load_skill(skill_dir, source="global")
    assert any("description" in e for e in exc.value.errors)


def test_description_too_long_rejected(tmp_path: Path):
    skill_dir = tmp_path / "long-desc"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: long-desc\ndescription: {'x' * 1025}\n---\nbody\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillValidationError) as exc:
        _load_skill(skill_dir, source="global")
    assert any("1-1024 chars" in e for e in exc.value.errors)


def test_compatibility_length_capped(tmp_path: Path):
    _write_skill(
        tmp_path,
        "compat-skill",
        extra_frontmatter=f"compatibility: {'x' * 501}\n",
    )
    # load_skills logs the error and skips the bad skill rather than raising.
    store = load_skills(global_dir=tmp_path, workspace_dir=None)
    assert store.get("compat-skill") is None


def test_missing_frontmatter_rejected(tmp_path: Path):
    skill_dir = tmp_path / "no-fm"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("just a body, no frontmatter\n", encoding="utf-8")
    with pytest.raises(SkillValidationError) as exc:
        _load_skill(skill_dir, source="global")
    assert any("frontmatter" in e for e in exc.value.errors)


def test_unclosed_frontmatter_rejected(tmp_path: Path):
    skill_dir = tmp_path / "unclosed"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: unclosed\ndescription: x\nbody continues without close\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillValidationError):
        _load_skill(skill_dir, source="global")


def test_malformed_yaml_rejected(tmp_path: Path):
    skill_dir = tmp_path / "bad-yaml"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: bad-yaml\ndescription: x\n[broken: yaml\n---\nbody\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillValidationError) as exc:
        _load_skill(skill_dir, source="global")
    assert any("yaml parse error" in e for e in exc.value.errors)


# ---- File / directory enumeration ----------------------------------------


def test_skipping_non_dir_entries(tmp_path: Path):
    """Plain files at the skill root must be ignored — only dirs with
    SKILL.md count."""
    _write_skill(tmp_path, "real-skill")
    (tmp_path / "loose-file.md").write_text("not a skill\n")
    store = load_skills(global_dir=tmp_path, workspace_dir=None)
    assert [s.name for s in store.list_skills()] == ["real-skill"]


def test_dir_without_skill_md_skipped(tmp_path: Path):
    """Empty directory or a directory missing SKILL.md is silently ignored."""
    _write_skill(tmp_path, "good-skill")
    (tmp_path / "no-skill-md").mkdir()
    store = load_skills(global_dir=tmp_path, workspace_dir=None)
    assert [s.name for s in store.list_skills()] == ["good-skill"]


def test_uppercase_skill_md_required(tmp_path: Path):
    """Loader looks for `SKILL.md` (uppercase). On macOS HFS+ the FS is
    case-insensitive so this test only meaningfully runs on Linux. We use
    `pathlib.Path.exists()` (case-sensitive on the underlying syscall on
    case-insensitive FS) — if both names resolve to the same file, the
    skill loads either way."""
    skill_dir = tmp_path / "case-test"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: case-test\ndescription: x\n---\nbody\n", encoding="utf-8"
    )
    store = load_skills(global_dir=tmp_path, workspace_dir=None)
    assert store.get("case-test") is not None


def test_invalid_skill_skipped_not_raised(tmp_path: Path, caplog):
    """One bad skill should not break the rest of the load."""
    _write_skill(tmp_path, "good-skill")
    bad_dir = tmp_path / "bad-skill"
    bad_dir.mkdir()
    (bad_dir / "SKILL.md").write_text(
        "---\nname: wrong-name\ndescription: x\n---\n", encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING):
        store = load_skills(global_dir=tmp_path, workspace_dir=None)
    assert [s.name for s in store.list_skills()] == ["good-skill"]
    assert "bad-skill" in caplog.text or "wrong-name" in caplog.text


def test_no_dirs_returns_empty(tmp_path: Path):
    store = load_skills(global_dir=tmp_path, workspace_dir=None)
    assert len(store) == 0


def test_nonexistent_root_returns_empty(tmp_path: Path):
    store = load_skills(global_dir=tmp_path / "does-not-exist", workspace_dir=None)
    assert len(store) == 0


# ---- Workspace overrides global ------------------------------------------


def test_workspace_overrides_global_on_name_collision(tmp_path: Path):
    global_dir = tmp_path / "global"
    workspace_dir = tmp_path / "workspace"
    _write_skill(global_dir, "shared", description="global version")
    _write_skill(workspace_dir, "shared", description="workspace version")
    store = load_skills(global_dir=global_dir, workspace_dir=workspace_dir)
    assert len(store) == 1
    skill = store.get("shared")
    assert skill.description == "workspace version"
    assert skill.source == "workspace"


def test_global_and_workspace_skills_both_present(tmp_path: Path):
    global_dir = tmp_path / "global"
    workspace_dir = tmp_path / "workspace"
    _write_skill(global_dir, "only-global")
    _write_skill(workspace_dir, "only-workspace")
    store = load_skills(global_dir=global_dir, workspace_dir=workspace_dir)
    assert {s.name for s in store.list_skills()} == {"only-global", "only-workspace"}
    assert store.get("only-global").source == "global"
    assert store.get("only-workspace").source == "workspace"


def test_workspace_override_logged(tmp_path: Path, caplog):
    global_dir = tmp_path / "global"
    workspace_dir = tmp_path / "workspace"
    _write_skill(global_dir, "shared")
    _write_skill(workspace_dir, "shared")
    with caplog.at_level(logging.INFO):
        load_skills(global_dir=global_dir, workspace_dir=workspace_dir)
    assert "shared" in caplog.text


# ---- Body size warning ---------------------------------------------------


def test_body_size_warning_emitted(tmp_path: Path, caplog):
    big_body = "x" * (BODY_TOKEN_WARN_THRESHOLD * 5)  # ~5x threshold tokens
    _write_skill(tmp_path, "huge", body=big_body)
    with caplog.at_level(logging.WARNING):
        store = load_skills(global_dir=tmp_path, workspace_dir=None)
    skill = store.get("huge")
    assert skill is not None
    assert skill.over_body_token_warn is True
    assert "huge" in caplog.text


def test_small_body_no_warning(tmp_path: Path):
    _write_skill(tmp_path, "tiny", body="hi\n")
    store = load_skills(global_dir=tmp_path, workspace_dir=None)
    skill = store.get("tiny")
    assert skill.over_body_token_warn is False


# ---- Search ---------------------------------------------------------------


def test_search_substring(tmp_path: Path):
    _write_skill(tmp_path, "git-commit", description="write git commit messages")
    _write_skill(tmp_path, "review", description="code review")
    _write_skill(tmp_path, "other", description="unrelated")
    store = load_skills(global_dir=tmp_path, workspace_dir=None)
    results = store.search("git")
    assert [s.name for s in results] == ["git-commit"]


def test_search_ranks_name_match_over_description(tmp_path: Path):
    _write_skill(tmp_path, "git-commit", description="write commit messages")
    _write_skill(tmp_path, "other", description="git stuff here")
    store = load_skills(global_dir=tmp_path, workspace_dir=None)
    results = store.search("git")
    # name match scores higher (2) than description match (1).
    assert results[0].name == "git-commit"


def test_search_empty_query_returns_all(tmp_path: Path):
    _write_skill(tmp_path, "a")
    _write_skill(tmp_path, "b")
    store = load_skills(global_dir=tmp_path, workspace_dir=None)
    assert len(store.search("")) == 2


def test_search_limit_respected(tmp_path: Path):
    for i in range(5):
        _write_skill(tmp_path, f"skill-{i}", description="x" * 20)
    store = load_skills(global_dir=tmp_path, workspace_dir=None)
    assert len(store.search("skill", limit=3)) == 3


def test_search_no_match_returns_empty(tmp_path: Path):
    _write_skill(tmp_path, "git-commit")
    store = load_skills(global_dir=tmp_path, workspace_dir=None)
    assert store.search("does-not-exist") == []


# ---- Discovery index -----------------------------------------------------


def test_discovery_index_pairs(tmp_path: Path):
    _write_skill(tmp_path, "alpha", description="first")
    _write_skill(tmp_path, "beta", description="second")
    store = load_skills(global_dir=tmp_path, workspace_dir=None)
    assert store.discovery_index() == [("alpha", "first"), ("beta", "second")]


def test_empty_store_via_constructor():
    store = SkillStore.empty()
    assert len(store) == 0
    assert store.list_skills() == []
    assert store.search("x") == []
    assert store.discovery_index() == []
