"""Structural fingerprint of a turn.

Mechanical only — no LLM calls, no embeddings. Pulled from the turn's
canonical state (history, tool definitions, user message) so the same input
yields the same fingerprint id across re-runs.

See `pattern-store.md §5`. The fingerprint is the unit of K-NN lookup: the
routing engine builds one for the current turn and the store finds the K
nearest neighbors among recorded outcomes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import msgspec

from metis_core.canonical.ids import next_monotonic_ulid

# Mechanical intent regex per spec §5.2. Each entry maps an intent tag to a
# case-insensitive substring/keyword pattern. The order is stable; ties
# preserved by tuple sort at the end.
_INTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("commit", re.compile(r"\b(commit|stage|git\s+add)\b", re.IGNORECASE)),
    (
        "refactor",
        re.compile(r"\b(refactor|rename|extract|inline|cleanup|simplif)\w*\b", re.IGNORECASE),
    ),
    (
        "architecture",
        re.compile(r"\b(architect\w*|design|module|abstraction|interface)\b", re.IGNORECASE),
    ),
    ("debug", re.compile(r"\b(debug|bug|error|stack\s*trace|broken|fails?)\b", re.IGNORECASE)),
    ("doc", re.compile(r"\b(doc|docs|document|readme|comment|docstring)\b", re.IGNORECASE)),
    ("test", re.compile(r"\b(test|tests|pytest|unit\s*test|e2e)\b", re.IGNORECASE)),
)


def _intent_tags(user_message_text: str) -> tuple[str, ...]:
    """Return the matched intent tags in spec order."""
    matched = [tag for tag, pattern in _INTENT_PATTERNS if pattern.search(user_message_text)]
    return tuple(matched)


def _token_bucket(tokens: int) -> int:
    """Log10 bucket: 0=<1k, 1=1k-10k, 2=10k-100k, 3=100k+."""
    if tokens < 1_000:
        return 0
    if tokens < 10_000:
        return 1
    if tokens < 100_000:
        return 2
    return 3


def _workspace_hash(workspace_path: str) -> str:
    """SHA-256 of the absolute workspace path. Mirrors event-bus §6.1."""
    return hashlib.sha256(workspace_path.encode("utf-8")).hexdigest()


class FingerprintKind(StrEnum):
    """Closed enum. Adding a kind is a deliberate spec change."""

    STRUCTURAL = "structural"
    HYBRID = "hybrid"


class StructuralFeatures(msgspec.Struct, frozen=True):
    """The deterministic shape of a turn.

    Computed from session state at routing time. Used as the v1 fingerprint
    and as the structural half of v2 hybrids.
    """

    file_extensions: tuple[str, ...]
    file_path_buckets: tuple[str, ...]
    tool_names: tuple[str, ...]
    side_effect_classes: tuple[str, ...]
    has_images: bool
    has_tool_calls_in_history: bool
    estimated_input_tokens_bucket: int
    intent_tags: tuple[str, ...]
    workspace_hash: str
    workload_id: str | None = None


class Fingerprint(msgspec.Struct, frozen=True):
    """The full fingerprint stored in the pattern store.

    For v1 `kind == STRUCTURAL` and the embedding fields are None. The schema
    admits hybrid fingerprints additively for v2.
    """

    id: str
    kind: FingerprintKind
    structural: StructuralFeatures
    embedding: tuple[float, ...] | None
    embedding_provider: str | None
    embedding_dim: int | None
    created_at: datetime


@dataclass(frozen=True)
class FingerprintInputs:
    """Raw inputs the fingerprinter needs to derive `StructuralFeatures`.

    The session manager already computes most of these for the turn context.
    The pattern subscriber/extractor maps from the trace events into this
    shape; tests can construct it directly.

    `embedding` and `embedding_provider` are populated on the v2 path
    (pattern-store.md §16); v1 callers leave them None and the resulting
    `Fingerprint.kind` stays `STRUCTURAL`.
    """

    user_message_text: str
    workspace_path: str
    estimated_input_tokens: int
    has_images: bool
    has_tool_calls_in_history: bool
    file_extensions: tuple[str, ...] = ()
    file_path_buckets: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()
    side_effect_classes: tuple[str, ...] = ()
    workload_id: str | None = None
    embedding: tuple[float, ...] | None = None
    embedding_provider: str | None = None


def derive_fingerprint_inputs(
    *,
    user_message_text: str,
    workspace_path: str,
    estimated_input_tokens: int,
    has_images: bool,
    has_tool_calls_in_history: bool,
    files_touched: tuple[str, ...] = (),
    tool_names: tuple[str, ...] = (),
    side_effect_classes: tuple[str, ...] = (),
    workload_id: str | None = None,
) -> FingerprintInputs:
    """Convenience: derive `file_extensions` and `file_path_buckets` from a
    flat list of file paths.

    Extensions are lowercased and include the leading dot. Buckets are the
    top-level workspace-relative directory (or the file itself when at root).
    """
    extensions: set[str] = set()
    buckets: set[str] = set()
    for raw in files_touched:
        if not raw:
            continue
        normalized = raw.replace("\\", "/").lstrip("./")
        if "." in normalized.rsplit("/", 1)[-1]:
            tail = normalized.rsplit("/", 1)[-1]
            ext = "." + tail.rsplit(".", 1)[-1].lower()
            extensions.add(ext)
        head = normalized.split("/", 1)[0]
        if head:
            buckets.add(head)
    return FingerprintInputs(
        user_message_text=user_message_text,
        workspace_path=workspace_path,
        estimated_input_tokens=estimated_input_tokens,
        has_images=has_images,
        has_tool_calls_in_history=has_tool_calls_in_history,
        file_extensions=tuple(sorted(extensions)),
        file_path_buckets=tuple(sorted(buckets)),
        tool_names=tuple(sorted(set(tool_names))),
        side_effect_classes=tuple(sorted(set(side_effect_classes))),
        workload_id=workload_id,
    )


def build_structural_features(inputs: FingerprintInputs) -> StructuralFeatures:
    """Build a `StructuralFeatures` from raw inputs.

    Pure function: same inputs ⇒ same output. The fingerprint id is *not*
    derived from the features (it is a fresh ULID); use `structural_signature`
    to obtain the deterministic dedup key.
    """
    return StructuralFeatures(
        file_extensions=tuple(sorted(set(inputs.file_extensions))),
        file_path_buckets=tuple(sorted(set(inputs.file_path_buckets))),
        tool_names=tuple(sorted(set(inputs.tool_names))),
        side_effect_classes=tuple(sorted(set(inputs.side_effect_classes))),
        has_images=inputs.has_images,
        has_tool_calls_in_history=inputs.has_tool_calls_in_history,
        estimated_input_tokens_bucket=_token_bucket(inputs.estimated_input_tokens),
        intent_tags=_intent_tags(inputs.user_message_text),
        workspace_hash=_workspace_hash(inputs.workspace_path),
        workload_id=inputs.workload_id,
    )


def compute_fingerprint(inputs: FingerprintInputs, *, now: datetime | None = None) -> Fingerprint:
    """Build a fresh `Fingerprint` (with a new ULID id) for the given inputs.

    Note: the id is fresh per call; dedup happens at the store layer via
    `structural_signature`.

    When `inputs.embedding` is set the resulting fingerprint is `HYBRID`
    (v2; pattern-store.md §16); otherwise it is `STRUCTURAL` (v1).
    """
    features = build_structural_features(inputs)
    if inputs.embedding is not None:
        kind = FingerprintKind.HYBRID
        embedding = tuple(float(x) for x in inputs.embedding)
        embedding_dim: int | None = len(embedding)
    else:
        kind = FingerprintKind.STRUCTURAL
        embedding = None
        embedding_dim = None
    return Fingerprint(
        id=str(next_monotonic_ulid()),
        kind=kind,
        structural=features,
        embedding=embedding,
        embedding_provider=inputs.embedding_provider if embedding is not None else None,
        embedding_dim=embedding_dim,
        created_at=now or datetime.now(UTC),
    )


def text_sha256(text: str) -> str:
    """SHA-256 of the user message text. The v2 embedding cache key.

    Same pre-image as the v1 structural dedup basis (pattern-store.md
    §16.4.1), so re-running an identical user message in the same
    workspace under v2 hits both caches without recomputation.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def attach_embedding_for_recording(
    inputs: FingerprintInputs,
    *,
    store: object,
    embedder: object,
) -> FingerprintInputs:
    """Recording-path embed lookup: cache-first, API call on miss.

    On miss the embedder is awaited and the result is written to the
    cache (`pattern-store.md §16.4.4`). Returns inputs with `embedding`
    + `embedding_provider` set so `compute_fingerprint` produces a
    HYBRID fingerprint. The function is async because the embedder may
    perform network I/O; the cache path is sync SQLite.

    Used by the pattern subscriber (`subscriber.py`); the routing engine
    uses the sync cache-only path instead (`engine._attach_cached_embedding`).
    """
    from dataclasses import replace

    provider_id = embedder.provider_id  # type: ignore[attr-defined]
    cached = store.lookup_embedding(inputs.user_message_text, provider_id)  # type: ignore[attr-defined]
    if cached is None:
        vector = await embedder.embed(inputs.user_message_text)  # type: ignore[attr-defined]
        store.store_embedding(inputs.user_message_text, provider_id, vector)  # type: ignore[attr-defined]
    else:
        vector = cached
    return replace(inputs, embedding=vector, embedding_provider=provider_id)


def structural_signature(features: StructuralFeatures) -> str:
    """SHA-256 of the canonical-form structural feature set.

    Stable across re-runs and across processes; used as the dedup key when
    the store decides whether a write is a new fingerprint or an outcome
    update on an existing one. `workspace_hash` participates so a structurally
    identical turn in a different workspace still gets its own row.
    """
    # Canonical-form JSON: msgspec sorts struct fields by definition order.
    canonical = msgspec.json.encode(features)
    return hashlib.sha256(canonical).hexdigest()
