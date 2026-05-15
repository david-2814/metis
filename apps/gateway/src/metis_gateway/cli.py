"""Entry point used by `metis gateway` (mounted in metis_cli.main)."""

from __future__ import annotations

import sys
from pathlib import Path

from metis_gateway.app import GatewayConfig, run_gateway
from metis_gateway.runtime import (
    GatewaySetupError,
    default_keystore_path,
    setup_gateway_runtime,
    shutdown_gateway_runtime,
)


async def run_gateway_command(
    *,
    keystore_path: str | None,
    db_path: str | None,
    global_default_model: str,
    host: str,
    port: int,
) -> int:
    """Run `metis gateway` until shutdown.

    Returns a Unix-style exit code so `metis_cli.main` can propagate it.
    """
    resolved_keystore = (
        Path(keystore_path).expanduser() if keystore_path else default_keystore_path()
    )
    resolved_db = Path(db_path).expanduser() if db_path else None
    try:
        runtime = await setup_gateway_runtime(
            keystore_path=resolved_keystore,
            db_path=resolved_db,
            global_default_model=global_default_model,
        )
    except GatewaySetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"metis gateway listening on http://{host}:{port} "
        f"(keystore={resolved_keystore}, db={runtime.db_file})",
        file=sys.stderr,
    )
    try:
        await run_gateway(runtime, GatewayConfig(host=host, port=port))
    finally:
        await shutdown_gateway_runtime(runtime)
    return 0
