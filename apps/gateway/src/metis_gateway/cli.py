"""Entry point used by `metis gateway` (mounted in metis_cli.main)."""

from __future__ import annotations

import sys
from pathlib import Path

from metis_gateway.app import (
    DEFAULT_MAX_CONCURRENT_CONNECTIONS,
    GatewayConfig,
    GatewayConfigError,
    run_gateway,
)
from metis_gateway.billing import BillingConfig
from metis_gateway.runtime import (
    GatewaySetupError,
    default_keystore_path,
    setup_gateway_runtime,
    shutdown_gateway_runtime,
)
from metis_gateway.signup import SignupConfig


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
    signup_enabled: bool = False,
    signup_dashboard_url: str | None = None,
    signup_accounts_path: str | None = None,
    billing_enabled: bool = False,
    billing_stripe_api_key: str | None = None,
    billing_stripe_webhook_secret: str | None = None,
    billing_store_path: str | None = None,
    billing_pro_price_id: str | None = None,
    billing_enterprise_metered_price_id: str | None = None,
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
    signup_cfg: SignupConfig | None = None
    if signup_enabled:
        scheme = "https" if tls_cert else "http"
        default_dashboard = f"{scheme}://{host}:{port}"
        signup_cfg = SignupConfig(
            enabled=True,
            keystore_path=Path(keystore_path).expanduser() if keystore_path else None,
            accounts_path=(
                Path(signup_accounts_path).expanduser() if signup_accounts_path else None
            ),
            dashboard_base_url=signup_dashboard_url or default_dashboard,
        )
    billing_cfg: BillingConfig | None = None
    if billing_enabled:
        if not signup_enabled:
            print(
                "error: --enable-billing requires --enable-signup",
                file=sys.stderr,
            )
            return 1
        kwargs = {}
        if billing_pro_price_id:
            kwargs["pro_price_id"] = billing_pro_price_id
        if billing_enterprise_metered_price_id:
            kwargs["enterprise_metered_price_id"] = billing_enterprise_metered_price_id
        billing_cfg = BillingConfig(
            enabled=True,
            stripe_api_key=billing_stripe_api_key,
            stripe_webhook_secret=billing_stripe_webhook_secret,
            store_path=Path(billing_store_path).expanduser() if billing_store_path else None,
            **kwargs,
        )
    try:
        config = GatewayConfig(
            host=host,
            port=port,
            tls_cert=Path(tls_cert).expanduser() if tls_cert else None,
            tls_key=Path(tls_key).expanduser() if tls_key else None,
            max_concurrent_connections=max_connections,
            reuse_port=reuse_port,
            signup=signup_cfg,
            billing=billing_cfg,
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
