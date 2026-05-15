"""Curated "what this model is good for" tags — the standard recommendations
Metis ships for known models.

These are *defaults* shown in `/models` and exposed on the HTTP `/models`
response as ``task_profile``. They are **not enforcement** — routing rules
(per `routing-engine.md §5`) remain the customization layer customers use to
shape model selection for their own workloads.

Two sources of truth:

1. ``STANDARD_TASK_PROFILES`` — explicit, exact-match dict for natively
   adapted models (Anthropic, OpenAI). Curated by hand; tested.
2. ``OPENROUTER_PROFILE_PATTERNS`` — ordered regex patterns for OpenRouter
   mirrors of common upstream models. Heuristic: pattern matching can be
   wrong; when in doubt, leave the profile empty rather than mislead.

Tag vocabulary is intentionally free-form for v1 so usage can shape the
canonical set before snapping to an enum (see `STRATEGY.md §1` — Layer 1).

See `docs/standard-model-profiles.md` for the rubric behind each assignment.
"""

from __future__ import annotations

import re

# Free-form short strings; lowercase, hyphen-separated. Avoid duplicating
# information that's already in `AdapterCapabilities` (e.g. don't tag
# "vision" — `supports_images` says that). Tags answer "what tasks does this
# model excel at?" rather than "what features does it support?"
STANDARD_TASK_PROFILES: dict[str, list[str]] = {
    # Anthropic
    "anthropic:claude-opus-4-7": [
        "deep-reasoning",
        "architecture",
        "security-review",
        "long-context",
    ],
    "anthropic:claude-sonnet-4-6": [
        "coding",
        "refactoring",
        "debugging",
        "tool-use",
        "balanced",
    ],
    "anthropic:claude-haiku-4-5": [
        "commits",
        "summarization",
        "quick-edits",
        "cheap-bulk",
    ],
    # OpenAI
    "openai:gpt-5": [
        "coding",
        "tool-use",
        "balanced",
    ],
    "openai:gpt-5-mini": [
        "cheap-bulk",
        "summarization",
        "quick-edits",
    ],
}


