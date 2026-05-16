"""Cache connector that consumes the loaded config."""

from __future__ import annotations

from typing import Any


class Cache:
    def __init__(self, host: str, port: int, ttl_seconds: int) -> None:
        self.host = host
        self.port = port
        self.ttl_seconds = ttl_seconds

    def __repr__(self) -> str:
        return f"Cache(host={self.host!r}, port={self.port}, ttl_seconds={self.ttl_seconds})"


def connect(config: dict[str, Any]) -> Cache:
    """Build a Cache connection from the merged config.

    Contract: ``config["cache"]`` is fully populated.
    """
    cache_config = config["cache"]
    return Cache(
        host=cache_config["host"],
        port=cache_config["port"],
        ttl_seconds=cache_config["ttl_seconds"],
    )
