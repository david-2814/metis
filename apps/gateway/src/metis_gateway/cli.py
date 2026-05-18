"""Entry point used by `metis gateway` (mounted in metis_cli.main).

Wave 17 (repo-split-plan.md §4.2b + §4.3, 2026-05-18): billing and
signup wiring both moved to the closed-source `metis-pro` overlay. OSS
launches the gateway with the noop ``SignupBackend`` and
``BillingBackend`` defaults; the Pro overlay's CLI entrypoint wraps
this to add `--enable-signup` / `--enable-billing` and inject the real
``MagicLinkSignupBackend`` / ``StripeBillingBackend`` implementations.
"""

from __future__ import annotations

import sys
from pathlib import Path

from metis_gateway.app import (
    DEFAULT_MAX_CONCURRENT_CONNECTIONS,
    GatewayConfig,
    GatewayConfigError,
    run_gateway,
)
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
    tls_cert: str | None = None,
    tls_key: str | None = None,
    max_connections: int = DEFAULT_MAX_CONCURRENT_CONNECTIONS,
    reuse_port: bool = False,
) -> int:
    """Run `metis gateway` until shutdown.

    Returns a Unix-style exit code so `metis_cli.main` can propagate it.

    Wave 13 (gateway-hardening.md §2.1) — `host`, `tls_cert`, `tls_key`,
    `max_connections`, and `reuse_port` are forwarded to `GatewayConfig`.
    `host` default of `127.0.0.1` is preserved; passing `0.0.0.0`
    exposes the gateway and logs a hardening-checklist warning.
    """
    resolved_keystore = (
        Path(keystore_path).expanduser() if keystore_path else default_keystore_path()
    )
    resolved_db = Path(db_path).expanduser() if db_path else None
    try:
        config = GatewayConfig(
            host=host,
            port=port,
            tls_cert=Path(tls_cert).expanduser() if tls_cert else None,
            tls_key=Path(tls_key).expanduser() if tls_key else None,
            max_concurrent_connections=max_connections,
            reuse_port=reuse_port,
        )
    except GatewayConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        runtime = await setup_gateway_runtime(
            keystore_path=resolved_keystore,
            db_path=resolved_db,
            global_default_model=global_default_model,
        )
    except GatewaySetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    scheme = "https" if config.tls_enabled else "http"
    print(
        f"metis gateway listening on {scheme}://{host}:{port} "
        f"(keystore={resolved_keystore}, db={runtime.db_file})",
        file=sys.stderr,
    )
    try:
        await run_gateway(runtime, config)
    finally:
        await shutdown_gateway_runtime(runtime)
    return 0
