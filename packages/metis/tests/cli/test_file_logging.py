"""Smoke tests for the runtime's file-logging configuration.

`/tmp/metis.log` (or wherever `METIS_LOG_FILE` points) catches adapter
warnings — upstream errors, request bodies, etc. — so users have a place
to grep when something fails.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from metis.cli.runtime import _configure_file_logging


@pytest.fixture(autouse=True)
def _reset_logging_state(monkeypatch):
    """Reset the module-level idempotence flag and detach any handlers we
    attach during a test so tests don't leak handler state."""
    import metis.cli.runtime as runtime

    monkeypatch.setattr(runtime, "_FILE_LOGGING_CONFIGURED", False)
    metis_logger = logging.getLogger("metis.core")
    original_handlers = list(metis_logger.handlers)
    original_level = metis_logger.level
    yield
    # Remove anything we added during the test, restore original state.
    for h in list(metis_logger.handlers):
        if h not in original_handlers:
            metis_logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
    metis_logger.setLevel(original_level)


def test_configure_file_logging_uses_env_var_path(tmp_path, monkeypatch):
    log_path = tmp_path / "metis-test.log"
    monkeypatch.setenv("METIS_LOG_FILE", str(log_path))

    _configure_file_logging()

    # Emit a record through the metis logger and verify it lands in the file.
    metis_logger = logging.getLogger("metis.core")
    metis_logger.warning("hello from the test")

    # Force handler flushes.
    for h in metis_logger.handlers:
        h.flush()

    assert log_path.exists(), "log file should be created after configure"
    contents = log_path.read_text()
    assert "hello from the test" in contents
    assert "WARNING" in contents


def test_configure_file_logging_empty_env_disables(tmp_path, monkeypatch):
    """Empty METIS_LOG_FILE disables file logging — no handler attached."""
    monkeypatch.setenv("METIS_LOG_FILE", "")

    metis_logger = logging.getLogger("metis.core")
    handlers_before = list(metis_logger.handlers)
    _configure_file_logging()
    handlers_after = list(metis_logger.handlers)

    # No new FileHandler should have been added.
    new_handlers = [h for h in handlers_after if h not in handlers_before]
    assert not any(isinstance(h, logging.FileHandler) for h in new_handlers)


def test_configure_file_logging_idempotent(tmp_path, monkeypatch):
    """Calling configure twice doesn't pile on handlers."""
    log_path = tmp_path / "metis-test.log"
    monkeypatch.setenv("METIS_LOG_FILE", str(log_path))

    _configure_file_logging()
    handlers_after_first = list(logging.getLogger("metis.core").handlers)
    _configure_file_logging()
    handlers_after_second = list(logging.getLogger("metis.core").handlers)

    assert len(handlers_after_first) == len(handlers_after_second)


def test_configure_file_logging_creates_parent_dirs(tmp_path, monkeypatch):
    """Parent directories are created if missing — user shouldn't have to
    pre-create the path."""
    log_path = tmp_path / "deep" / "nested" / "metis.log"
    monkeypatch.setenv("METIS_LOG_FILE", str(log_path))

    _configure_file_logging()
    logging.getLogger("metis.core").warning("creates parents")
    for h in logging.getLogger("metis.core").handlers:
        h.flush()

    assert log_path.exists()


def test_configure_file_logging_bad_path_warns_not_raises(tmp_path, monkeypatch, capsys):
    """An unwritable path shouldn't crash startup — print a warning and
    continue with no file handler."""
    # Try to write into a path under a file (not a directory) — that's a
    # cross-platform way to force an OSError on mkdir.
    not_a_dir = tmp_path / "actually_a_file"
    not_a_dir.write_text("blocker")
    bad_path = not_a_dir / "metis.log"
    monkeypatch.setenv("METIS_LOG_FILE", str(bad_path))

    # Should not raise.
    _configure_file_logging()

    captured = capsys.readouterr()
    assert "could not open log file" in captured.err


def test_adapter_warning_lands_in_log_file(tmp_path, monkeypatch):
    """End-to-end: a warning from the OpenRouter adapter logger reaches the
    configured file. Exercises the actual log path we care about."""
    log_path = tmp_path / "metis-test.log"
    monkeypatch.setenv("METIS_LOG_FILE", str(log_path))
    _configure_file_logging()

    adapter_logger = logging.getLogger("metis.core.adapters.openrouter")
    adapter_logger.warning("upstream rejected: %s", "test reason")

    for h in logging.getLogger("metis.core").handlers:
        h.flush()

    contents = Path(log_path).read_text()
    assert "upstream rejected: test reason" in contents
    assert "metis.core.adapters.openrouter" in contents
