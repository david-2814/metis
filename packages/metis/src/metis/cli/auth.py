"""`metis auth` subcommand: add / list / remove / test / doctor.

Buyer-facing setup + diagnostics surface for the credentials module. All
subcommands route through `DefaultCredentialResolver` so they see the same
view of the resolution chain as the runtime.

Design notes:
- `add` uses stdlib `getpass.getpass` so the key is not echoed to the
  terminal, then writes to `~/.metis/credentials.yaml` with mode 0o600.
- `test` calls each configured provider's `validate_endpoint` via httpx
  (which is already a runtime dependency for the adapters).
- `doctor` peeks at the trace DB for last-successful-call and recent
  AUTH-error counts per provider — purely best-effort; an unreadable
  trace DB just elides those fields rather than failing the command.
- No subcommand prints the full key. The display helper from
  `credentials.protocol.truncate_key` is the single source of truth for
  what reaches stdout.
"""

from __future__ import annotations

import getpass
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from metis.core.credentials import (
    CURRENT_SCHEMA_VERSION,
    KNOWN_PROVIDERS,
    ConfiguredCredential,
    CredentialsFile,
    CredentialsFileInsecure,
    CredentialsFileSchemaUnknown,
    DefaultCredentialResolver,
    default_credentials_file_path,
    provider_names,
    truncate_key,
)
from metis.core.credentials.protocol import CredentialSource

# Timeout for validation pings. The endpoints are deliberately light
# (1-token Anthropic completion or two-byte GETs), so 10s is generous
# and prevents the command from hanging on an unresponsive provider.
_VALIDATE_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class ValidateResult:
    ok: bool
    latency_ms: int
    status_code: int | None
    message: str  # human-readable; never contains the key


# ---------------------------------------------------------------------------
# `metis auth add`
# ---------------------------------------------------------------------------


def run_auth_add(
    *,
    provider: str,
    validate: bool = True,
    file_path: Path | None = None,
    api_key: str | None = None,
    input_stream=None,
    output_stream=None,
) -> int:
    """Interactive add. Returns a Unix exit code.

    Arguments:
        provider: canonical provider name; must be in KNOWN_PROVIDERS.
        validate: if True, ping the validate_endpoint before persisting.
        file_path: override the credentials-file location (tests).
        api_key: override the interactive prompt (tests / CI).
        input_stream / output_stream: stdin / stdout substitutes (tests).
    """
    out = output_stream or sys.stdout
    err = sys.stderr

    if provider not in KNOWN_PROVIDERS:
        print(
            f"error: unknown provider {provider!r}. Known: {', '.join(provider_names())}.",
            file=err,
        )
        return 2

    if api_key is None:
        api_key = getpass.getpass(f"API key for {provider}: ", stream=err)
    api_key = (api_key or "").strip()
    if not api_key:
        print("error: API key is required.", file=err)
        return 1

    if validate:
        print(f"Validating {provider} key...", file=out)
        result = validate_provider(provider, api_key)
        if not result.ok:
            print(
                f"error: validation failed for {provider}: {result.message}",
                file=err,
            )
            print(
                "(pass --no-validate to skip the ping and persist anyway)",
                file=err,
            )
            return 1
        print(f"  -> ok ({result.latency_ms} ms)", file=out)

    path = (file_path or default_credentials_file_path()).expanduser()
    try:
        file = CredentialsFile.load(path)
    except FileNotFoundError:
        file = CredentialsFile.empty(path)
    except (
        CredentialsFileInsecure,
        CredentialsFileSchemaUnknown,
        ValueError,
    ) as exc:
        print(f"error: cannot update credentials file: {exc}", file=err)
        return 1

    file.upsert(provider, api_key)
    file.save()
    truncated = truncate_key(api_key)
    print(f"Added {provider}={truncated} to {_render_path(file.path)}", file=out)
    return 0


# ---------------------------------------------------------------------------
# `metis auth list`
# ---------------------------------------------------------------------------


def run_auth_list(
    *,
    file_path: Path | None = None,
    legacy_dotenv_path: Path | None = None,
    env: dict[str, str] | None = None,
    output_stream=None,
) -> int:
    """Tabular `provider | source | key` listing. Returns exit code."""
    out = output_stream or sys.stdout
    err = sys.stderr
    resolver = DefaultCredentialResolver(
        env=env,
        file_path=file_path,
        legacy_dotenv_path=legacy_dotenv_path,
    )

    # Surface a file-mode / schema problem early so the user understands
    # why subsequent rows look empty (otherwise list silently skips the
    # FILE source).
    loadable, detail = resolver.file_status()
    if not loadable and detail not in ("(not present)",):
        print(f"warning: credentials file unusable — {detail}", file=err)

    configured: dict[str, ConfiguredCredential] = {
        c.provider: c for c in resolver.list_configured()
    }
    rows = []
    for provider in provider_names():
        entry = configured.get(provider)
        if entry is None:
            rows.append((provider, "(not configured)", ""))
        else:
            rows.append(
                (
                    provider,
                    _render_source(entry.source, entry.source_detail),
                    entry.key_truncated,
                )
            )

    widths = [
        max(len("PROVIDER"), max((len(r[0]) for r in rows), default=0)),
        max(len("SOURCE"), max((len(r[1]) for r in rows), default=0)),
        max(len("KEY"), max((len(r[2]) for r in rows), default=0)),
    ]
    header = f"{'PROVIDER':<{widths[0]}}  {'SOURCE':<{widths[1]}}  {'KEY':<{widths[2]}}"
    print(header, file=out)
    for r in rows:
        print(f"{r[0]:<{widths[0]}}  {r[1]:<{widths[1]}}  {r[2]:<{widths[2]}}", file=out)
    return 0


