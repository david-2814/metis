"""Canonical id generation.

ULIDs are monotonic per process and sortable. Tool-use ids carry a `tu_` prefix
per canonical-message-format.md §6.2.
"""

from ulid import ULID


def new_session_id() -> str:
    return f"sess_{ULID()}"


def new_message_id() -> str:
    return str(ULID())


def new_tool_use_id() -> str:
    return f"tu_{ULID()}"
