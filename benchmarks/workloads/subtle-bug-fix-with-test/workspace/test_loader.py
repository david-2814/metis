"""Root-cause test: the loader itself must produce a fully-merged config.

This test only passes if the fix lives in config_loader.load_config —
specifically, if it deep-merges nested dicts. Patching db_connector to
fall back to defaults at the use-site does not make this test pass.
"""

from __future__ import annotations

import config_loader


def test_partial_database_section_merges_with_defaults() -> None:
    """Loading a config that overrides only database.host must yield
    a fully-populated database section (host overridden, the other
    keys preserved from DEFAULTS)."""
    config = config_loader.load_config("user_config.json")
    assert config["database"] == {
        "host": "production.db",
        "port": 5432,
        "timeout": 30,
        "pool_size": 10,
    }


def test_cache_section_untouched_by_user_keeps_defaults() -> None:
    """The user_config.json doesn't mention cache at all. The merged
    config must still expose the full cache defaults so cache_connector
    can build cleanly."""
    config = config_loader.load_config("user_config.json")
    assert config["cache"] == {
        "host": "localhost",
        "port": 6379,
        "ttl_seconds": 300,
    }


def test_log_level_override_wins() -> None:
    """Scalar overrides at the top level should still win — make sure
    a deep-merge fix doesn't break the simple-override case."""
    config = config_loader.load_config("user_config.json")
    assert config["log_level"] == "WARNING"
