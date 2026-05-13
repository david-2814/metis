"""Tests for the curated standard task-profile recommendations."""

from __future__ import annotations

from metis.routing.profiles import STANDARD_TASK_PROFILES, standard_profile_for


def test_known_native_models_have_profiles():
    """Every model in the curated runtime registration set has a profile."""
    for model_id in [
        "anthropic:claude-opus-4-7",
        "anthropic:claude-sonnet-4-6",
        "anthropic:claude-haiku-4-5",
        "openai:gpt-5",
        "openai:gpt-5-mini",
    ]:
        assert model_id in STANDARD_TASK_PROFILES
        assert STANDARD_TASK_PROFILES[model_id], f"{model_id} has empty profile"


def test_standard_profile_for_known_model():
    """Returns a fresh list copy (callers can mutate without affecting the source)."""
    profile = standard_profile_for("anthropic:claude-opus-4-7")
    assert "deep-reasoning" in profile
    profile.append("mutated")
    assert "mutated" not in STANDARD_TASK_PROFILES["anthropic:claude-opus-4-7"]


def test_standard_profile_for_unknown_model_returns_empty_list():
    assert standard_profile_for("openrouter:obscure/unknown-model") == []
    assert standard_profile_for("nonexistent:model") == []


def test_profile_tags_are_lowercase_hyphenated():
    """Vocabulary convention: short, lowercase, hyphen-separated."""
    for model_id, tags in STANDARD_TASK_PROFILES.items():
        for tag in tags:
            assert tag == tag.lower(), f"{model_id} has non-lowercase tag: {tag!r}"
            assert " " not in tag, f"{model_id} has space in tag: {tag!r}"


def test_profile_tags_are_non_empty_strings():
    for model_id, tags in STANDARD_TASK_PROFILES.items():
        for tag in tags:
            assert isinstance(tag, str) and tag, f"{model_id} has bad tag: {tag!r}"


def test_no_tag_duplicates_within_a_model():
    for model_id, tags in STANDARD_TASK_PROFILES.items():
        assert len(tags) == len(set(tags)), f"{model_id} has duplicate tags: {tags!r}"


# ---- OpenRouter heuristic profiles --------------------------------------


def test_openrouter_anthropic_opus_gets_opus_tags():
    assert "deep-reasoning" in standard_profile_for(
        "openrouter:anthropic/claude-opus-4.7"
    )
    assert "deep-reasoning" in standard_profile_for(
        "openrouter:anthropic/claude-3-opus-20240229"
    )


def test_openrouter_anthropic_sonnet_gets_sonnet_tags():
    assert "coding" in standard_profile_for("openrouter:anthropic/claude-3.5-sonnet")
    assert "tool-use" in standard_profile_for("openrouter:anthropic/claude-sonnet-4.6")


def test_openrouter_anthropic_haiku_gets_haiku_tags():
    assert "cheap-bulk" in standard_profile_for("openrouter:anthropic/claude-3-haiku")
    assert "summarization" in standard_profile_for(
        "openrouter:anthropic/claude-haiku-4-5"
    )


def test_openrouter_openai_o_series_gets_reasoning_tags():
    """o1/o3 series are reasoning-tuned; full size goes to architecture."""
    full = standard_profile_for("openrouter:openai/o1")
    assert "deep-reasoning" in full
    assert "architecture" in full


def test_openrouter_openai_o_mini_separately_tagged():
    """o-mini reasoning models are cheap variants of o-series."""
    mini = standard_profile_for("openrouter:openai/o1-mini")
    assert "deep-reasoning" in mini
    assert "cheap-bulk" in mini


def test_openrouter_openai_gpt5_mini_more_specific_than_gpt5():
    """Specific patterns must match before broader ones."""
    base = standard_profile_for("openrouter:openai/gpt-5")
    mini = standard_profile_for("openrouter:openai/gpt-5-mini")
    assert base != mini  # specificity ordering correct
    assert "cheap-bulk" in mini
    assert "balanced" in base


def test_openrouter_openai_gpt4o_variants():
    full = standard_profile_for("openrouter:openai/gpt-4o")
    mini = standard_profile_for("openrouter:openai/gpt-4o-mini")
    assert "balanced" in full
    assert "cheap-bulk" in mini


def test_openrouter_google_gemini_pro_vs_flash():
    pro = standard_profile_for("openrouter:google/gemini-1.5-pro")
    flash = standard_profile_for("openrouter:google/gemini-1.5-flash")
    assert "long-context" in pro
    assert "cheap-bulk" in flash


def test_openrouter_deepseek_coder_specialized():
    coder = standard_profile_for("openrouter:deepseek/deepseek-coder")
    chat = standard_profile_for("openrouter:deepseek/deepseek-chat-v3.1")
    r1 = standard_profile_for("openrouter:deepseek/deepseek-r1")
    assert "coding" in coder
    assert "tool-use" in chat
    assert "deep-reasoning" in r1


def test_openrouter_meta_llama_size_tier_dictates_tags():
    big = standard_profile_for("openrouter:meta-llama/llama-3.1-405b-instruct")
    mid = standard_profile_for("openrouter:meta-llama/llama-3.3-70b-instruct")
    small = standard_profile_for("openrouter:meta-llama/llama-3.1-8b-instruct")
    assert "long-context" in big
    assert "balanced" in mid
    assert "cheap-bulk" in small


def test_openrouter_mistral_codestral_specialized():
    cs = standard_profile_for("openrouter:mistralai/codestral-2501")
    assert cs == ["coding"]


def test_openrouter_qwen_coder_specialized():
    coder = standard_profile_for("openrouter:qwen/qwen-2.5-coder-32b-instruct")
    base = standard_profile_for("openrouter:qwen/qwen-2.5-72b-instruct")
    assert coder == ["coding"]
    assert "long-context" in base


def test_openrouter_grok_falls_into_default():
    assert "coding" in standard_profile_for("openrouter:x-ai/grok-2")
    assert "coding" in standard_profile_for("openrouter:x-ai/grok-4-mini")


def test_openrouter_unknown_provider_returns_empty():
    """A provider we don't recognize gets no curated profile — honest."""
    assert standard_profile_for("openrouter:totally-unknown/some-model") == []
    assert standard_profile_for("openrouter:obscure-provider/foo") == []


def test_native_curated_wins_over_openrouter_pattern():
    """Exact dict match short-circuits before regex patterns can match."""
    assert standard_profile_for("anthropic:claude-opus-4-7") == [
        "deep-reasoning",
        "architecture",
        "security-review",
        "long-context",
    ]


def test_openrouter_pattern_returns_fresh_list():
    """Mutating the returned list doesn't poison the pattern table."""
    profile = standard_profile_for("openrouter:anthropic/claude-opus-4.7")
    profile.append("mutated")
    second = standard_profile_for("openrouter:anthropic/claude-opus-4.7")
    assert "mutated" not in second
