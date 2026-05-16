"""Forward-compat tests for `PatternStore` schema_version handling.

The Wave 10 v2 fingerprint feature bumped `store_meta.schema_version` from
"1" to "2" and added (a) a new `embedding_cache` table and (b) three new
columns on `fingerprints` (`embedding_blob`, `embedding_provider`,
`embedding_dim`). The bump path uses `ON CONFLICT DO UPDATE WHERE value <
excluded.value`, which is monotonic — a v1 process opening a v2 db never
downgrades the recorded version.

These tests exercise:

1. **Fresh v2 db:** schema_version stamped, embedding_cache present, new
   fingerprint columns present.
2. **Pre-Wave-10 (v1) db opened by current code:** schema_version bumps to
   "2" in-place, the `embedding_cache` table is created, and historical
   `fingerprints` / `outcomes` rows are preserved verbatim.
3. **Documented gap:** the new fingerprint columns are NOT backfilled by
   `CREATE TABLE IF NOT EXISTS` (SQLite's no-op for existing tables). A
   follow-up `record()` call against a pre-Wave-10 db raises
   `OperationalError: no such column: embedding_provider`. The operator
   workaround lives in `docs/operations/upgrade-guide.md` (§Pattern store
   v1→v2): delete `<workspace>/.metis/patterns.db` before upgrade and let
   slot 4 rebuild from trace events.

If the impl ever grows a real ALTER-TABLE migration, test 3 should be
deleted and replaced with the upgrade-success assertion.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from metis_core.patterns.fingerprint import (
    Fingerprint,
    FingerprintKind,
    StructuralFeatures,
)
from metis_core.patterns.store import _SCHEMA_VERSION, PatternStore

# Pre-Wave-10 (v1) schema. Notable: no `embedding_blob` / `embedding_provider`
# / `embedding_dim` columns, no `embedding_cache` table, schema_version="1".
_PRE_WAVE10_SCHEMA = """
CREATE TABLE fingerprints (
  id                 TEXT PRIMARY KEY,
  kind               TEXT NOT NULL,
  structural_json    TEXT NOT NULL,
  structural_sig     TEXT NOT NULL,
  created_at_us      INTEGER NOT NULL,
  UNIQUE (structural_sig)
);
CREATE INDEX idx_fp_created    ON fingerprints(created_at_us);
CREATE INDEX idx_fp_struct_sig ON fingerprints(structural_sig);

CREATE TABLE outcomes (
  fingerprint_id        TEXT NOT NULL,
  primary_model         TEXT NOT NULL,
  sample_size           INTEGER NOT NULL,
  success_score_mean    REAL NOT NULL,
  success_score_count   INTEGER NOT NULL,
  sum_cost_usd_micros   INTEGER NOT NULL,
  sum_latency_ms        REAL NOT NULL,
  pricing_version_last  TEXT NOT NULL,
  last_updated_at_us    INTEGER NOT NULL,
  PRIMARY KEY (fingerprint_id, primary_model)
);

CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO store_meta(key, value) VALUES ('schema_version', '1');
"""


def _seed_pre_wave10_workspace(workspace: Path) -> Path:
    metis_dir = workspace / ".metis"
    metis_dir.mkdir(parents=True, exist_ok=True)
    db_path = metis_dir / "patterns.db"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.executescript(_PRE_WAVE10_SCHEMA)
        conn.execute(
            "INSERT INTO fingerprints(id, kind, structural_json, structural_sig, created_at_us) "
            "VALUES (?, ?, ?, ?, ?)",
            ("fp_v1_legacy", "structural", "{}", "sig_legacy_001", 1_700_000_000_000_000),
        )
        conn.execute(
            "INSERT INTO outcomes("
            "fingerprint_id, primary_model, sample_size, success_score_mean, "
            "success_score_count, sum_cost_usd_micros, sum_latency_ms, "
            "pricing_version_last, last_updated_at_us"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("fp_v1_legacy", "haiku", 3, 0.85, 3, 1500, 250.0, "v1", 1_700_000_000_000_000),
        )
    finally:
        conn.close()
    return db_path


def test_fresh_db_has_v2_schema(tmp_path: Path) -> None:
    store = PatternStore(tmp_path)
    try:
        sv = store._conn.execute(
            "SELECT value FROM store_meta WHERE key = 'schema_version'"
        ).fetchone()
        assert sv[0] == _SCHEMA_VERSION
        cols = {r[1] for r in store._conn.execute("PRAGMA table_info(fingerprints)").fetchall()}
        assert {"embedding_blob", "embedding_provider", "embedding_dim"}.issubset(cols)
        tables = {
            r[0]
            for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "embedding_cache" in tables
    finally:
        store.close()


def test_legacy_v1_db_bumps_schema_and_adds_cache_table(tmp_path: Path) -> None:
    _seed_pre_wave10_workspace(tmp_path)
    store = PatternStore(tmp_path)
    try:
        sv = store._conn.execute(
            "SELECT value FROM store_meta WHERE key = 'schema_version'"
        ).fetchone()
        assert sv[0] == _SCHEMA_VERSION
        tables = {
            r[0]
            for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "embedding_cache" in tables
    finally:
        store.close()


def test_legacy_v1_db_preserves_historical_rows(tmp_path: Path) -> None:
    _seed_pre_wave10_workspace(tmp_path)
    store = PatternStore(tmp_path)
    try:
        fps = store._conn.execute("SELECT id FROM fingerprints").fetchall()
        outs = store._conn.execute("SELECT primary_model, sample_size FROM outcomes").fetchall()
        assert fps == [("fp_v1_legacy",)]
        assert outs == [("haiku", 3)]
    finally:
        store.close()


def test_legacy_v1_db_record_path_breaks_documented_gap(tmp_path: Path) -> None:
    """The documented v1→v2 limitation: `record()` fails on a legacy db.

    `CREATE TABLE IF NOT EXISTS fingerprints (...)` is a no-op when the
    table already exists, so the new `embedding_blob` / `embedding_provider`
    / `embedding_dim` columns are NOT added by an open. The first write
    that targets the new columns surfaces the gap.

    Operator workaround: delete `<workspace>/.metis/patterns.db` before
    upgrade. Slot 4 rebuilds the store as new turns arrive (pattern-store
    spec §2.2 non-goal 3: "If the pattern store is lost, it can be rebuilt
    by re-running the projection over the trace.").
    """
    _seed_pre_wave10_workspace(tmp_path)
    store = PatternStore(tmp_path)
    try:
        sf = StructuralFeatures(
            file_extensions=(".py",),
            file_path_buckets=("src",),
            tool_names=("read_file",),
            side_effect_classes=("read",),
            has_images=False,
            has_tool_calls_in_history=False,
            estimated_input_tokens_bucket=1,
            intent_tags=(),
            workspace_hash="wh_test",
            workload_id=None,
        )
        fp = Fingerprint(
            id="fp_post_upgrade",
            kind=FingerprintKind.STRUCTURAL,
            structural=sf,
            embedding=None,
            embedding_provider=None,
            embedding_dim=None,
            created_at=datetime.now(UTC),
        )
        with pytest.raises(sqlite3.OperationalError, match="no such column: embedding_provider"):
            store.record(fp, "haiku", 0.95, Decimal("0.001"), 100.0, "v1")
    finally:
        store.close()
