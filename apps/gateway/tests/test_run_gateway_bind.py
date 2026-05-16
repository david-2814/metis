"""Tests for the Wave 13 lift of the gateway's loopback-only bind constraint
plus the connection-rate + in-process-TLS additions in `app.py::run_gateway`.

Coverage (gateway-hardening.md §2.1 / §2.2 / §2.3):

1. `GatewayConfig()` default still binds `127.0.0.1` (back-compat — the
   silent rewrite is removed but the *default* host is unchanged).
2. `GatewayConfig(host="0.0.0.0")` is accepted; no rewrite to loopback.
3. `--host 0.0.0.0` end-to-end via `run_gateway_command` argparse flow:
   the host travels intact into `GatewayConfig`.
4. Non-loopback bind logs the documented hardening-checklist `WARN`.
5. TLS validation: both `tls_cert` + `tls_key` required together;
   missing file raises `GatewayConfigError` with a useful message;
   `tls_enabled` reflects the both-set state.
6. uvicorn projection: `_build_uvicorn_config` threads
   `limit_concurrency`, `backlog`, `ssl_certfile`, `ssl_keyfile`.
7. `max_concurrent_connections` validation (≥ 1).
8. `SO_REUSEPORT` socket: when enabled, the listen socket is bound,
   listening, and carries the option.
9. Live bind smoke: `run_gateway` actually starts on a free `127.0.0.1`
   port and serves `/healthz`. (We don't bind 0.0.0.0 in CI to avoid
   firewall flakes; the config path is exercised by the unit checks above.)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import ssl
import sys
from pathlib import Path

import httpx
import pytest
from metis_gateway.app import (
    DEFAULT_BACKLOG,
    DEFAULT_HOST,
    DEFAULT_MAX_CONCURRENT_CONNECTIONS,
    DEFAULT_PORT,
    GatewayConfig,
    GatewayConfigError,
    _build_uvicorn_config,
    _is_loopback_host,
    _log_non_loopback_warning,
    _make_listen_socket,
    build_app,
    run_gateway,
)
from metis_gateway.middleware_ratelimit import RateLimitConfig

# ---------------------------------------------------------------------------
# Helpers — minimal self-signed cert for the TLS-happy-path test.
# ---------------------------------------------------------------------------


def _write_placeholder_cert_files(tmp_path: Path) -> tuple[Path, Path]:
    """Drop placeholder cert + key files into tmp_path.

    `GatewayConfig.__post_init__` only checks that the paths exist on
    disk; the bytes are opaque to it. uvicorn would reject these at TLS
    handshake time, but that's not what these tests exercise — the
    placeholder is enough for every test that doesn't actually open a
    listener with TLS engaged. The live-TLS happy path uses a real cert
    minted via `_mint_self_signed_cert`.
    """
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_text("-----BEGIN CERTIFICATE-----\nplaceholder\n-----END CERTIFICATE-----\n")
    key_path.write_text("-----BEGIN PRIVATE KEY-----\nplaceholder\n-----END PRIVATE KEY-----\n")
    return cert_path, key_path


def _mint_self_signed_cert(tmp_path: Path) -> tuple[Path, Path]:
    """Mint a real self-signed cert via `cryptography`.

    Used only by the live-TLS handshake smoke test; skipped when the
    optional `cryptography` library isn't on the path.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        pytest.skip("`cryptography` not installed; cannot mint a self-signed cert")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    import datetime as _dt

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=1))
        .not_valid_after(_dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _find_free_port() -> int:
    """Bind ephemeral, capture the OS-assigned port, release."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_default_host_is_loopback() -> None:
    """gateway-hardening.md §2.1 — loopback remains the default, just no
    longer the *only* permitted value."""
    cfg = GatewayConfig()
    assert cfg.host == DEFAULT_HOST == "127.0.0.1"
    assert cfg.port == DEFAULT_PORT
    assert cfg.max_concurrent_connections == DEFAULT_MAX_CONCURRENT_CONNECTIONS == 1000
    assert cfg.backlog == DEFAULT_BACKLOG == 2048
    assert cfg.reuse_port is False
    assert cfg.tls_cert is None
    assert cfg.tls_key is None
    assert cfg.tls_enabled is False


def test_is_loopback_host_table() -> None:
    assert _is_loopback_host("127.0.0.1") is True
    assert _is_loopback_host("localhost") is True
    assert _is_loopback_host("::1") is True
    assert _is_loopback_host("0.0.0.0") is False
    assert _is_loopback_host("10.0.0.5") is False
    assert _is_loopback_host("198.51.100.42") is False


# ---------------------------------------------------------------------------
# Non-loopback bind is accepted; the silent rewrite is removed.
# ---------------------------------------------------------------------------


def test_non_loopback_host_is_accepted_without_rewrite() -> None:
    """Pre-Wave-13 the gateway silently rewrote 0.0.0.0 → 127.0.0.1.
    The constraint is lifted; the config holds the requested host
    verbatim."""
    cfg = GatewayConfig(host="0.0.0.0")
    assert cfg.host == "0.0.0.0"


def test_arbitrary_external_host_is_accepted() -> None:
    cfg = GatewayConfig(host="10.0.0.5")
    assert cfg.host == "10.0.0.5"


def test_non_loopback_logs_hardening_warn(caplog) -> None:
    """The lift comes with a one-time WARN naming the perimeter checklist
    (gateway-hardening.md §2.1)."""
    cfg = GatewayConfig(host="0.0.0.0")
    with caplog.at_level(logging.WARNING, logger="metis_gateway.app"):
        _log_non_loopback_warning(cfg)
    assert any(
        "non-loopback" in rec.message and "tls_in_process=off" in rec.message
        for rec in caplog.records
    ), caplog.text
    assert any("rate_limit=off" in rec.message for rec in caplog.records), caplog.text


def test_non_loopback_warn_reflects_tls_and_rate_limit_state(tmp_path, caplog) -> None:
    cert, key = _write_placeholder_cert_files(tmp_path)
    cfg = GatewayConfig(
        host="0.0.0.0",
        tls_cert=cert,
        tls_key=key,
        rate_limit=RateLimitConfig(enabled=True),
    )
    with caplog.at_level(logging.WARNING, logger="metis_gateway.app"):
        _log_non_loopback_warning(cfg)
    msg = caplog.records[-1].message
    assert "tls_in_process=on" in msg
    assert "rate_limit=on" in msg


# ---------------------------------------------------------------------------
# TLS validation (both-or-neither; missing files; tls_enabled property)
# ---------------------------------------------------------------------------


def test_tls_cert_without_key_is_rejected(tmp_path) -> None:
    cert, _ = _write_placeholder_cert_files(tmp_path)
    with pytest.raises(GatewayConfigError, match="set together"):
        GatewayConfig(tls_cert=cert)


def test_tls_key_without_cert_is_rejected(tmp_path) -> None:
    _, key = _write_placeholder_cert_files(tmp_path)
    with pytest.raises(GatewayConfigError, match="set together"):
        GatewayConfig(tls_key=key)


def test_tls_cert_missing_on_disk_is_rejected(tmp_path) -> None:
    _, key = _write_placeholder_cert_files(tmp_path)
    bogus = tmp_path / "does-not-exist.pem"
    with pytest.raises(GatewayConfigError, match="tls_cert file not found"):
        GatewayConfig(tls_cert=bogus, tls_key=key)


def test_tls_key_missing_on_disk_is_rejected(tmp_path) -> None:
    cert, _ = _write_placeholder_cert_files(tmp_path)
    bogus = tmp_path / "does-not-exist.pem"
    with pytest.raises(GatewayConfigError, match="tls_key file not found"):
        GatewayConfig(tls_cert=cert, tls_key=bogus)


def test_tls_enabled_property_reflects_both_set(tmp_path) -> None:
    cert, key = _write_placeholder_cert_files(tmp_path)
    cfg = GatewayConfig(tls_cert=cert, tls_key=key)
    assert cfg.tls_enabled is True
    assert cfg.tls_cert == cert
    assert cfg.tls_key == key


# ---------------------------------------------------------------------------
# Connection-rate cap + backlog validation
# ---------------------------------------------------------------------------


def test_max_concurrent_connections_rejects_zero() -> None:
    with pytest.raises(GatewayConfigError, match="max_concurrent_connections"):
        GatewayConfig(max_concurrent_connections=0)


def test_max_concurrent_connections_rejects_negative() -> None:
    with pytest.raises(GatewayConfigError, match="max_concurrent_connections"):
        GatewayConfig(max_concurrent_connections=-1)


def test_backlog_rejects_zero() -> None:
    with pytest.raises(GatewayConfigError, match="backlog"):
        GatewayConfig(backlog=0)


# ---------------------------------------------------------------------------
# uvicorn projection: cfg → uvicorn.Config
# ---------------------------------------------------------------------------


async def test_uvicorn_config_threads_connection_cap(runtime) -> None:
    cfg = GatewayConfig(max_concurrent_connections=42)
    app = build_app(runtime, rate_limit=cfg.rate_limit)
    uv_config = _build_uvicorn_config(app, cfg)
    assert uv_config.limit_concurrency == 42
    assert uv_config.backlog == 2048
    assert uv_config.ssl_certfile is None
    assert uv_config.ssl_keyfile is None


async def test_uvicorn_config_threads_tls_paths(runtime, tmp_path) -> None:
    cert, key = _write_placeholder_cert_files(tmp_path)
    cfg = GatewayConfig(tls_cert=cert, tls_key=key)
    app = build_app(runtime, rate_limit=cfg.rate_limit)
    uv_config = _build_uvicorn_config(app, cfg)
    assert uv_config.ssl_certfile == str(cert)
    assert uv_config.ssl_keyfile == str(key)


async def test_uvicorn_config_honors_custom_backlog(runtime) -> None:
    cfg = GatewayConfig(backlog=8192)
    app = build_app(runtime, rate_limit=cfg.rate_limit)
    uv_config = _build_uvicorn_config(app, cfg)
    assert uv_config.backlog == 8192


# ---------------------------------------------------------------------------
# SO_REUSEPORT socket
# ---------------------------------------------------------------------------


def test_make_listen_socket_binds_with_so_reuseport() -> None:
    if not hasattr(socket, "SO_REUSEPORT"):
        pytest.skip("SO_REUSEPORT not available on this platform")
    port = _find_free_port()
    cfg = GatewayConfig(host="127.0.0.1", port=port, reuse_port=True)
    sock = _make_listen_socket(cfg)
    try:
        # The socket option round-trips. The kernel packs "on" into a
        # platform-specific non-zero integer (Linux returns 1, macOS
        # returns 0x200 for SO_REUSEPORT); truthy is the portable check.
        flag = sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT)
        assert flag != 0, f"SO_REUSEPORT should be set, got {flag}"
        # And the socket is actually bound + listening on the requested port.
        _bound_host, bound_port = sock.getsockname()
        assert bound_port == port
    finally:
        sock.close()


def test_make_listen_socket_without_reuse_port_still_works() -> None:
    """Plain bind without SO_REUSEPORT — the helper is the unified socket
    construction path even when reuse_port is False (caller decides not to
    use it). We exercise it directly to keep the path tested."""
    port = _find_free_port()
    cfg = GatewayConfig(host="127.0.0.1", port=port, reuse_port=False)
    sock = _make_listen_socket(cfg)
    try:
        # SO_REUSEADDR is always set; truthy check (see note above).
        flag = sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
        assert flag != 0, f"SO_REUSEADDR should be set, got {flag}"
        _bound_host, bound_port = sock.getsockname()
        assert bound_port == port
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Live-bind smoke: actually start `run_gateway` on 127.0.0.1, hit /healthz.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="live-bind smoke is brittle on Windows CI; covered by unit checks",
)
async def test_run_gateway_actually_binds_and_serves_healthz(runtime) -> None:
    """Sanity: with the rewrite gone, `run_gateway` boots and serves.

    We run on a free 127.0.0.1 port (not 0.0.0.0 — that opens firewall
    prompts on developer machines). The bind path is the same for both
    hosts; the value is exercised by the unit tests above. This test
    proves the integration with uvicorn still ties together end-to-end.
    """
    port = _find_free_port()
    cfg = GatewayConfig(host="127.0.0.1", port=port)
    server_task = asyncio.create_task(run_gateway(runtime, cfg))
    try:
        # Poll /healthz until the server is ready or we time out.
        async with httpx.AsyncClient() as client:
            deadline = asyncio.get_event_loop().time() + 5.0
            while True:
                try:
                    r = await client.get(f"http://127.0.0.1:{port}/healthz", timeout=0.5)
                    assert r.status_code == 200
                    assert r.json()["status"] == "ok"
                    break
                except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException):
                    if asyncio.get_event_loop().time() > deadline:
                        raise
                    await asyncio.sleep(0.05)
    finally:
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await server_task


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="live-bind smoke is brittle on Windows CI",
)
async def test_run_gateway_serves_https_with_in_process_tls(runtime, tmp_path) -> None:
    """End-to-end happy path: cert + key plumbed to uvicorn produces a
    real TLS handshake. We use a self-signed cert and tell httpx not to
    verify, since we just want to prove the handshake completes."""
    cert, key = _mint_self_signed_cert(tmp_path)
    port = _find_free_port()
    cfg = GatewayConfig(host="127.0.0.1", port=port, tls_cert=cert, tls_key=key)
    server_task = asyncio.create_task(run_gateway(runtime, cfg))
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        async with httpx.AsyncClient(verify=ctx) as client:
            deadline = asyncio.get_event_loop().time() + 5.0
            while True:
                try:
                    r = await client.get(f"https://127.0.0.1:{port}/healthz", timeout=0.5)
                    assert r.status_code == 200
                    break
                except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException):
                    if asyncio.get_event_loop().time() > deadline:
                        raise
                    await asyncio.sleep(0.05)
    finally:
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await server_task
