"""Tests for ToolIdMap."""

from __future__ import annotations

from metis.core.adapters.tool_id_map import ToolIdMap


def test_remember_bidirectional():
    m = ToolIdMap()
    m.remember("tu_canonical", "toolu_provider")
    assert m.to_provider("tu_canonical") == "toolu_provider"
    assert m.to_canonical("toolu_provider") == "tu_canonical"


def test_has_canonical():
    m = ToolIdMap()
    assert m.has_canonical("tu_x") is False
    m.remember("tu_x", "p_x")
    assert m.has_canonical("tu_x") is True


def test_to_provider_returns_none_for_unknown():
    m = ToolIdMap()
    assert m.to_provider("tu_unknown") is None


def test_to_canonical_returns_none_for_unknown():
    m = ToolIdMap()
    assert m.to_canonical("p_unknown") is None


def test_remember_overwrites_existing():
    m = ToolIdMap()
    m.remember("tu_a", "p_a")
    m.remember("tu_a", "p_a_new")
    assert m.to_provider("tu_a") == "p_a_new"


def test_len_counts_unique_entries():
    m = ToolIdMap()
    m.remember("tu_a", "p_a")
    m.remember("tu_b", "p_b")
    assert len(m) == 2
