"""Tasks module — uses functools.partial to bind fetch arguments."""

from __future__ import annotations

from functools import partial

from api import fetch

# Positional binding: equivalent to ``fetch("/admins", ...)``.
get_admin = partial(fetch, "/admins")

# Positional + keyword binding.
poll_health = partial(fetch, "/health", retries=1)
