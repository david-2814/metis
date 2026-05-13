"""Test helpers for routing tests (sibling to conftest.py).

Lives here rather than inside `conftest.py` so other test files can import
`StubAdapter` by name. Pytest's `conftest.py` is reserved for fixtures and
hooks; it's intentionally not on `sys.path`, so symbols defined inside it
aren't reliably importable.
"""

from __future__ import annotations

from dataclasses import dataclass

from metis_core.canonical.capabilities import AdapterCapabilities


@dataclass
class StubAdapter:
    """Minimal stand-in for ProviderAdapter for routing tests."""

    name: str = "anthropic"
    caps_map: dict[str, AdapterCapabilities] | None = None

    def capabilities_for(self, model: str) -> AdapterCapabilities:
        return (self.caps_map or {})[model]

    async def complete(self, request):  # pragma: no cover — not exercised here
        raise NotImplementedError

    async def cancel(self, request_id: str) -> bool:  # pragma: no cover
        return False

    async def close(self) -> None:  # pragma: no cover
        return

    def estimate_input_tokens(self, *args, **kwargs) -> int:  # pragma: no cover
        return 0

    def stream(self, request):  # pragma: no cover
        raise NotImplementedError
