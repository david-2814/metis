"""Model "family" grouping and primary-version selection.

OpenRouter's catalog frequently carries multiple versions of the same model
(`openrouter:anthropic/claude-opus-4`, `openrouter:anthropic/claude-opus-4.7`,
date-pinned releases like `openrouter:openai/gpt-4-turbo-2024-04-09`, etc.).
The user-facing `/models` listing benefits from showing one "primary" entry
per family by default; `/models all` and `/models <pattern>` are the escape
hatches when the user needs the full set.

This module computes two things:

- ``family_key(model_id)`` — strips trailing version-like tokens (semver and
  ISO/compact dates) so siblings of the same family collapse to one key.
  Only applied to OpenRouter ids; native ids (Anthropic, OpenAI) are their
  own family — each registered model is curated and unique by construction.

- ``select_primary(model_ids)`` — for each family, returns the "latest"
  member (highest version tuple; ties broken by lex). Models that don't look
  versioned end up in single-member families and survive untouched.

The grouping is heuristic. Edge cases (`gpt-5-mini` vs `gpt-5` — siblings,
not versions of each other) are sidestepped by restricting stripping to
OpenRouter ids. The exposed escape hatches handle cases the heuristic gets
wrong.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_OPENROUTER_PREFIX = "openrouter:"

# Trailing date forms: `-2024-04-09` (ISO dashed) or `-20240229` (compact 8-digit).
_DATE_DASHED = re.compile(r"-\d{4}-\d{2}-\d{2}$")
_DATE_COMPACT = re.compile(r"-\d{8}$")
# Trailing semver-ish version: `-4`, `-4.7`, `-4.7.1` (numeric segments only).
_VERSION_SUFFIX = re.compile(r"-\d+(?:\.\d+)*$")


def family_key(model_id: str) -> str:
    """Return the "family" key for grouping model ids.

    For non-OpenRouter ids: returns the id unchanged. Each curated native
    model (Anthropic / OpenAI) is its own family.

    For OpenRouter ids: strips trailing date / semver tokens. Examples::

        openrouter:anthropic/claude-opus-4.7  →  openrouter:anthropic/claude-opus
        openrouter:anthropic/claude-opus-4    →  openrouter:anthropic/claude-opus
        openrouter:openai/gpt-4-turbo-2024-04-09  →  openrouter:openai/gpt-4-turbo
        openrouter:meta-llama/llama-3-70b-instruct → openrouter:meta-llama/llama-3-70b-instruct
            (no trailing version pattern; stays whole)
    """
    if not model_id.startswith(_OPENROUTER_PREFIX):
        return model_id
    out = model_id
    out = _DATE_DASHED.sub("", out)
    out = _DATE_COMPACT.sub("", out)
    out = _VERSION_SUFFIX.sub("", out)
    return out


def version_key(model_id: str) -> tuple[int, ...]:
    """Return a sortable version tuple parsed from the id's trailing suffix.

    Used to pick the "latest" within a family. Returns ``(-1,)`` when no
    trailing version is found, so unversioned siblings sort below versioned
    ones (i.e. ``claude-opus`` is older than ``claude-opus-4``).
    """
    # Date forms — read as a single big int so they sort newest-first.
    m = re.search(r"-(\d{4})-(\d{2})-(\d{2})$", model_id)
    if m:
        return (int("".join(m.groups())),)
    m = re.search(r"-(\d{8})$", model_id)
    if m:
        return (int(m.group(1)),)
    # Semver-ish.
    m = re.search(r"-(\d+(?:\.\d+)*)$", model_id)
    if m:
        return tuple(int(part) for part in m.group(1).split("."))
    return (-1,)


def select_primary(model_ids: Iterable[str]) -> list[str]:
    """Reduce ``model_ids`` to one entry per family, sorted by family key.

    Within each family, the member with the highest ``version_key`` wins.
    Ties are broken by lex order on the original id (stable, deterministic).
    """
    by_family: dict[str, list[str]] = {}
    for mid in model_ids:
        by_family.setdefault(family_key(mid), []).append(mid)
    primary: list[str] = []
    for fam in sorted(by_family):
        members = by_family[fam]
        members.sort(key=lambda m: (version_key(m), m))
        primary.append(members[-1])
    return primary


def filter_by_pattern(model_ids: Iterable[str], pattern: str) -> list[str]:
    """Case-insensitive substring match across the id, aliases would be the
    caller's job. Returns matches in input order.
    """
    needle = pattern.lower()
    return [mid for mid in model_ids if needle in mid.lower()]


__all__ = [
    "family_key",
    "filter_by_pattern",
    "select_primary",
    "version_key",
]
