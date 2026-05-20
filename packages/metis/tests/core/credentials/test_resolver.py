"""Resolution-chain tests for DefaultCredentialResolver."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from metis.core.credentials import (
    CredentialsFile,
    CredentialsFileInsecure,
    CredentialsFileSchemaUnknown,
    CredentialSource,
    DefaultCredentialResolver,
    truncate_key,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_credentials_file(
    tmp_path: Path,
    providers: dict[str, str],
    *,
    mode: int = 0o600,
    schema_version: int = 1,
) -> Path:
    path = tmp_path / "credentials.yaml"
    file = CredentialsFile(
        path=path,
        schema_version=schema_version,
        providers=dict(providers),
    )
    file.save()
    os.chmod(path, mode)
    return path


def _write_legacy_env(tmp_path: Path, env: dict[str, str]) -> Path:
    path = tmp_path / ".env"
    body = "".join(f"{k}={v}\n" for k, v in env.items())
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Spec §3 chain ordering (truth table)
# ---------------------------------------------------------------------------


def test_cli_override_wins_over_env_file_and_legacy(tmp_path: Path) -> None:
    file_path = _write_credentials_file(tmp_path, {"anthropic": "from-file-key"})
    legacy_path = _write_legacy_env(tmp_path, {"ANTHROPIC_API_KEY": "from-legacy-key"})
    resolver = DefaultCredentialResolver(
        cli_overrides={"anthropic": "from-cli-key"},
        env={"ANTHROPIC_API_KEY": "from-env-key"},
        file_path=file_path,
        legacy_dotenv_path=legacy_path,
    )
    assert resolver.get("anthropic") == "from-cli-key"


def test_env_var_wins_over_file_and_legacy(tmp_path: Path) -> None:
    file_path = _write_credentials_file(tmp_path, {"anthropic": "from-file-key"})
    legacy_path = _write_legacy_env(tmp_path, {"ANTHROPIC_API_KEY": "from-legacy-key"})
    resolver = DefaultCredentialResolver(
        env={"ANTHROPIC_API_KEY": "from-env-key"},
        file_path=file_path,
        legacy_dotenv_path=legacy_path,
    )
    assert resolver.get("anthropic") == "from-env-key"


def test_file_wins_over_legacy_when_env_missing(tmp_path: Path) -> None:
    file_path = _write_credentials_file(tmp_path, {"anthropic": "from-file-key"})
    legacy_path = _write_legacy_env(tmp_path, {"ANTHROPIC_API_KEY": "from-legacy-key"})
    resolver = DefaultCredentialResolver(
        env={},
        file_path=file_path,
        legacy_dotenv_path=legacy_path,
    )
    assert resolver.get("anthropic") == "from-file-key"


def test_legacy_used_when_only_source(tmp_path: Path) -> None:
    legacy_path = _write_legacy_env(tmp_path, {"ANTHROPIC_API_KEY": "from-legacy-key"})
    resolver = DefaultCredentialResolver(
        env={},
        file_path=tmp_path / "missing.yaml",
        legacy_dotenv_path=legacy_path,
    )
    assert resolver.get("anthropic") == "from-legacy-key"


def test_returns_none_when_every_source_misses(tmp_path: Path) -> None:
    resolver = DefaultCredentialResolver(
        env={},
        file_path=tmp_path / "missing.yaml",
        legacy_dotenv_path=tmp_path / "missing.env",
    )
    assert resolver.get("anthropic") is None
    assert resolver.list_configured() == []


# ---------------------------------------------------------------------------
# Provenance — list_configured must surface the winning source per provider
# ---------------------------------------------------------------------------


def test_list_configured_records_source_per_provider(tmp_path: Path) -> None:
    file_path = _write_credentials_file(tmp_path, {"openrouter": "from-file-key"})
    legacy_path = _write_legacy_env(tmp_path, {"OPENROUTER_API_KEY": "from-legacy-key"})
    resolver = DefaultCredentialResolver(
        env={"OPENAI_API_KEY": "from-env-key"},
        file_path=file_path,
        legacy_dotenv_path=legacy_path,
    )
    entries = {c.provider: c for c in resolver.list_configured()}
    # anthropic not configured anywhere
    assert "anthropic" not in entries
    # openai resolved from env
    assert entries["openai"].source == CredentialSource.ENV_VAR
    assert entries["openai"].source_detail == "OPENAI_API_KEY"
    assert entries["openai"].key_truncated == truncate_key("from-env-key")
    # openrouter resolved from file (env miss → file wins over legacy)
    assert entries["openrouter"].source == CredentialSource.FILE
    assert entries["openrouter"].key_truncated == truncate_key("from-file-key")


# ---------------------------------------------------------------------------
# File-mode enforcement
# ---------------------------------------------------------------------------


def test_file_loose_mode_rejected(tmp_path: Path) -> None:
    file_path = _write_credentials_file(tmp_path, {"anthropic": "from-file-key"}, mode=0o644)
    resolver = DefaultCredentialResolver(
        env={},
        file_path=file_path,
        legacy_dotenv_path=tmp_path / "missing.env",
    )
    # Resolution silently skips the file (chain continues to legacy/none).
    assert resolver.get("anthropic") is None
    # file_status surfaces the problem for the doctor / runtime preflight.
    loadable, detail = resolver.file_status()
    assert loadable is False
    assert "insecure mode 0644" in detail


def test_file_load_raises_credentials_file_insecure(tmp_path: Path) -> None:
    file_path = _write_credentials_file(tmp_path, {"anthropic": "from-file-key"}, mode=0o644)
    with pytest.raises(CredentialsFileInsecure) as exc:
        CredentialsFile.load(file_path)
    assert exc.value.mode == 0o644
    assert "chmod 600" in str(exc.value)


# ---------------------------------------------------------------------------
# Schema-version enforcement
# ---------------------------------------------------------------------------


def test_unknown_schema_version_refused(tmp_path: Path) -> None:
    # Bypass CredentialsFile.save() so we can write a v2 file even though
    # this build only understands v1.
    path = tmp_path / "credentials.yaml"
    path.write_text(
        "schema_version: 2\nproviders:\n  anthropic:\n    api_key: foo\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    with pytest.raises(CredentialsFileSchemaUnknown) as exc:
        CredentialsFile.load(path)
    assert exc.value.schema_version == 2


def test_resolver_silently_skips_unknown_schema_but_status_loud(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credentials.yaml"
    path.write_text(
        "schema_version: 99\nproviders:\n  anthropic:\n    api_key: foo\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    resolver = DefaultCredentialResolver(
        env={},
        file_path=path,
        legacy_dotenv_path=tmp_path / "missing.env",
    )
    assert resolver.get("anthropic") is None
    loadable, detail = resolver.file_status()
    assert loadable is False
    assert "unsupported schema_version" in detail


# ---------------------------------------------------------------------------
# Atomic write — partial-write crash leaves the previous file intact
# ---------------------------------------------------------------------------


def test_atomic_write_survives_mid_write_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_path = tmp_path / "credentials.yaml"
    initial = CredentialsFile(path=file_path, providers={"anthropic": "first-key"})
    initial.save()
    before_text = file_path.read_text(encoding="utf-8")

    # Patch os.replace to simulate a crash AFTER the temp file is written
    # but BEFORE the atomic rename completes.
    import metis.core.credentials.file as module_under_test

    def boom(_src: str, _dst: str) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(module_under_test.os, "replace", boom)

    crashing = CredentialsFile(
        path=file_path,
        providers={"anthropic": "first-key", "openai": "new-key"},
    )
    with pytest.raises(OSError):
        crashing.save()

    # The original file is untouched; no stray temp files left behind.
    assert file_path.read_text(encoding="utf-8") == before_text
    leftover = [p for p in file_path.parent.iterdir() if p.name.startswith(".credentials.")]
    assert leftover == []


def test_save_chmods_to_0600(tmp_path: Path) -> None:
    file_path = tmp_path / "credentials.yaml"
    file = CredentialsFile(path=file_path, providers={"anthropic": "x" * 30})
    file.save()
    import stat

    mode = stat.S_IMODE(os.stat(file_path).st_mode)
    assert mode == 0o600


# ---------------------------------------------------------------------------
# Unknown provider names default to {NAME}_API_KEY env var (forward-compat)
# ---------------------------------------------------------------------------


def test_unknown_provider_uses_uppercase_env_var(tmp_path: Path) -> None:
    resolver = DefaultCredentialResolver(
        env={"MISTRAL_API_KEY": "mistral-secret"},
        file_path=tmp_path / "missing.yaml",
        legacy_dotenv_path=tmp_path / "missing.env",
    )
    assert resolver.get("mistral") == "mistral-secret"


# ---------------------------------------------------------------------------
# Reload drops caches so re-saved files are picked up
# ---------------------------------------------------------------------------


def test_reload_picks_up_file_changes(tmp_path: Path) -> None:
    file_path = _write_credentials_file(tmp_path, {"anthropic": "first"})
    resolver = DefaultCredentialResolver(
        env={}, file_path=file_path, legacy_dotenv_path=tmp_path / "missing.env"
    )
    assert resolver.get("anthropic") == "first"
    updated = CredentialsFile.load(file_path)
    updated.upsert("anthropic", "second")
    updated.save()
    # Without reload, cache is stale.
    assert resolver.get("anthropic") == "first"
    resolver.reload()
    assert resolver.get("anthropic") == "second"
