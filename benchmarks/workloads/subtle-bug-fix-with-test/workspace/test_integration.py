"""Failing test that surfaces the symptom — KeyError on missing port."""

from __future__ import annotations

import config_loader
import db_connector


def test_db_connector_reads_production_config() -> None:
    """The user_config.json sets database.host = production.db. The DB
    connector should still build successfully because the loader is
    supposed to merge user input with the defaults.
    """
    config = config_loader.load_config("user_config.json")
    db = db_connector.connect(config)
    assert db.host == "production.db"
    assert db.port == 5432  # from defaults
    assert db.timeout == 30  # from defaults
    assert db.pool_size == 10  # from defaults