# ---------------------------------------------------------------------------
# `metis auth remove`
# ---------------------------------------------------------------------------


def run_auth_remove(
    *,
    provider: str,
    file_path: Path | None = None,
    output_stream=None,
) -> int:
    """Idempotent remove of `provider` from the credentials file. Exit code."""
    out = output_stream or sys.stdout
    err = sys.stderr
    path = (file_path or default_credentials_file_path()).expanduser()
    if not path.exists():
        print(f"no credentials file at {_render_path(path)}; nothing to remove.", file=out)
        return 0
    try:
        file = CredentialsFile.load(path)
    except (
        CredentialsFileInsecure,
        CredentialsFileSchemaUnknown,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=err)
        return 1
    removed = file.remove(provider)
    if removed:
        file.save()
        print(f"removed {provider} from {_render_path(file.path)}", file=out)
    else:
        print(f"{provider} not present in {_render_path(file.path)}; nothing to do.", file=out)
    return 0


# ---------------------------------------------------------------------------
# `metis auth test`
# ---------------------------------------------------------------------------


def run_auth_test(
    *,
    provider: str | None = None,
    file_path: Path | None = None,
    legacy_dotenv_path: Path | None = None,
    env: dict[str, str] | None = None,
    validate_fn=None,
    output_stream=None,
) -> int:
    """Ping each configured provider's validate endpoint. Returns 0 on full pass.

    `validate_fn` is the ProviderSpec → key → ValidateResult callable; the
    default uses httpx for real HTTP. Tests inject a scripted callable
    instead of the network.
    """
    out = output_stream or sys.stdout
    resolver = DefaultCredentialResolver(
        env=env,
        file_path=file_path,
        legacy_dotenv_path=legacy_dotenv_path,
    )
    validator = validate_fn or validate_provider

    if provider is not None:
        if provider not in KNOWN_PROVIDERS:
            print(
                f"error: unknown provider {provider!r}. Known: {', '.join(provider_names())}.",
                file=sys.stderr,
            )
            return 2
        providers = [provider]
    else:
        providers = provider_names()

    all_ok = True
    any_configured = False
    for name in providers:
        key = resolver.get(name)
        if not key:
            print(f"{name:<12} (not configured)", file=out)
            continue
        any_configured = True
        result = validator(name, key)
        if result.ok:
            print(f"{name:<12} ok ({result.latency_ms} ms)", file=out)
        else:
            all_ok = False
            print(f"{name:<12} FAIL — {result.message}", file=out)
    if not any_configured:
        print("no providers configured; run `metis auth add <provider>` first.", file=out)
        return 1
    return 0 if all_ok else 1


# ---------------------------------------------------------------------------
# `metis auth doctor`
# ---------------------------------------------------------------------------


def run_auth_doctor(
    *,
    file_path: Path | None = None,
    legacy_dotenv_path: Path | None = None,
    env: dict[str, str] | None = None,
    db_path: Path | None = None,
    now: datetime | None = None,
    output_stream=None,
) -> int:
    """Full diagnostic: resolver state + per-provider readiness from the trace.

    Returns 0 always — doctor is a read-only report; non-zero would mislead
    operators into thinking they triggered a real failure.
    """
    out = output_stream or sys.stdout
    resolver = DefaultCredentialResolver(
        env=env,
        file_path=file_path,
        legacy_dotenv_path=legacy_dotenv_path,
    )
    now = now or datetime.now(UTC)

    print("Credential resolver:", file=out)
    loadable, file_detail = resolver.file_status()
    file_label = _render_path(resolver.file_path)
    print(f"  {file_label:<35}  {file_detail}", file=out)

    legacy_path = resolver.legacy_dotenv_path
    legacy_label = _render_path(legacy_path)
    legacy_detail = "present" if legacy_path.exists() else "(not present)"
    print(f"  {legacy_label:<35}  {legacy_detail}", file=out)

    print(f"  {'Keychain support':<35}  (opt-in; not active)", file=out)
    print("", file=out)

    configured: dict[str, ConfiguredCredential] = {
        c.provider: c for c in resolver.list_configured()
    }

    db = _open_trace_db(db_path)
    try:
        print("Providers:", file=out)
        for provider in provider_names():
            entry = configured.get(provider)
            if entry is None:
                print(f"  {provider:<14} (not configured)", file=out)
                print(f"  {'':<16}Add via: metis auth add {provider}", file=out)
                continue
            source_label = _render_source(entry.source, entry.source_detail)
            print(f"  {provider:<14} configured ({source_label})", file=out)
            if db is not None:
                last = _last_successful_call(db, provider)
                auth_errors = _recent_auth_errors(db, provider, now=now, window=timedelta(days=1))
                last_label = last.isoformat() if last else "(none in trace)"
                print(f"  {'':<16}last successful call: {last_label}", file=out)
                print(f"  {'':<16}recent AUTH errors:    {auth_errors} (last 24h)", file=out)
    finally:
        if db is not None:
            db.close()

    if loadable:
        file = resolver.loaded_file()
        if file is not None and file.default_provider:
            print("", file=out)
            print(f"Default provider: {file.default_provider}", file=out)
    return 0


