"""Tests for the model-family / primary-version selection helpers."""

from __future__ import annotations

from metis_core.routing.families import (
    family_key,
    filter_by_pattern,
    select_primary,
    version_key,
)

# ---- family_key ---------------------------------------------------------


def test_native_ids_are_their_own_family():
    assert family_key("anthropic:claude-opus-4-7") == "anthropic:claude-opus-4-7"
    assert family_key("openai:gpt-5-mini") == "openai:gpt-5-mini"
    assert family_key("openai:gpt-5") == "openai:gpt-5"


def test_openrouter_strips_semver_version():
    assert family_key("openrouter:anthropic/claude-opus-4.7") == "openrouter:anthropic/claude-opus"
    assert family_key("openrouter:anthropic/claude-opus-4") == "openrouter:anthropic/claude-opus"
    assert family_key("openrouter:openai/gpt-5-1.2.3") == "openrouter:openai/gpt-5"


def test_openrouter_strips_iso_date():
    assert family_key("openrouter:openai/gpt-4-turbo-2024-04-09") == "openrouter:openai/gpt-4-turbo"


def test_openrouter_strips_compact_date():
    assert (
        family_key("openrouter:anthropic/claude-3-opus-20240229")
        == "openrouter:anthropic/claude-3-opus"
    )


def test_openrouter_without_version_suffix_unchanged():
    assert (
        family_key("openrouter:meta-llama/llama-3-70b-instruct")
        == "openrouter:meta-llama/llama-3-70b-instruct"
    )


# ---- version_key --------------------------------------------------------


def test_version_key_semver():
    assert version_key("foo-4") == (4,)
    assert version_key("foo-4.7") == (4, 7)
    assert version_key("foo-4.10") == (4, 10)


def test_version_key_orders_semver_naturally():
    assert version_key("foo-4.7") < version_key("foo-4.10")
    assert version_key("foo-4") < version_key("foo-4.0.1")


def test_version_key_compact_date():
    assert version_key("foo-20240229") == (20240229,)


def test_version_key_iso_date():
    assert version_key("foo-2024-04-09") == (20240409,)


def test_version_key_no_version_sorts_lowest():
    assert version_key("foo-bar") == (-1,)
    assert version_key("foo-bar") < version_key("foo-1")


# ---- select_primary -----------------------------------------------------


def test_native_curated_models_all_kept():
    ids = [
        "anthropic:claude-opus-4-7",
        "anthropic:claude-sonnet-4-6",
        "anthropic:claude-haiku-4-5",
        "openai:gpt-5",
        "openai:gpt-5-mini",
    ]
    assert sorted(select_primary(ids)) == sorted(ids)


def test_openrouter_versions_collapse_to_latest():
    ids = [
        "openrouter:anthropic/claude-opus-4",
        "openrouter:anthropic/claude-opus-4.7",
        "openrouter:anthropic/claude-opus-4.5",
    ]
    assert select_primary(ids) == ["openrouter:anthropic/claude-opus-4.7"]


def test_openrouter_dated_releases_keep_latest():
    ids = [
        "openrouter:openai/gpt-4-turbo-2024-04-09",
        "openrouter:openai/gpt-4-turbo-2024-11-20",
    ]
    assert select_primary(ids) == ["openrouter:openai/gpt-4-turbo-2024-11-20"]


def test_openrouter_unversioned_kept_as_sibling_when_other_versions_exist():
    """If the family has a versioned sibling, the unversioned one is older."""
    ids = [
        "openrouter:openai/gpt-4-turbo",
        "openrouter:openai/gpt-4-turbo-2024-04-09",
    ]
    # Both collapse to the same family. The dated one wins as "latest."
    assert select_primary(ids) == ["openrouter:openai/gpt-4-turbo-2024-04-09"]


def test_size_variants_stay_separate():
    """`-mini` / `-nano` aren't version patterns; they're tier variants."""
    ids = [
        "openrouter:openai/gpt-5",
        "openrouter:openai/gpt-5-mini",
        "openrouter:openai/gpt-5-nano",
    ]
    out = sorted(select_primary(ids))
    assert out == sorted(ids)


def test_mixed_providers():
    ids = [
        "anthropic:claude-opus-4-7",
        "openai:gpt-5",
        "openrouter:anthropic/claude-opus-4",
        "openrouter:anthropic/claude-opus-4.7",
        "openrouter:meta-llama/llama-3-70b-instruct",
    ]
    out = sorted(select_primary(ids))
    assert out == sorted(
        [
            "anthropic:claude-opus-4-7",
            "openai:gpt-5",
            "openrouter:anthropic/claude-opus-4.7",
            "openrouter:meta-llama/llama-3-70b-instruct",
        ]
    )


# ---- filter_by_pattern --------------------------------------------------


def test_filter_by_pattern_substring_case_insensitive():
    ids = [
        "anthropic:claude-opus-4-7",
        "openrouter:anthropic/claude-opus-4",
        "openrouter:anthropic/claude-opus-4.7",
        "anthropic:claude-sonnet-4-6",
        "openai:gpt-5",
    ]
    result = filter_by_pattern(ids, "OPUS")
    assert result == [
        "anthropic:claude-opus-4-7",
        "openrouter:anthropic/claude-opus-4",
        "openrouter:anthropic/claude-opus-4.7",
    ]


def test_filter_by_pattern_preserves_input_order():
    ids = ["a:b", "c:d", "a:e", "f:g"]
    assert filter_by_pattern(ids, "a:") == ["a:b", "a:e"]
