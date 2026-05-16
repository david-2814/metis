"""Load a JSON config file and merge it with built-in defaults."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "database": {
        "host": "localhost",
        "port": 5432,
        "timeout": 30,
        "pool_size": 10,
    },
    "cache": {
        "host": "localhost",
        "port": 6379,
        "ttl_seconds": 300,
    },
    "log_level": "INFO",
}


def load_config(path: str | Path) -> dict[str, Any]:
    """Load a JSON config file, merging it with DEFAULTS.

    User-supplied values override the defaults. Sections the user omits
    fall back to the default values.
    """
    text = Path(path).read_text()
    user = json.loads(text)
    return {**DEFAULTS, **user}
