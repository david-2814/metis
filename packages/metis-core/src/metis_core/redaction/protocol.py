"""`Redactor` Protocol — the contract 12a-3 (redaction.md) ships.

A `Redactor` knows how to enact the "right to be forgotten" against the trace
store: pseudonymize the named subject's identifying fields across all rows
that carry them. The append-only invariant on the trace store (per
multi-user.md §7.4.4) rules out hard deletion; pseudonymization is the
GDPR / CCPA-compatible alternative.

The contract is intentionally narrow — one method, idempotent, returns a
row count — so that the wider redaction policy (rationale-redacted opt-in
fields, audit-export scrubbing rules, retention overrides) can grow in
redaction.md without churning this surface.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Redactor(Protocol):
    """Pseudonymize a user's identifying fields in the trace store.

    `forget_user` is invoked by the portability/forget surface in
    `analytics-api.md §4.10`. The implementation:

    - MUST be idempotent. A second call for the same `user_id` returns 0
      (or some other safe count) without raising — the pseudonym has
      already been substituted.
    - MUST be append-only-safe. No hard `DELETE`; only in-place
      `json_set` / `UPDATE` operations on the existing rows.
    - MUST be confined to fields the user is plausibly the subject of —
      `user_id` itself on `llm.call_completed` / `turn.completed`, plus
      whatever other rationale-redacted opt-in fields redaction.md
      enumerates.
    - Returns the count of rows touched (the "pseudonymized count" the
      `/analytics/user/{user_id}/forget` endpoint surfaces).

    Errors (DB locked, I/O, etc.) propagate; the caller is responsible
    for emitting `analytics.user_forgotten` only on success.
    """

    def forget_user(self, user_id: str) -> int: ...
