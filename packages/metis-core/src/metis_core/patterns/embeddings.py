"""v2 hybrid fingerprint embedding providers.

See `pattern-store.md §16.2 / §16.3`. The Protocol is the v2 entry point;
three concrete implementations cover the cost/latency/dependency-weight
spectrum (hosted-cheap / hosted-multilingual / local-no-API). Selection is
per-workspace via `PatternConfig.embedding_provider`.

The embedding cache (§16.4) lives in `PatternStore`; providers are pure
inference. The cache absorbs the minor non-determinism hosted providers
occasionally exhibit under load — once `(text, provider_id)` is cached, all
subsequent same-input turns return the same vector regardless of upstream
drift.
"""

from __future__ import annotations

import hashlib
import math
import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """v2 fingerprint embedding provider.

    Pluggable per-workspace via `PatternConfig.embedding_provider`. The
    `provider_id` forms part of the cache key (§16.4) and the
    `fingerprints.embedding_provider` column; changing the id invalidates
    cached vectors and forces re-embedding on next use.
    """

    @property
    def provider_id(self) -> str: ...

    @property
    def dim(self) -> int: ...

    @property
    def max_input_tokens(self) -> int: ...

    async def embed(self, text: str) -> tuple[float, ...]: ...

    async def aclose(self) -> None: ...


def _l2_normalize(vec: tuple[float, ...]) -> tuple[float, ...]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return tuple(x / norm for x in vec)


def _truncate_bytes(text: str, max_input_tokens: int) -> str:
    # §16.2: tokenizers are provider-specific; v2 approximates with
    # `max_input_tokens * 4` UTF-8 bytes. A non-Latin user message
    # under-truncates under this rule. Documented limitation (§16.11 q4).
    limit_bytes = max_input_tokens * 4
    encoded = text.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return text
    return encoded[:limit_bytes].decode("utf-8", errors="ignore")


class OpenAIEmbeddingProvider:
    """OpenAI `text-embedding-3-small` — $0.02 / 1M tokens, 1536-dim.

    Construction defers the SDK import + client creation so unit tests can
    import this module without an OPENAI_API_KEY in the environment.
    """

    provider_id = "openai:text-embedding-3-small"
    dim = 1536
    max_input_tokens = 8192

    def __init__(self, *, api_key: str | None = None, client: object | None = None) -> None:
        self._explicit_key = api_key
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> object:
        if self._client is not None:
            return self._client
        import openai

        api_key = self._explicit_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OpenAIEmbeddingProvider requires OPENAI_API_KEY (env or constructor)"
            )
        self._client = openai.AsyncOpenAI(api_key=api_key)
        return self._client

    async def embed(self, text: str) -> tuple[float, ...]:
        client = self._get_client()
        truncated = _truncate_bytes(text, self.max_input_tokens)
        response = await client.embeddings.create(  # type: ignore[attr-defined]
            model="text-embedding-3-small",
            input=truncated,
        )
        vec = tuple(float(x) for x in response.data[0].embedding)
        return _l2_normalize(vec)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            close = getattr(self._client, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            self._client = None


class CohereEmbeddingProvider:
    """Cohere `embed-multilingual-v3.0` — $0.10 / 1M tokens, 1024-dim.

    Uses httpx directly; Cohere SDK isn't a `metis-core` dependency.
    """

    provider_id = "cohere:embed-multilingual-v3.0"
    dim = 1024
    max_input_tokens = 512

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.cohere.com",
        client: object | None = None,
    ) -> None:
        self._explicit_key = api_key
        self._base_url = base_url
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> object:
        if self._client is not None:
            return self._client
        import httpx

        api_key = self._explicit_key or os.environ.get("COHERE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "CohereEmbeddingProvider requires COHERE_API_KEY (env or constructor)"
            )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
        return self._client

    async def embed(self, text: str) -> tuple[float, ...]:
        client = self._get_client()
        truncated = _truncate_bytes(text, self.max_input_tokens)
        response = await client.post(  # type: ignore[attr-defined]
            "/v2/embed",
            json={
                "model": "embed-multilingual-v3.0",
                "texts": [truncated],
                "input_type": "clustering",
                "embedding_types": ["float"],
            },
        )
        response.raise_for_status()
        body = response.json()
        raw = body["embeddings"]["float"][0]
        vec = tuple(float(x) for x in raw)
        return _l2_normalize(vec)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            close = getattr(self._client, "aclose", None)
            if close is not None:
                await close()
            self._client = None


class LocalEmbeddingProvider:
    """`local:sentence-transformers:all-MiniLM-L6-v2` — 384-dim, CPU-bound.

    Requires `sentence-transformers` installed (extra: `metis-patterns-local`).
    Import is deferred to first use so the bare `metis-core` install does
    not pull Torch.
    """

    provider_id = "local:sentence-transformers:all-MiniLM-L6-v2"
    dim = 384
    max_input_tokens = 256

    def __init__(self, *, model_name: str = "all-MiniLM-L6-v2") -> None:
        self._model_name = model_name
        self._model = None

    def _get_model(self) -> object:
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "LocalEmbeddingProvider requires the `metis-patterns-local` "
                "extra (sentence-transformers)"
            ) from exc
        self._model = SentenceTransformer(self._model_name)
        return self._model

    async def embed(self, text: str) -> tuple[float, ...]:
        import asyncio

        model = self._get_model()
        truncated = _truncate_bytes(text, self.max_input_tokens)
        vec_array = await asyncio.to_thread(
            model.encode,  # type: ignore[attr-defined]
            truncated,
            normalize_embeddings=True,
        )
        return tuple(float(x) for x in vec_array)

    async def aclose(self) -> None:
        self._model = None


class DeterministicEmbeddingProvider:
    """Deterministic SHA-256-derived vectors. Used by tests and as the v2
    code path's reference impl for cluster-tightening fixtures.

    Properties: same input → byte-identical vector, no API calls, no
    external dependencies. Vectors are L2-normalized so cosine reduces to
    dot product per `pattern-store.md §16.5.1`.
    """

    def __init__(self, *, provider_id: str = "deterministic:sha256:64", dim: int = 64) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self._provider_id = provider_id
        self._dim = dim

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def max_input_tokens(self) -> int:
        return 100_000

    async def embed(self, text: str) -> tuple[float, ...]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # Repeat digest as needed so `dim` floats can be drawn from it.
        needed = self._dim * 8
        extended = digest
        counter = 0
        while len(extended) < needed:
            counter += 1
            extended += hashlib.sha256(extended + counter.to_bytes(4, "big")).digest()
        values: list[float] = []
        for i in range(self._dim):
            chunk = extended[i * 8 : (i + 1) * 8]
            raw = int.from_bytes(chunk, "big", signed=False)
            values.append((raw / 2**64) * 2.0 - 1.0)
        return _l2_normalize(tuple(values))

    async def aclose(self) -> None:
        return


_PROVIDER_REGISTRY: dict[str, type] = {
    "openai:text-embedding-3-small": OpenAIEmbeddingProvider,
    "cohere:embed-multilingual-v3.0": CohereEmbeddingProvider,
    "local:sentence-transformers:all-MiniLM-L6-v2": LocalEmbeddingProvider,
}


def resolve_embedding_provider(provider_id: str | None) -> EmbeddingProvider | None:
    """Build the configured `EmbeddingProvider`. `None` keeps v1 path."""
    if provider_id is None:
        return None
    factory = _PROVIDER_REGISTRY.get(provider_id)
    if factory is None:
        raise ValueError(
            f"unknown embedding provider id: {provider_id!r}; known: {sorted(_PROVIDER_REGISTRY)}"
        )
    return factory()
