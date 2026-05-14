"""Terminal-prompting confirmation handler for the CLI.

Replaces `AutoAllowHandler` as the default for `metis chat` / `metis tui`.
Auto-approves `NONE` and `READ` side effects (matching the conservative
defaults in `ConfirmationPolicy`); for `WRITE` / `EXECUTE` / `NETWORK`
side effects, consults a per-workspace trust file and otherwise prompts
the user at the terminal.

Trust file: `<workspace>/.metis/trust.yaml`

    schema_version: 1
    always_allow:
      - read_file
      - list_dir
    always_deny:
      - shell

Prompt answers:

    y / yes        → ALLOW (this call only)
    n / no / ""    → DENY (this call only)
    a / always     → ALLOW + persist to `always_allow`
    never          → DENY + persist to `always_deny`

A 60-second default timeout applies to the prompt itself; on timeout the
handler returns DENY (per spec). The outer dispatcher also wraps the
handler in a longer wait_for, so a dropped CLI never blocks tool dispatch
indefinitely.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from metis_core.canonical.tools import SideEffects
from metis_core.tools.confirmation import ConfirmationDecision, ConfirmationRequest

logger = logging.getLogger(__name__)


TRUST_FILE_RELATIVE = Path(".metis") / "trust.yaml"


_AUTO_APPROVE_CLASSES: frozenset[SideEffects] = frozenset({SideEffects.NONE, SideEffects.READ})


class TrustFileError(Exception):
    """Raised when the trust file is malformed and cannot be loaded."""


@dataclass(frozen=True)
class TrustList:
    always_allow: frozenset[str] = field(default_factory=frozenset)
    always_deny: frozenset[str] = field(default_factory=frozenset)


def _empty_trust() -> TrustList:
    return TrustList()


def load_trust_file(path: Path) -> TrustList:
    """Read the trust file at `path`. Missing file → empty trust.

    Raises TrustFileError on malformed yaml or unexpected shape.
    """
    if not path.exists():
        return _empty_trust()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TrustFileError(f"trust file {path} is not valid yaml: {exc}") from exc
    if raw is None:
        return _empty_trust()
    if not isinstance(raw, dict):
        raise TrustFileError(f"trust file {path} must be a mapping at top level")
    allow_raw = raw.get("always_allow", []) or []
    deny_raw = raw.get("always_deny", []) or []
    if not isinstance(allow_raw, list) or not all(isinstance(x, str) for x in allow_raw):
        raise TrustFileError(f"`always_allow` in {path} must be a list of tool names")
    if not isinstance(deny_raw, list) or not all(isinstance(x, str) for x in deny_raw):
        raise TrustFileError(f"`always_deny` in {path} must be a list of tool names")
    return TrustList(
        always_allow=frozenset(allow_raw),
        always_deny=frozenset(deny_raw),
    )


def save_trust_file(path: Path, trust: TrustList) -> None:
    """Write `trust` back to `path`, creating the parent directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "always_allow": sorted(trust.always_allow),
        "always_deny": sorted(trust.always_deny),
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


class CLIConfirmationHandler:
    """ConfirmationHandler that prompts at the terminal.

    Implements the `ConfirmationHandler` protocol from
    `tools/confirmation.py`. Reads the trust file on every request so
    "always" persists across the running session without an in-memory
    cache that drifts from disk.
    """

    def __init__(
        self,
        workspace_path: str | Path,
        *,
        timeout_seconds: float = 60.0,
        input_fn: Callable[[str], str] | None = None,
        output_fn: Callable[[str], None] | None = None,
        trust_path: Path | None = None,
    ) -> None:
        self._workspace = Path(workspace_path).expanduser().resolve()
        self._timeout = timeout_seconds
        self._input = input_fn or input
        self._output = output_fn or (lambda s: print(s, file=sys.stderr, flush=True))
        self._trust_path = trust_path or (self._workspace / TRUST_FILE_RELATIVE)

    @property
    def trust_path(self) -> Path:
        return self._trust_path

    def _load_trust(self) -> TrustList:
        try:
            return load_trust_file(self._trust_path)
        except TrustFileError as exc:
            # A malformed trust file shouldn't fail-open. Log + treat as empty
            # so the handler still prompts for every call.
            logger.warning("ignoring malformed trust file: %s", exc)
            return _empty_trust()

    def _persist(self, *, allow: str | None = None, deny: str | None = None) -> None:
        trust = self._load_trust()
        always_allow = set(trust.always_allow)
        always_deny = set(trust.always_deny)
        if allow is not None:
            always_allow.add(allow)
            always_deny.discard(allow)
        if deny is not None:
            always_deny.add(deny)
            always_allow.discard(deny)
        save_trust_file(
            self._trust_path,
            TrustList(
                always_allow=frozenset(always_allow),
                always_deny=frozenset(always_deny),
            ),
        )

    async def request(self, req: ConfirmationRequest) -> ConfirmationDecision:
        if req.side_effects in _AUTO_APPROVE_CLASSES:
            return ConfirmationDecision.ALLOW

        trust = self._load_trust()
        if req.tool_name in trust.always_deny:
            logger.info("trust file denies %s; rejecting", req.tool_name)
            return ConfirmationDecision.DENY
        if req.tool_name in trust.always_allow:
            logger.debug("trust file allows %s; auto-approving", req.tool_name)
            return ConfirmationDecision.ALLOW

        prompt = self._format_prompt(req)
        try:
            answer = await asyncio.wait_for(
                asyncio.to_thread(self._prompt_once, prompt), self._timeout
            )
        except TimeoutError:
            self._output(
                f"\n[metis] no response within {int(self._timeout)}s — denying {req.tool_name}."
            )
            return ConfirmationDecision.DENY

        return self._apply_answer(answer, req)

    def _format_prompt(self, req: ConfirmationRequest) -> str:
        lines = [
            "",
            f"[metis] tool {req.tool_name!r} ({req.side_effects.value}) wants to run.",
            f"        input: {req.input_summary}",
        ]
        if req.projected_modifications:
            lines.append(f"        will modify: {', '.join(req.projected_modifications)}")
        if req.command_summary:
            lines.append(f"        command: {req.command_summary}")
        lines.append("        approve? [y]es / [n]o / [a]lways / never : ")
        return "\n".join(lines)

    def _prompt_once(self, prompt: str) -> str:
        try:
            return self._input(prompt)
        except (EOFError, KeyboardInterrupt):
            return ""

    def _apply_answer(self, answer: str, req: ConfirmationRequest) -> ConfirmationDecision:
        normalized = (answer or "").strip().lower()
        if normalized in {"y", "yes"}:
            return ConfirmationDecision.ALLOW
        if normalized in {"a", "always"}:
            self._persist(allow=req.tool_name)
            self._output(f"[metis] {req.tool_name} added to {self._trust_path}")
            return ConfirmationDecision.ALLOW
        if normalized == "never":
            self._persist(deny=req.tool_name)
            self._output(f"[metis] {req.tool_name} added to deny list in {self._trust_path}")
            return ConfirmationDecision.DENY
        # Empty / "n" / "no" / anything else → DENY (safe default).
        return ConfirmationDecision.DENY
