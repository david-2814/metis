"""Scripted-response provider adapter for cross-package tests.

Used by:
  - packages/metis-core/tests/sessions/test_manager.py (and test_streaming)
  - apps/cli/tests/tui/test_app.py

Lives at the workspace root so any workspace member's tests can import it
without depending on another member's test tree. The root `conftest.py`
puts the repo root on `sys.path` so `from tests_shared.scripted_adapter
import ...` resolves from anywhere.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass

from metis.core.adapters.protocol import (
    CanonicalRequest,
    CanonicalResponse,
    StopReason,
    TokenUsage,
)
from metis.core.adapters.streaming import (
    MessageComplete,
    MessageStart,
    TextDelta,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)
from metis.core.canonical.capabilities import AdapterCapabilities
from metis.core.canonical.content import TextBlock, ToolUseBlock
from metis.core.canonical.ids import new_message_id


@dataclass
class _ScriptedResponse:
    content: list
    stop_reason: StopReason
    input_tokens: int = 10
    output_tokens: int = 5


class _ScriptedAnthropicAdapter:
    """Returns scripted responses in order. Records every request."""

    name = "anthropic"

    def __init__(
        self,
        responses: list[_ScriptedResponse],
        *,
        capability_overrides: dict[str, AdapterCapabilities] | None = None,
    ) -> None:
        self._responses = list(responses)
        self.requests: list[CanonicalRequest] = []
        self._caps_overrides = capability_overrides or {}

    def capabilities_for(self, model: str) -> AdapterCapabilities:
        if model in self._caps_overrides:
            return self._caps_overrides[model]
        return AdapterCapabilities(
            supports_thinking=False,
            supports_images=True,
            supports_tools=True,
            supports_system_prompt=True,
            supports_structured_output=False,
            supports_streaming=True,
            supports_streaming_tool_calls=True,
            supports_parallel_tool_calls=True,
            supports_prompt_caching=True,
            supports_system_messages_in_list=False,
            max_context_tokens=200_000,
            max_output_tokens=8192,
        )

    async def complete(self, request: CanonicalRequest) -> CanonicalResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("scripted adapter ran out of responses")
        scripted = self._responses.pop(0)
        return CanonicalResponse(
            request_id=request.request_id,
            model=request.model,
            provider=self.name,
            content=scripted.content,
            stop_reason=scripted.stop_reason,
            usage=TokenUsage(
                input_tokens=scripted.input_tokens,
                output_tokens=scripted.output_tokens,
            ),
            latency_ms=42,
        )

    async def stream(self, request: CanonicalRequest):
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("scripted adapter ran out of responses")
        scripted = self._responses.pop(0)
        message_id = new_message_id()

        yield MessageStart(message_id=message_id, model=request.model)
        for idx, block in enumerate(scripted.content):
            if isinstance(block, TextBlock):
                yield TextDelta(message_id=message_id, content_block_index=idx, text=block.text)
            elif isinstance(block, ToolUseBlock):
                yield ToolUseStart(
                    message_id=message_id,
                    content_block_index=idx,
                    tool_use_id=block.id,
                    tool_name=block.name,
                )
                json_str = _json.dumps(block.input)
                yield ToolUseInputDelta(
                    message_id=message_id,
                    content_block_index=idx,
                    tool_use_id=block.id,
                    partial_json=json_str,
                )
                yield ToolUseEnd(
                    message_id=message_id,
                    content_block_index=idx,
                    tool_use_id=block.id,
                    final_input=block.input,
                )
        yield MessageComplete(
            message_id=message_id,
            stop_reason=scripted.stop_reason,
            final_content=scripted.content,
            usage=TokenUsage(
                input_tokens=scripted.input_tokens,
                output_tokens=scripted.output_tokens,
            ),
            latency_ms=42,
        )

    async def cancel(self, request_id: str) -> bool:
        return False

    async def close(self) -> None:
        return

    def estimate_input_tokens(self, messages, tools, system_prompt) -> int:
        return 100
