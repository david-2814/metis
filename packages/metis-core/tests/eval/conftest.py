"""Pytest fixtures for evaluator tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from metis_core.trace.store import TraceStore


@pytest.fixture
def trace_db(tmp_path: Path) -> Iterator[TraceStore]:
    db_path = tmp_path / "trace.db"
    store = TraceStore(db_path)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def trace_db_path(tmp_path: Path) -> Path:
    return tmp_path / "trace.db"
