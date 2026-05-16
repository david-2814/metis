"""DB connector that consumes the loaded config."""

from __future__ import annotations

from typing import Any


class DB:
    def __init__(self, host: str, port: int, timeout: int, pool_size: int) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.pool_size = pool_size

    def __repr__(self) -> str:
        return f"DB(host={self.host!r}, port={self.port}, timeout={self.timeout}, pool_size={self.pool_size})"


def connect(config: dict[str, Any]) -> DB:
    """Build a DB connection from the merged config.

    The contract: ``config["database"]`` is a fully-populated dict
    containing host, port, timeout, and pool_size. The config loader
    guarantees this by merging user input with the defaults.
    """
    db_config = config["database"]
    return DB(
        host=db_config["host"],
        port=db_config["port"],
        timeout=db_config["timeout"],
        pool_size=db_config["pool_size"],
    )
