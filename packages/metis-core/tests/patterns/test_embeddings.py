"""EmbeddingProvider Protocol contract + deterministic-impl tests.

Covers pattern-store.md §16.10 test 1 (Protocol contract) and §16.10 test 2
(determinism) for the in-tree `DeterministicEmbeddingProvider`. The
network-backed providers are unit-tested here only to the extent that
construction without an API key fails fast and the registry resolves
provider ids; the SDK-backed embed paths are covered by integration smoke,
not unit tests.
"""

from __future__ import annotations

import math

import pytest
from metis_core.patterns.embeddings import (
    CohereEmbeddingProvider,
    DeterministicEmbeddingProvider,
    EmbeddingProvider,
    LocalEmbeddingProvider,
    OpenAIEmbeddingProvider,
    resolve_embedding_provider,
)


async def test_protocol_runtime_checkable_accepts_deterministic_impl() -> None:
    provider = DeterministicEmbeddingProvider()
    assert isinstance(provider, EmbeddingProvider)


async def test_protocol_runtime_checkable_rejects_class_missing_methods() -> None:
    class _Partial:
        provider_id = "x"
        dim = 1

    assert not isinstance(_Partial(), EmbeddingProvider)


async def test_deterministic_provider_returns_unit_normalized_vector() -> None:
    provider = DeterministicEmbeddingProvider(dim=32)
    vec = await provider.embed("the rain in spain")
    assert len(vec) == 32
    norm = math.sqrt(sum(x * x for x in vec))
    assert norm == pytest.approx(1.0, abs=1e-6)


async def test_deterministic_provider_is_deterministic() -> None:
    provider = DeterministicEmbeddingProvider(dim=16)
    a = await provider.embed("refactor the auth middleware")
    b = await provider.embed("refactor the auth middleware")
    assert a == b


async def test_deterministic_provider_distinguishes_different_text() -> None:
    provider = DeterministicEmbeddingProvider(dim=16)
    a = await provider.embed("write a doc")
    b = await provider.embed("refactor a module")
    assert a != b


async def test_deterministic_provider_aclose_is_idempotent() -> None:
    provider = DeterministicEmbeddingProvider()
    await provider.aclose()
    await provider.aclose()


async def test_deterministic_provider_rejects_zero_dim() -> None:
    with pytest.raises(ValueError):
        DeterministicEmbeddingProvider(dim=0)


def test_resolve_embedding_provider_none_returns_none() -> None:
    assert resolve_embedding_provider(None) is None


def test_resolve_embedding_provider_unknown_raises() -> None:
    with pytest.raises(ValueError):
        resolve_embedding_provider("unknown:provider")


def test_resolve_embedding_provider_openai_builds_instance() -> None:
    # Constructing the provider does not require an API key — the key is
    # read on first embed call. The provider_id is the registry key.
    provider = resolve_embedding_provider("openai:text-embedding-3-small")
    assert isinstance(provider, OpenAIEmbeddingProvider)
    assert provider.provider_id == "openai:text-embedding-3-small"
    assert provider.dim == 1536


def test_resolve_embedding_provider_cohere_builds_instance() -> None:
    provider = resolve_embedding_provider("cohere:embed-multilingual-v3.0")
    assert isinstance(provider, CohereEmbeddingProvider)
    assert provider.dim == 1024


def test_resolve_embedding_provider_local_builds_instance() -> None:
    # Local provider construction defers the heavy sentence-transformers
    # import to first embed; we only verify the registry maps the id.
    provider = resolve_embedding_provider("local:sentence-transformers:all-MiniLM-L6-v2")
    assert isinstance(provider, LocalEmbeddingProvider)
    assert provider.dim == 384


async def test_openai_provider_requires_api_key_on_embed() -> None:
    import os

    saved = os.environ.pop("OPENAI_API_KEY", None)
    try:
        provider = OpenAIEmbeddingProvider()
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            await provider.embed("hello")
    finally:
        if saved is not None:
            os.environ["OPENAI_API_KEY"] = saved


async def test_cohere_provider_requires_api_key_on_embed() -> None:
    import os

    saved = os.environ.pop("COHERE_API_KEY", None)
    try:
        provider = CohereEmbeddingProvider()
        with pytest.raises(RuntimeError, match="COHERE_API_KEY"):
            await provider.embed("hello")
    finally:
        if saved is not None:
            os.environ["COHERE_API_KEY"] = saved
