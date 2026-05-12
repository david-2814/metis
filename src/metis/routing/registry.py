"""Model registry: canonical model id ↔ adapter + capabilities + aliases.

Per provider-adapter-contract.md §8.1. The routing engine consults this to:
- resolve aliases (sonnet → anthropic:claude-sonnet-4-6)
- check that a model is configured (has an adapter + API key)
- look up capabilities for validation
- identify the provider of a model (for availability tracking)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from metis.adapters.protocol import ProviderAdapter
from metis.canonical.capabilities import AdapterCapabilities


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


@dataclass(frozen=True)
class ModelEntry:
    model_id: str  # canonical "provider:name"
    adapter: ProviderAdapter
    capabilities: AdapterCapabilities
    aliases: tuple[str, ...] = field(default_factory=tuple)


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
    ) -> ModelEntry:
        capabilities = adapter.capabilities_for(model_id)
        entry = ModelEntry(
            model_id=model_id,
            adapter=adapter,
            capabilities=capabilities,
            aliases=tuple(aliases or ()),
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

    def list_models(self) -> list[str]:
        return sorted(self._entries.keys())

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, model_id: object) -> bool:
        return model_id in self._entries
