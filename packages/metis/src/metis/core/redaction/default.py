"""Minimum-viable `Redactor` implementation; 12a-3 (redaction.md) replaces it.

Pseudonymizes a user's `user_id` field in the trace store's `events` table
so the forget endpoint can produce a non-zero rows-touched count and a
subsequent export returns no events. Scope is deliberately narrow — only
the `user_id` field on payloads that carry it. The canonical policy (which
free-text fields to scrub, which rationale-redacted opt-in fields exist,
audit-export scrubbing rules) lives in redaction.md and replaces this
implementation when it lands.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path


def pseudonym_for(user_id: str) -> str:
    """Deterministic pseudonym: stable across calls, irreversible.

    SHA-256 truncated to 12 hex chars is enough to avoid collisions in any
    plausible single-deployment user population (~4.7B for 50/50 collision)
    while staying short enough to read in a trace dump.
    """
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return f"redacted_{digest[:12]}"


class PseudonymizingRedactor:
    """Default `Redactor` — SQL `json_set` over the trace DB's `events` table.

    Targets the two payload fields multi-user.md §4.4 stamps `user_id`
    onto (`llm.call_completed`, `turn.completed`) and the audit events
    that carry it (`gateway.key_issued`, `gateway.key_rotated`,
    `gateway.quota_exceeded`, `quota.alert`). The query rewrites the
    `user_id` JSON field in place with the deterministic pseudonym
    returned by `pseudonym_for()`.

    Idempotent: a second call with the same `user_id` finds zero rows
    (the original id is no longer present) and returns 0.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    def forget_user(self, user_id: str) -> int:
        """Pseudonymize all event rows that stamp `user_id` and return the count."""
        pseudonym = pseudonym_for(user_id)
        # Opens a fresh short-lived connection so we don't fight with a
        # long-lived AnalyticsStore reader. WAL is set by the writer; readers
        # see committed writes immediately.
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            cursor = conn.execute(
                "UPDATE events "
                "SET payload_json = json_set(payload_json, '$.user_id', ?) "
                "WHERE json_extract(payload_json, '$.user_id') = ?",
                (pseudonym, user_id),
            )
            return int(cursor.rowcount or 0)
        finally:
            conn.close()
