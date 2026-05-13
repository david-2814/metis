"""Shared formatting for `/models` across CLI, TUI, and HTTP.

The three surfaces (REPL print, RichLog write, JSON response) build
slightly different output, but the model-selection logic and the per-row
data shape are identical. This module owns:

- ``parse_models_command(arg)`` — slash-command argument parsing.
- ``resolve_models(...)`` — filter the registered set by mode (primary /
  all / pattern), always including the active sticky if present.
- ``format_models_lines(...)`` — text rows for `metis chat` and `metis tui`.
- ``model_dict(...)`` — structured per-model record for the HTTP response.

Filtering uses :mod:`metis_core.routing.families` to collapse OpenRouter versions
into one "primary" entry per family. Native (Anthropic / OpenAI) ids are
their own family — each registered model is curated and kept.
"""

from __future__ import annotations

from typing import Literal

from metis_core.pricing.table import PriceTable
from metis_core.routing.families import filter_by_pattern, select_primary
from metis_core.routing.registry import ModelRegistry

Mode = Literal["primary", "all", "pattern"]


def parse_models_command(arg: str) -> tuple[Mode, str | None]:
    """Parse the slash-command argument to ``/models``.

    ``""`` → ``("primary", None)``
    ``"all"`` → ``("all", None)``
    ``"<pattern>"`` → ``("pattern", pattern)``
    """
    a = arg.strip()
    if not a:
        return ("primary", None)
    if a == "all":
        return ("all", None)
    return ("pattern", a)


def resolve_models(
    *,
    registry: ModelRegistry,
    mode: Mode,
    pattern: str | None = None,
    always_include: str | None = None,
) -> tuple[list[str], int]:
    """Return ``(displayed_ids, total_registered)``.

    ``displayed_ids`` is filtered + sorted; ``total_registered`` is the
    full registry size so callers can compute "how many were hidden."
    """
    all_ids = registry.list_models()
    total = len(all_ids)

    if mode == "all":
        return (sorted(all_ids), total)
    if mode == "pattern":
        return (sorted(filter_by_pattern(all_ids, pattern or "")), total)

    # primary
    primary = select_primary(all_ids)
    if always_include and always_include in all_ids and always_include not in primary:
        primary.append(always_include)
    return (sorted(primary), total)


def format_pricing_inline(model_id: str, pricing: PriceTable) -> str:
    """Format pricing for a model id, or '—' if unknown."""
    if model_id not in pricing:
        return "—"
    rates = pricing.pricing_for(model_id)
    return f"${rates.input_per_mtok:.2f} in / ${rates.output_per_mtok:.2f} out / MTok"


_INDENT_PER_LEVEL = 2
_MARKER_WIDTH = 2  # "* " or "  "


def format_task_profile(task_profile: tuple[str, ...]) -> str:
    """Render the curated task tags as ``[tag1, tag2, ...]`` or empty string."""
    if not task_profile:
        return ""
    return "[" + ", ".join(task_profile) + "]"


def format_models_lines(
    model_ids: list[str],
    *,
    registry: ModelRegistry,
    pricing: PriceTable,
    sticky_model: str | None = None,
) -> list[str]:
    """Return formatted lines with provider/namespace nesting.

    Model ids are split on ``:`` (provider boundary) and then on ``/``
    (namespace boundary inside OpenRouter-style ids). Internal nodes become
    header rows with a trailing colon; leaves carry pricing and (when known)
    a curated "good for" task profile.

    Example::

        anthropic:
          * claude-opus-4-7     $15.00 in / $75.00 out / MTok    [deep-reasoning, architecture]
            claude-sonnet-4-6   $3.00 in / $15.00 out / MTok     [coding, refactoring]
        openrouter:
          deepseek:
            deepseek-chat-v3.1  $0.30 in / $0.90 out / MTok      [coding, cheap-bulk]

    The active sticky model (if displayed) gets a ``*`` immediately before
    its leaf name; non-sticky leaves get two spaces in that column so leaf
    names line up across siblings.

    Columns: prefix and pricing are both aligned across the whole tree, so
    profile labels start at a consistent x-offset. Profile labels themselves
    are *not* padded — long ones overflow off the right edge rather than
    inflate column widths. Models with no curated profile contribute nothing
    after the pricing column.
    """
    if not model_ids:
        return ["  (no models match)"]

    tree = _build_tree(model_ids)
    leaves = _gather_leaves(tree, depth=0)
    if not leaves:
        return ["  (no models match)"]

    # The leaf-prefix column (indent + marker + name) is aligned across the
    # whole tree. The pricing column is aligned too — its width is the max
    # rendered pricing string across all leaves — so the trailing profile
    # column starts at a consistent x-offset. Profiles are appended without
    # padding; long labels overflow rather than dictate column widths.
    max_prefix = max(
        _INDENT_PER_LEVEL * depth + _MARKER_WIDTH + len(name) for depth, name, _ in leaves
    )
    max_price = max(len(format_pricing_inline(mid, pricing)) for _, _, mid in leaves)
    any_profile = False
    for _, _, mid in leaves:
        try:
            if registry.get(mid).task_profile:
                any_profile = True
                break
        except KeyError:
            continue

    lines: list[str] = []
    _emit(
        tree,
        depth=0,
        lines=lines,
        max_prefix=max_prefix,
        max_price=max_price,
        any_profile=any_profile,
        registry=registry,
        pricing=pricing,
        sticky=sticky_model,
    )
    return lines


