"""Shell tool: run a shell command in the workspace.

Sends SIGTERM on cancel; SIGKILL after 5 seconds if still running
(tool-dispatcher.md §8).
"""

from __future__ import annotations

import asyncio
import signal

from metis.canonical.content import TextBlock
from metis.canonical.tools import SideEffects, ToolDefinition
from metis.tools.protocol import ToolContext, ToolOutput

_SIGKILL_DELAY_SECONDS = 5.0
_OUTPUT_CAP_BYTES = 256 * 1024  # 256 KB; truncate to avoid runaway memory


class ShellTool:
    definition = ToolDefinition(
        name="shell",
        description="Run a shell command in the workspace directory and capture output.",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 600},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        side_effects=SideEffects.EXECUTE,
    )

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        command = input["command"]
        self._process = await asyncio.create_subprocess_shell(
            command,
            cwd=context.workspace_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout_bytes = (
                await self._process.stdout.read(_OUTPUT_CAP_BYTES) if self._process.stdout else b""
            )
            # Drain anything past the cap so the process doesn't block on pipe.
            if self._process.stdout and not self._process.stdout.at_eof():
                tail = await self._process.stdout.read()
                stdout_bytes += b"\n... [output truncated]" if tail else b""
            returncode = await self._process.wait()
        except asyncio.CancelledError:
            await self._terminate()
            raise
        text = stdout_bytes.decode(errors="replace")
        success = returncode == 0
        body = f"$ {command}\nexit_code={returncode}\n\n{text}".strip()
        if not success:
            # Non-zero exit is reported but doesn't raise — the agent often
            # wants the output (e.g., `git status` on a non-repo).
            return ToolOutput(
                content=[TextBlock(text=body)],
                success=False,
                command_executed=command,
                metadata={"exit_code": returncode},
            )
        return ToolOutput(
            content=[TextBlock(text=body)],
            success=True,
            command_executed=command,
            metadata={"exit_code": returncode},
        )

    async def cancel(self) -> bool:
        return await self._terminate()

    async def _terminate(self) -> bool:
        proc = self._process
        if proc is None or proc.returncode is not None:
            return False
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return False
        try:
            await asyncio.wait_for(proc.wait(), _SIGKILL_DELAY_SECONDS)
            return True
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                return False
            try:
                await proc.wait()
            except Exception:
                pass
            return True
