"""CLI driver — calls fetch with mixed positional / **kwargs unpacking."""

from __future__ import annotations

from api import fetch


def run_command(endpoint, options):
    """The endpoint comes from one place; the rest from a dict."""
    return fetch(endpoint, **options)