def _build_tree(model_ids: list[str]) -> dict:
    """Split each id on `:` then `/`; build a nested dict where leaves map
    to the full original id.

    Example: ``"openrouter:deepseek/deepseek-chat-v3.1"`` becomes
    ``{"openrouter": {"deepseek": {"deepseek-chat-v3.1": "openrouter:deepseek/deepseek-chat-v3.1"}}}``.

    Variant suffixes (``:free``, ``:nitro``) inside a path are NOT split — only
    the first ``:`` (provider boundary) and any ``/`` afterwards.
    """
    tree: dict = {}
    for mid in model_ids:
        head, sep, tail = mid.partition(":")
        if not sep:
            segments = [head]
        else:
            segments = [head, *tail.split("/")]
        node = tree
        for seg in segments[:-1]:
            child = node.setdefault(seg, {})
            if not isinstance(child, dict):
                # A previous id was a leaf at this segment; collision means
                # the input has both `foo:bar` and `foo:bar/baz`. Promote the
                # leaf to a dict with `""` as a leaf-self key.
                node[seg] = {"": child}
                child = node[seg]
            node = child
        node[segments[-1]] = mid
    return tree


def _gather_leaves(tree: dict, *, depth: int) -> list[tuple[int, str, str]]:
    """Walk the tree and collect ``(depth, leaf_name, full_id)`` tuples in
    the order they will be emitted (alphabetical by segment at each level).
    """
    out: list[tuple[int, str, str]] = []
    for key in sorted(tree.keys()):
        val = tree[key]
        if isinstance(val, dict):
            out.extend(_gather_leaves(val, depth=depth + 1))
        else:
            out.append((depth, key, val))
    return out


def _emit(
    tree: dict,
    *,
    depth: int,
    lines: list[str],
    max_prefix: int,
    max_price: int,
    any_profile: bool,
    registry: ModelRegistry,
    pricing: PriceTable,
    sticky: str | None,
) -> None:
    indent = " " * (_INDENT_PER_LEVEL * depth)
    for key in sorted(tree.keys()):
        val = tree[key]
        if isinstance(val, dict):
            lines.append(f"{indent}{key}:")
            _emit(
                val,
                depth=depth + 1,
                lines=lines,
                max_prefix=max_prefix,
                max_price=max_price,
                any_profile=any_profile,
                registry=registry,
                pricing=pricing,
                sticky=sticky,
            )
        else:
            mid = val
            marker = "*" if mid == sticky else " "
            prefix = f"{indent}{marker} {key}"
            prefix_pad = " " * (max_prefix - len(prefix))
            try:
                profile_text = format_task_profile(registry.get(mid).task_profile)
            except KeyError:
                profile_text = ""
            price = format_pricing_inline(mid, pricing)
            # Column order: prefix → pricing (aligned) → profile (overflow).
            # When no leaf has tags, omit the profile column entirely.
            if not any_profile:
                lines.append(f"{prefix}{prefix_pad}  {price}")
            else:
                price_pad = " " * (max_price - len(price))
                profile_segment = f"  {profile_text}" if profile_text else ""
                lines.append(f"{prefix}{prefix_pad}  {price}{price_pad}{profile_segment}")


def truncation_hint(
    displayed: list[str], total: int, *, mode: Mode, pattern: str | None
) -> str | None:
    """Footer hint when entries were filtered out.

    Returned only for ``primary`` mode with non-zero hidden count, so users
    discover the ``/models all`` escape hatch.
    """
    if mode != "primary":
        return None
    hidden = total - len(displayed)
    if hidden <= 0:
        return None
    return f"  ({hidden} more — `/models all` to list every version, `/models <pattern>` to filter)"


def model_dict(
    model_id: str,
    *,
    registry: ModelRegistry,
    pricing: PriceTable,
) -> dict:
    """Structured record for HTTP /models. Decimals serialized as strings
    (consistent with `Usage.cost_usd` elsewhere).
    """
    entry = registry.get(model_id)
    caps = entry.capabilities
    record: dict = {
        "id": model_id,
        "adapter": registry.provider_of(model_id),
        "aliases": list(entry.aliases),
        "task_profile": list(entry.task_profile),
        "capabilities": {
            "supports_images": caps.supports_images,
            "supports_tools": caps.supports_tools,
            "max_context_tokens": caps.max_context_tokens,
            "max_output_tokens": caps.max_output_tokens,
        },
        "availability": "healthy",
        "pricing": None,
    }
    if model_id in pricing:
        rates = pricing.pricing_for(model_id)
        record["pricing"] = {
            "input_per_mtok": str(rates.input_per_mtok),
            "output_per_mtok": str(rates.output_per_mtok),
            "cached_read_per_mtok": str(rates.cached_read_per_mtok),
            "cache_creation_per_mtok": str(rates.cache_creation_per_mtok),
            "currency": "USD",
            "pricing_version": pricing.version,
        }
    return record


__all__ = [
    "Mode",
    "format_models_lines",
    "format_pricing_inline",
    "format_task_profile",
    "model_dict",
    "parse_models_command",
    "resolve_models",
    "truncation_hint",
]
