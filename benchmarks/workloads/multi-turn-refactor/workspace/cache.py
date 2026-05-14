"""In-memory LRU cache shared by the dashboard handlers.

Public surface:
    - `get_cached(key)` — returns the value or None.
    - `put_cached(key, value)` — store; evicts oldest if at capacity.
    - `cache_size()` — current entry count.

The naming has bugged us for a while: `get_cached` reads like "get [the thing
that was] cached," which is fine in isolation but reads awkwardly at every
call site. We're renaming `get_cached` -> `lookup` repo-wide.
"""

from __future__ import annotations

from collections import OrderedDict

_MAX = 128
_store: OrderedDict[str, object] = OrderedDict()


def get_cached(key: str) -> object | None:
    """Return the cached value for `key`, or None. Move-to-end on hit."""
    if key not in _store:
        return None
    _store.move_to_end(key)
    return _store[key]


def put_cached(key: str, value: object) -> None:
    if key in _store:
        _store.move_to_end(key)
    _store[key] = value
    if len(_store) > _MAX:
        _store.popitem(last=False)


def cache_size() -> int:
    return len(_store)
