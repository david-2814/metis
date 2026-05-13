"""Per-session bidirectional tool id map.

See canonical-message-format.md §6.2 and provider-adapter-contract.md §4.1.1.

Adapters maintain this map so canonical tool_use_ids (tu_<ulid>) survive
provider swaps. For a session that runs entirely on one provider where the
provider accepts our canonical id as-is (Anthropic), the map can be identity.
For OpenAI (which issues call_* ids), the map records the translation.
"""

from __future__ import annotations


class ToolIdMap:
    """Mutable bidirectional map. Not thread-safe; one per session."""

    def __init__(self) -> None:
        self._c2p: dict[str, str] = {}
        self._p2c: dict[str, str] = {}

    def remember(self, canonical_id: str, provider_id: str) -> None:
        self._c2p[canonical_id] = provider_id
        self._p2c[provider_id] = canonical_id

    def to_provider(self, canonical_id: str) -> str | None:
        return self._c2p.get(canonical_id)

    def to_canonical(self, provider_id: str) -> str | None:
        return self._p2c.get(provider_id)

    def has_canonical(self, canonical_id: str) -> bool:
        return canonical_id in self._c2p

    def __len__(self) -> int:
        return len(self._c2p)
