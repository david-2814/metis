"""Model registry: canonical model id ↔ adapter + capabilities + aliases.

Per provider-adapter-contract.md §8.1. The routing engine consults this to:
- resolve aliases (sonnet → anthropic:claude-sonnet-4-6)
- check that a model is configured (has an adapter + API key)
- look up capabilities for validation
- identify the provider of a model (for availability tracking)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from metis.core.adapters.protocol import ProviderAdapter
from metis.core.canonical.capabilities import AdapterCapabilities


class UnknownModelError(KeyError):
    """Raised when a model id isn't registered."""

    def __init__(self, model_id: str) -> None:
        super().__init__(model_id)
        self.model_id = model_id


class DuplicateAliasError(ValueError):
    def __init__(self, alias: str, existing: str, attempted: str) -> None:
        super().__init__(
            f"alias {alias!r} already maps to {existing!r}; cannot also map to {attempted!r}"
        )


DelegationTier = str  # "fast" | "balanced" | "deep" by convention (delegation.md §4.2)


@dataclass(frozen=True)
class ModelEntry:
    model_id: str  # canonical "provider:name"
    adapter: ProviderAdapter
    capabilities: AdapterCapabilities
    aliases: tuple[str, ...] = field(default_factory=tuple)
    task_profile: tuple[str, ...] = field(default_factory=tuple)
    """Curated "what this model is good for" tags — short freeform strings
    like ``"deep-reasoning"`` or ``"commits"``. These are *recommendations*,
    not enforcement: routing rules (per `routing-engine.md §5`) are the
    customization layer customers use to override them. See
    `docs/standard-model-profiles.md` for the curated vocabulary and the
    defaults shipped for known models."""
    can_delegate: bool = False
    """Whether the `delegate()` tool is registered when this model is the
    active planner model (delegation.md §3.1, §4.2). Default `False` so
    delegation stays opt-in per registry. A worker session never sees the
    tool regardless of this flag (§5.6)."""
    delegation_tier: str | None = None
    """When non-`None`, this model is a candidate for `delegate(tier=...)`
    via `ModelRegistry.model_for_tier`. Convention is `fast` / `balanced`
    / `deep` (delegation.md §4.2). Multiple models can share a tier; the
    first registered wins."""


class ModelRegistry:
    """Registry of configured models. Built at server startup."""

    def __init__(self) -> None:
        self._entries: dict[str, ModelEntry] = {}
        self._alias_to_model: dict[str, str] = {}

    def register(
        self,
        *,
        model_id: str,
        adapter: ProviderAdapter,
        aliases: list[str] | None = None,
        task_profile: list[str] | None = None,
        can_delegate: bool = False,
        delegation_tier: str | None = None,
    ) -> ModelEntry:
        capabilities = adapter.capabilities_for(model_id)
        entry = ModelEntry(
            model_id=model_id,
            adapter=adapter,
            capabilities=capabilities,
            aliases=tuple(aliases or ()),
            task_profile=tuple(task_profile or ()),
            can_delegate=can_delegate,
            delegation_tier=delegation_tier,
        )
        self._entries[model_id] = entry
        # The model id itself is its own alias.
        self._alias_to_model[model_id] = model_id
        for alias in entry.aliases:
            existing = self._alias_to_model.get(alias)
            if existing is not None and existing != model_id:
                raise DuplicateAliasError(alias, existing, model_id)
            self._alias_to_model[alias] = model_id
        return entry

    def unregister(self, model_id: str) -> None:
        entry = self._entries.pop(model_id, None)
        if entry is None:
            return
        for alias in (*entry.aliases, model_id):
            if self._alias_to_model.get(alias) == model_id:
                self._alias_to_model.pop(alias, None)

    # ---- Lookups -------------------------------------------------------

    def get(self, model_id: str) -> ModelEntry:
        entry = self._entries.get(model_id)
        if entry is None:
            raise UnknownModelError(model_id)
        return entry

    def is_configured(self, model_id: str) -> bool:
        return model_id in self._entries

    def adapter_for(self, model_id: str) -> ProviderAdapter:
        return self.get(model_id).adapter

    def capabilities_for(self, model_id: str) -> AdapterCapabilities:
        return self.get(model_id).capabilities

    def resolve_alias(self, alias_or_id: str) -> str | None:
        """Return the canonical model id for `alias_or_id`, or None if unknown."""
        return self._alias_to_model.get(alias_or_id)

    def provider_of(self, model_id: str) -> str:
        """Extract the provider prefix from a canonical model id."""
        if ":" not in model_id:
            return model_id
        return model_id.split(":", 1)[0]

    def can_delegate(self, model_id: str) -> bool:
        """Whether `model_id` is a planner-capable model (delegation.md §3.1)."""
        entry = self._entries.get(model_id)
        return bool(entry and entry.can_delegate)

    def model_for_tier(self, tier: str) -> str | None:
        """First registered model with `delegation_tier == tier`, or None.

        Convention tiers: `fast` / `balanced` / `deep` (delegation.md §4.2).
        Caller resolves "tier unsupported" → `no_model_available_for_tier`.
        """
        for entry in self._entries.values():
            if entry.delegation_tier == tier:
                return entry.model_id
        return None

    def list_models(self) -> list[str]:
        return sorted(self._entries.keys())

    def find_by_suffix(self, input: str) -> list[str]:
        """Return canonical model ids whose tail matches `input` at a boundary.

        A "boundary" is the ``:`` separating provider from name, or any ``/``
        inside an OpenRouter-style id. This rejects mid-name substring
        matches that are likely typos. An input that is itself a full
        registered canonical id is also returned (the boundary check is
        skipped when the lengths match).

        Used by the `/model` resolver to let users type the visible tail of
        a model id and have it auto-resolved when unambiguous.

        Examples (with `openrouter:openai/gpt-oss-20b` and
        `openai:gpt-5`, `openrouter:openai/gpt-5` registered)::

            find_by_suffix("gpt-oss-20b")    → ["openrouter:openai/gpt-oss-20b"]
            find_by_suffix("openai/gpt-5")   → ["openrouter:openai/gpt-5"]
            find_by_suffix("gpt-5")          → ["openai:gpt-5", "openrouter:openai/gpt-5"]
            find_by_suffix("t-5")            → []  # boundary check fails
        """
        if not input:
            return []
        out: list[str] = []
        for model_id in self._entries:
            if not model_id.endswith(input):
                continue
            if len(model_id) == len(input):
                # Whole-id match; no boundary character to check.
                out.append(model_id)
                continue
            before = model_id[-len(input) - 1]
            if before in (":", "/"):
                out.append(model_id)
        return sorted(out)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, model_id: object) -> bool:
        return model_id in self._entries