# ---------------------------------------------------------------------------
# Provider validation (real HTTP)
# ---------------------------------------------------------------------------


def validate_provider(provider: str, api_key: str) -> ValidateResult:
    """Ping `provider`'s configured validate_endpoint with `api_key`.

    Never logs / surfaces `api_key`. The returned `message` contains HTTP
    status + a short error name only.
    """
    spec = KNOWN_PROVIDERS.get(provider)
    if spec is None:
        return ValidateResult(False, 0, None, f"unknown provider {provider!r}")
    method, url, body = spec.validate_endpoint
    headers: dict[str, str] = dict(spec.extra_headers)
    headers[spec.auth_header_name] = spec.auth_header_value_template.format(key=api_key)

    start = time.perf_counter()
    try:
        with httpx.Client(timeout=_VALIDATE_TIMEOUT_SECONDS) as client:
            response = client.request(method, url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        latency = int((time.perf_counter() - start) * 1000)
        # `repr(exc)` carries the exception class name; the api_key never
        # reaches the exception body because httpx receives it via headers
        # which are not part of the default exception message.
        return ValidateResult(False, latency, None, f"network error: {exc.__class__.__name__}")
    latency = int((time.perf_counter() - start) * 1000)

    if 200 <= response.status_code < 300:
        return ValidateResult(True, latency, response.status_code, "ok")
    if response.status_code in (401, 403):
        return ValidateResult(
            False,
            latency,
            response.status_code,
            f"AUTH error (HTTP {response.status_code}); key may be invalid or revoked",
        )
    return ValidateResult(
        False,
        latency,
        response.status_code,
        f"HTTP {response.status_code} from {provider}",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_source(source: CredentialSource, detail: str) -> str:
    if source == CredentialSource.ENV_VAR:
        return f"{detail} (env)"
    if source == CredentialSource.LEGACY_DOTENV:
        return f"{detail} (legacy .env)"
    if source == CredentialSource.CLI_FLAG:
        return "--api-key (cli)"
    if source == CredentialSource.KEYCHAIN:
        return f"keychain ({detail})"
    return detail


def _render_path(path: Path) -> str:
    try:
        home = Path.home()
        return f"~/{path.expanduser().relative_to(home)}"
    except (ValueError, RuntimeError):
        return str(path)


def _open_trace_db(db_path: Path | None) -> sqlite3.Connection | None:
    """Open the trace DB read-only. Returns None if missing / unreadable.

    Doctor is best-effort about per-provider history; an absent DB just
    means we don't render the last-call / recent-error lines.
    """
    if db_path is None:
        from metis.cli.runtime import default_db_path

        db_path = default_db_path()
    db_path = db_path.expanduser()
    if not db_path.exists():
        return None
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _last_successful_call(conn: sqlite3.Connection, provider: str) -> datetime | None:
    """Most-recent `llm.call_completed` event timestamp for `provider`.

    Reads the JSON payload's `provider` field via SQLite's `json_extract`
    (available since SQLite 3.38). The query is bounded by the existing
    `idx_events_type_id` index on `type`.
    """
    try:
        row = conn.execute(
            "SELECT MAX(timestamp_us) FROM events "
            "WHERE type = 'llm.call_completed' "
            "AND json_extract(payload_json, '$.provider') = ?",
            (provider,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row or row[0] is None:
        return None
    return datetime.fromtimestamp(row[0] / 1_000_000, tz=UTC)


def _recent_auth_errors(
    conn: sqlite3.Connection,
    provider: str,
    *,
    now: datetime,
    window: timedelta,
) -> int:
    cutoff_us = int((now - window).timestamp() * 1_000_000)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM events "
            "WHERE type = 'llm.call_failed' "
            "AND timestamp_us >= ? "
            "AND json_extract(payload_json, '$.provider') = ? "
            "AND json_extract(payload_json, '$.error_class') = 'auth'",
            (cutoff_us, provider),
        ).fetchone()
    except sqlite3.Error:
        return 0
    if not row:
        return 0
    return int(row[0])


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "ValidateResult",
    "run_auth_add",
    "run_auth_doctor",
    "run_auth_list",
    "run_auth_remove",
    "run_auth_test",
    "validate_provider",
]
