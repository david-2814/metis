"""Delegation v1 MVP: worker sessions and the `delegate()` tool's data shapes.

See `docs/specs/delegation.md`. v1 scope is synchronous, single-worker, no
recursion. Streaming, cancellation cascade, fan-out cap, and worker-spawns-
worker are deferred.
"""

from metis.core.workers.protocol import (
    ContextSpec,
    DelegateContextMode,
    DelegateFailureMode,
    DelegateOutcome,
    DelegateRequest,
    DelegateResult,
    DelegateTier,
    DelegateUsageSummary,
    WorkerSpawner,
    WorkerSpec,
)

__all__ = [
    "ContextSpec",
    "DelegateContextMode",
    "DelegateFailureMode",
    "DelegateOutcome",
    "DelegateRequest",
    "DelegateResult",
    "DelegateTier",
    "DelegateUsageSummary",
    "WorkerSpawner",
    "WorkerSpec",
]
