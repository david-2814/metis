"""Tests for cache.get_cached. After the rename these will need updating too."""

import cache


def test_miss_returns_none():
    assert cache.get_cached("nope") is None


def test_put_then_get_roundtrip():
    cache.put_cached("k", "v")
    assert cache.get_cached("k") == "v"
