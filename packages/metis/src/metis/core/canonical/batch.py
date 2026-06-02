"""Canonical types for asynchronous batch submission.

See `provider-adapter-contract.md §4.6`. Batch submission is an additive
adapter capability that opts in to the provider's batch endpoint at the
flat 50% input + output discount. Turnaround is "best-effort, target 24h",
so this surface is consumed by offline tooling (`metis evaluate
--batch-mode`, `scripts/benchmark.py --batch-mode`) — never by live agent
loops.

The types live in `canonical/` (not `adapters/`) because callers (the
evaluator, the benchmark harness) need to persist `BatchHandle`s across
process restarts and refer to them without importing adapter
implementation modules. Keeping them in canonical also matches the
broader convention: provider-agnostic data shapes belong here.
"""

from __future__ import annotations

from typing import Literal

import msgspec

from metis.core.adapters.errors import ErrorClass

# Closed status enum per §4.6.2. `expired` is a terminal failure (24h
# elapsed without completion); `failed` is a batch-level abort before any
# results were produced.
BatchStatus = Literal["queued", "in_progress", "completed", "expired", "failed"]


class BatchHandle(msgspec.Struct, frozen=True):
    """Caller-side handle returned by `submit_batch`.

    `custom_ids` preserves the caller's mapping from `requests[i]` to
    result rows — `fetch_batch` returns a same-length, same-order list
    keyed against this. The provider-side `batch_id` is opaque; only the
    adapter for `provider` knows how to interpret it.

    Persistence is the caller's responsibility (§4.6.5). Two callers are
    anticipated in v1:
      - `metis evaluate --batch-mode` persists to a small table in the
        trace DB.
      - `scripts/benchmark.py --batch-mode` persists to a JSONL file
        under `benchmarks/.runs/<run_id>/`.
    """

    provider: str
    batch_id: str
    submitted_at_ms: int
    request_count: int
    custom_ids: tuple[str, ...]


class BatchError(msgspec.Struct, frozen=True):
    """Per-request failure inside a successfully-completed batch.

    Batch-level failures (entire batch expired or aborted before any
    results were produced) raise `AdapterError` instead — see §4.6.6.

    Expired batches surface one `BatchError` per `custom_id` with
    `error_class=ErrorClass.SERVER_ERROR` and `retryable=True`. The spec
    (§4.6.6) names this class `PROVIDER_TRANSIENT`, but the closed
    `ErrorClass` enum (§6.1) does not carry that value; `SERVER_ERROR` is
    the closest match (transient upstream issue) and matches the
    `retryable=True` convention used by `ServerError`.
    """

    custom_id: str
    error_class: ErrorClass
    error_message: str
    retryable: bool


# Per-request result row returned by `fetch_batch`. Successful entries
# carry a `CanonicalResponse`; failed entries carry a `BatchError`. The
# list returned by `fetch_batch` is same-length, same-order as the
# `requests` list that produced the `BatchHandle`.
#
# Imported lazily to avoid a cycle: `adapters.protocol` already imports
# from `canonical`, so anything in `canonical` referencing
# `CanonicalResponse` would create a circular dependency at import time.
# Callers receive `CanonicalResponse | BatchError` from `fetch_batch`; we
# document the union as a `TypeAlias` in `canonical/__init__.py`.

__all__ = ["BatchError", "BatchHandle", "BatchStatus"]
