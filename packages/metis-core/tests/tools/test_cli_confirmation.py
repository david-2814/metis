"""Tests for the CLI confirmation handler."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from metis_core.canonical.tools import SideEffects
from metis_core.tools.cli_confirmation import (
    TRUST_FILE_RELATIVE,
    CLIConfirmationHandler,
    TrustFileError,
    TrustList,
    load_trust_file,
    save_trust_file,
)
from metis_core.tools.confirmation import ConfirmationDecision, ConfirmationRequest


def _req(
    *,
    tool: str = "write_file",
    side_effects: SideEffects = SideEffects.WRITE,
    summary: str = "writing src/foo.py",
) -> ConfirmationRequest:
    return ConfirmationRequest(
        tool_use_id=f"toolu_{tool}_1",
        tool_name=tool,
        side_effects=side_effects,
        input_summary=summary,
    )


# ---- Trust-file IO ------------------------------------------------------


def test_load_trust_file_missing_returns_empty(tmp_path: Path) -> None:
    result = load_trust_file(tmp_path / "trust.yaml")
    assert result == TrustList()


def test_load_trust_file_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "trust.yaml"
    original = TrustList(
        always_allow=frozenset({"read_file", "list_dir"}),
        always_deny=frozenset({"shell"}),
    )
    save_trust_file(path, original)
    assert load_trust_file(path) == original


def test_load_trust_file_empty_yaml(tmp_path: Path) -> None:
    path = tmp_path / "trust.yaml"
    path.write_text("", encoding="utf-8")
    assert load_trust_file(path) == TrustList()


def test_load_trust_file_rejects_non_mapping(tmp_path: Path) -> None:
    path = tmp_path / "trust.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(TrustFileError):
        load_trust_file(path)


def test_load_trust_file_rejects_non_string_entries(tmp_path: Path) -> None:
    path = tmp_path / "trust.yaml"
    path.write_text("always_allow: [1, 2]\n", encoding="utf-8")
    with pytest.raises(TrustFileError):
        load_trust_file(path)


# ---- Side-effect dispatching --------------------------------------------


@pytest.mark.asyncio
async def test_read_side_effect_auto_approves(tmp_path: Path) -> None:
    handler = CLIConfirmationHandler(tmp_path, input_fn=_unexpected_input)
    decision = await handler.request(_req(tool="read_file", side_effects=SideEffects.READ))
    assert decision == ConfirmationDecision.ALLOW


@pytest.mark.asyncio
async def test_none_side_effect_auto_approves(tmp_path: Path) -> None:
    handler = CLIConfirmationHandler(tmp_path, input_fn=_unexpected_input)
    decision = await handler.request(_req(tool="noop", side_effects=SideEffects.NONE))
    assert decision == ConfirmationDecision.ALLOW


@pytest.mark.asyncio
async def test_write_prompts_when_not_in_trust(tmp_path: Path) -> None:
    answers = iter(["y"])
    handler = CLIConfirmationHandler(
        tmp_path,
        input_fn=lambda _prompt: next(answers),
        output_fn=lambda _msg: None,
    )
    decision = await handler.request(_req(side_effects=SideEffects.WRITE))
    assert decision == ConfirmationDecision.ALLOW


@pytest.mark.asyncio
async def test_execute_prompts_when_not_in_trust(tmp_path: Path) -> None:
    answers = iter(["n"])
    handler = CLIConfirmationHandler(
        tmp_path,
        input_fn=lambda _prompt: next(answers),
        output_fn=lambda _msg: None,
    )
    decision = await handler.request(_req(tool="shell", side_effects=SideEffects.EXECUTE))
    assert decision == ConfirmationDecision.DENY


@pytest.mark.asyncio
async def test_network_prompts_when_not_in_trust(tmp_path: Path) -> None:
    answers = iter([""])  # empty answer → DENY
    handler = CLIConfirmationHandler(
        tmp_path,
        input_fn=lambda _prompt: next(answers),
        output_fn=lambda _msg: None,
    )
    decision = await handler.request(_req(tool="web_post", side_effects=SideEffects.NETWORK))
    assert decision == ConfirmationDecision.DENY


# ---- Trust-list short-circuits -------------------------------------------


@pytest.mark.asyncio
async def test_always_allow_skips_prompt(tmp_path: Path) -> None:
    trust_path = tmp_path / ".metis" / "trust.yaml"
    save_trust_file(trust_path, TrustList(always_allow=frozenset({"write_file"})))
    handler = CLIConfirmationHandler(tmp_path, input_fn=_unexpected_input)
    decision = await handler.request(_req(side_effects=SideEffects.WRITE))
    assert decision == ConfirmationDecision.ALLOW


@pytest.mark.asyncio
async def test_always_deny_skips_prompt(tmp_path: Path) -> None:
    trust_path = tmp_path / ".metis" / "trust.yaml"
    save_trust_file(trust_path, TrustList(always_deny=frozenset({"shell"})))
    handler = CLIConfirmationHandler(tmp_path, input_fn=_unexpected_input)
    decision = await handler.request(_req(tool="shell", side_effects=SideEffects.EXECUTE))
    assert decision == ConfirmationDecision.DENY


# ---- Persistence ---------------------------------------------------------


@pytest.mark.asyncio
async def test_always_appends_to_trust_file(tmp_path: Path) -> None:
    answers = iter(["always"])
    captured: list[str] = []
    handler = CLIConfirmationHandler(
        tmp_path,
        input_fn=lambda _prompt: next(answers),
        output_fn=captured.append,
    )
    decision = await handler.request(_req(side_effects=SideEffects.WRITE))
    assert decision == ConfirmationDecision.ALLOW

    trust_path = tmp_path / TRUST_FILE_RELATIVE
    assert trust_path.exists()
    loaded = load_trust_file(trust_path)
    assert "write_file" in loaded.always_allow
    raw = yaml.safe_load(trust_path.read_text())
    assert raw["always_allow"] == ["write_file"]


@pytest.mark.asyncio
async def test_never_appends_to_deny_list(tmp_path: Path) -> None:
    answers = iter(["never"])
    handler = CLIConfirmationHandler(
        tmp_path,
        input_fn=lambda _prompt: next(answers),
        output_fn=lambda _msg: None,
    )
    decision = await handler.request(_req(tool="shell", side_effects=SideEffects.EXECUTE))
    assert decision == ConfirmationDecision.DENY

    trust_path = tmp_path / TRUST_FILE_RELATIVE
    loaded = load_trust_file(trust_path)
    assert "shell" in loaded.always_deny


@pytest.mark.asyncio
async def test_always_promotes_over_existing_deny(tmp_path: Path) -> None:
    trust_path = tmp_path / TRUST_FILE_RELATIVE
    save_trust_file(trust_path, TrustList(always_deny=frozenset({"write_file"})))
    answers = iter(["always"])
    # Build a request whose pre-existing deny would short-circuit; we expect
    # the user override via "always" to remove it from the deny list.
    # To get a prompt, we need a clean handler — but the trust file already
    # has "always_deny: [write_file]", so request() short-circuits to DENY.
    # We test the persistence layer directly here:
    handler = CLIConfirmationHandler(
        tmp_path,
        input_fn=lambda _prompt: next(answers),
        output_fn=lambda _msg: None,
    )
    # Direct persistence path: "always" → moves out of deny into allow.
    handler._persist(allow="write_file")
    loaded = load_trust_file(trust_path)
    assert "write_file" in loaded.always_allow
    assert "write_file" not in loaded.always_deny
    # Suppress unused-variable warnings: answers iterator wasn't consumed.
    _ = answers


# ---- Timeout -------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_returns_deny(tmp_path: Path) -> None:
    async def _slow_input(_prompt: str) -> str:
        await asyncio.sleep(5.0)
        return "y"

    # We can't pass an async fn to input_fn directly (it expects sync),
    # so simulate a slow sync input by sleeping on a thread.
    def _blocking_input(_prompt: str) -> str:
        import time

        time.sleep(5.0)
        return "y"

    captured: list[str] = []
    handler = CLIConfirmationHandler(
        tmp_path,
        timeout_seconds=0.05,
        input_fn=_blocking_input,
        output_fn=captured.append,
    )
    decision = await handler.request(_req(side_effects=SideEffects.WRITE))
    assert decision == ConfirmationDecision.DENY
    assert any("no response" in line for line in captured)


# ---- Malformed trust file fails closed -----------------------------------


@pytest.mark.asyncio
async def test_malformed_trust_file_prompts_anyway(tmp_path: Path) -> None:
    trust_path = tmp_path / ".metis" / "trust.yaml"
    trust_path.parent.mkdir()
    trust_path.write_text("- not a mapping\n", encoding="utf-8")
    answers = iter(["y"])
    handler = CLIConfirmationHandler(
        tmp_path,
        input_fn=lambda _prompt: next(answers),
        output_fn=lambda _msg: None,
    )
    decision = await handler.request(_req(side_effects=SideEffects.WRITE))
    assert decision == ConfirmationDecision.ALLOW


# ---- Helpers -------------------------------------------------------------


def _unexpected_input(_prompt: str) -> str:
    raise AssertionError("input should not be requested in this case")
