"""`metis serve` — run the HTTP/WebSocket server bound to a workspace."""

from __future__ import annotations

import sys

from metis_server.app import ServerConfig, run_server

from metis_cli.runtime import SetupError, setup_runtime, shutdown_runtime


async def run_serve(
    *,
    workspace_path: str,
    db_path: str | None,
    global_default_model: str,
    host: str,
    port: int,
) -> int:
    try:
        runtime = await setup_runtime(
            workspace_path=workspace_path,
            db_path=db_path,
            global_default_model=global_default_model,
        )
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"metis serve listening on http://{host}:{port} "
        f"(workspace={workspace_path}, db={runtime.db_file})",
        file=sys.stderr,
    )
    try:
        await run_server(runtime, ServerConfig(host=host, port=port))
    finally:
        await shutdown_runtime(runtime)
    return 0
