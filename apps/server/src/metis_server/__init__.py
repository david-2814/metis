"""HTTP/WebSocket surface for the Metis server.

The HTTP surface (per `server-api.md`) covers session lifecycle, turn
submission, message history, and tool confirmation responses. The WebSocket
surface (per `streaming-protocol.md`) handles snapshot+live event streaming
plus cancellation.

Both surfaces share the same in-process runtime (event bus, session store,
session manager, dispatcher). Wiring lives in `app.build_app()`.
"""

from metis_server.app import ServerConfig, build_app, run_server
from metis_server.tokens import AttachTokenRegistry

__all__ = ["AttachTokenRegistry", "ServerConfig", "build_app", "run_server"]