# Heuristic profiles for OpenRouter mirrors of common upstream models.
#
# Evaluated top-to-bottom, first match wins. Order patterns *most specific
# first* within each provider so e.g. ``gpt-4o-mini`` matches before the
# broader ``gpt-4o``. Empty-result patterns are not listed — when no
# pattern matches, the model has no curated profile (which is honest:
# better than guessing).
#
# These tags ARE just opinions about typical upstream-model behavior. If
# OpenRouter routes the request to a fork or fine-tune that behaves
# differently, the tags will be slightly wrong. Customers correct via
# routing rules in their `~/.metis/routing.yaml`.
OPENROUTER_PROFILE_PATTERNS: list[tuple[re.Pattern[str], list[str]]] = [
    # ---- Anthropic mirrors -----------------------------------------------
    (
        re.compile(r"^openrouter:anthropic/.*opus"),
        ["deep-reasoning", "architecture", "security-review", "long-context"],
    ),
    (
        re.compile(r"^openrouter:anthropic/.*sonnet"),
        ["coding", "refactoring", "debugging", "tool-use", "balanced"],
    ),
    (
        re.compile(r"^openrouter:anthropic/.*haiku"),
        ["commits", "summarization", "quick-edits", "cheap-bulk"],
    ),
    # ---- OpenAI mirrors --------------------------------------------------
    # o-series (reasoning) — check before generic openai/.
    (
        re.compile(r"^openrouter:openai/o\d+-mini"),
        ["deep-reasoning", "cheap-bulk"],
    ),
    (
        re.compile(r"^openrouter:openai/o\d+"),
        ["deep-reasoning", "architecture"],
    ),
    # gpt-5 family
    (
        re.compile(r"^openrouter:openai/gpt-5-mini"),
        ["cheap-bulk", "summarization", "quick-edits"],
    ),
    (
        re.compile(r"^openrouter:openai/gpt-5-nano"),
        ["cheap-bulk", "quick-edits"],
    ),
    (
        re.compile(r"^openrouter:openai/gpt-5"),
        ["coding", "tool-use", "balanced"],
    ),
    # gpt-4 family
    (
        re.compile(r"^openrouter:openai/gpt-4o-mini"),
        ["cheap-bulk", "quick-edits"],
    ),
    (
        re.compile(r"^openrouter:openai/gpt-4o"),
        ["coding", "tool-use", "balanced"],
    ),
    (
        re.compile(r"^openrouter:openai/gpt-4-turbo"),
        ["coding", "tool-use", "long-context"],
    ),
    (
        re.compile(r"^openrouter:openai/gpt-4"),
        ["coding", "balanced"],
    ),
    (
        re.compile(r"^openrouter:openai/gpt-3\.5"),
        ["cheap-bulk", "quick-edits"],
    ),
    # ---- Google Gemini ---------------------------------------------------
    (
        re.compile(r"^openrouter:google/gemini-.*-pro"),
        ["long-context", "balanced", "coding"],
    ),
    (
        re.compile(r"^openrouter:google/gemini-.*-flash"),
        ["cheap-bulk", "quick-edits", "long-context"],
    ),
    (
        re.compile(r"^openrouter:google/gemini-"),
        ["balanced", "long-context"],
    ),
    # ---- DeepSeek --------------------------------------------------------
    (
        re.compile(r"^openrouter:deepseek/.*coder"),
        ["coding", "cheap-bulk"],
    ),
    (
        re.compile(r"^openrouter:deepseek/.*r1"),
        ["deep-reasoning", "cheap-bulk"],
    ),
    (
        re.compile(r"^openrouter:deepseek/.*chat"),
        ["coding", "tool-use", "cheap-bulk"],
    ),
    (
        re.compile(r"^openrouter:deepseek/"),
        ["coding", "cheap-bulk"],
    ),
    # ---- Meta Llama ------------------------------------------------------
    (
        re.compile(r"^openrouter:meta-llama/.*405b"),
        ["balanced", "long-context"],
    ),
    (
        re.compile(r"^openrouter:meta-llama/.*70b"),
        ["balanced", "cheap-bulk"],
    ),
    (
        re.compile(r"^openrouter:meta-llama/.*8b"),
        ["cheap-bulk", "quick-edits"],
    ),
    (
        re.compile(r"^openrouter:meta-llama/.*vision"),
        ["balanced", "long-context"],
    ),
    (
        re.compile(r"^openrouter:meta-llama/"),
        ["balanced", "cheap-bulk"],
    ),
    # ---- Mistral / Codestral --------------------------------------------
    (
        re.compile(r"^openrouter:mistralai/codestral"),
        ["coding"],
    ),
    (
        re.compile(r"^openrouter:mistralai/.*large"),
        ["balanced", "long-context"],
    ),
    (
        re.compile(r"^openrouter:mistralai/.*7b"),
        ["cheap-bulk", "quick-edits"],
    ),
    (
        re.compile(r"^openrouter:mistralai/"),
        ["balanced"],
    ),
    # ---- Qwen ------------------------------------------------------------
    (
        re.compile(r"^openrouter:qwen/.*coder"),
        ["coding"],
    ),
    (
        re.compile(r"^openrouter:qwen/.*72b"),
        ["balanced", "long-context"],
    ),
    (
        re.compile(r"^openrouter:qwen/"),
        ["balanced"],
    ),
    # ---- x-ai Grok -------------------------------------------------------
    (
        re.compile(r"^openrouter:x-ai/grok"),
        ["coding", "balanced"],
    ),
    # ---- Cohere Command --------------------------------------------------
    (
        re.compile(r"^openrouter:cohere/command-r-plus"),
        ["balanced", "long-context"],
    ),
    (
        re.compile(r"^openrouter:cohere/"),
        ["balanced"],
    ),
]


def standard_profile_for(model_id: str) -> list[str]:
    """Return the curated tag list for a canonical model id, or empty.

    Resolution order:

    1. Exact match in ``STANDARD_TASK_PROFILES`` (native Anthropic / OpenAI).
    2. First-matching ``OPENROUTER_PROFILE_PATTERNS`` regex (OpenRouter
       heuristic — covers common upstream-model families).
    3. Empty list (unknown model, customer fills in via routing rules).

    Always returns a fresh list — safe to mutate by callers.
    """
    if model_id in STANDARD_TASK_PROFILES:
        return list(STANDARD_TASK_PROFILES[model_id])
    if model_id.startswith("openrouter:"):
        for pattern, tags in OPENROUTER_PROFILE_PATTERNS:
            if pattern.search(model_id):
                return list(tags)
    return []


__all__ = [
    "OPENROUTER_PROFILE_PATTERNS",
    "STANDARD_TASK_PROFILES",
    "standard_profile_for",
]
