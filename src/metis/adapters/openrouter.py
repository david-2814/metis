"""OpenRouter provider adapter.

OpenRouter is an OpenAI-compatible aggregator that proxies to dozens of
models from various providers. The wire format on its `/api/v1/chat/completions`
endpoint matches OpenAI's, so we reuse the OpenAI wire-translation helpers.

The interesting parts are unique to OpenRouter:

- Model ids on the wire are `provider/model` (e.g. `anthropic/claude-sonnet-4`,
  `deepseek/deepseek-v3`). Canonical id form: `openrouter:provider/model`.
- The set of available models and their rates are fetched at startup from
  `/api/v1/models`. The returned catalog drives both per-model capabilities
  and pricing rates; nothing is hardcoded in this file.
- Headers: optional `HTTP-Referer` and `X-Title` for OpenRouter's analytics.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

import httpx
import openai

from metis.adapters.errors import (
    AdapterError,
    CancelledError,
    NetworkError,
)
from metis.adapters.openai import (
    _canonical_messages_to_openai,
    _classify_openai_response,
    _openai_message_to_canonical,
    _stop_reason,
    _tool_to_openai,
    _usage_to_canonical,
)
from metis.adapters.protocol import (
    CanonicalRequest,
    CanonicalResponse,
    TokenUsage,
)
from metis.adapters.retry import RetryPolicy, with_retry
from metis.adapters.tool_id_map import ToolIdMap
from metis.canonical.capabilities import AdapterCapabilities
from metis.canonical.messages import Message
from metis.canonical.tools import ToolDefinition
from metis.pricing.table import ModelPricing

logger = logging.getLogger(__name__)


DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_PER_MTOK = Decimal("1000000")


@dataclass(frozen=True)
class CatalogResult:
    """Result of OpenRouter.fetch_catalog()."""

    capabilities: dict[str, AdapterCapabilities]
    pricing: dict[str, ModelPricing]
    version: str  # opaque identifier (e.g. sha256 of catalog payload) for retroactive reprice


class OpenRouterAdapter:
    """OpenRouter adapter — OpenAI wire format with a dynamic catalog."""

    name = "openrouter"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        app_name: str | None = None,
        http_referer: str | None = None,
        timeout_seconds: float = 600.0,
        retry_policy: RetryPolicy | None = None,
        client: openai.AsyncOpenAI | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        extra_headers: dict[str, str] = {}
        if app_name:
            extra_headers["X-Title"] = app_name
        if http_referer:
            extra_headers["HTTP-Referer"] = http_referer
        # Disable SDK retries; we own retry policy.
        self._client = client or openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=extra_headers,
            timeout=timeout_seconds,
            max_retries=0,
        )
        self._retry_policy = retry_policy or RetryPolicy()
        self._in_flight: dict[str, asyncio.Task] = {}
        self._capabilities: dict[str, AdapterCapabilities] = {}
        self._catalog_version: str | None = None

    # ---- Catalog (capabilities + pricing) ------------------------------

    async def fetch_catalog(self, *, http_client: httpx.AsyncClient | None = None) -> CatalogResult:
        """Fetch /api/v1/models and build the canonical catalog.

        Caches capabilities internally. The caller is responsible for
        registering the returned pricing on the PriceTable (typically via
        `PriceTable.with_overlay`) and registering the models on the
        ModelRegistry.
        """
        owned_client = http_client is None
        client = http_client or httpx.AsyncClient(timeout=30.0)
        try:
            response = await client.get(
                f"{self._base_url}/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            response.raise_for_status()
            data = response.json().get("data") or []
        finally:
            if owned_client:
                await client.aclose()

        capabilities: dict[str, AdapterCapabilities] = {}
        pricing: dict[str, ModelPricing] = {}
        for entry in data:
            try:
                wire_id = entry.get("id")
                if not wire_id:
                    continue
                canonical_id = f"openrouter:{wire_id}"
                capabilities[canonical_id] = _parse_capabilities(entry)
                priced = _parse_pricing(entry)
                if priced is not None:
                    pricing[canonical_id] = priced
            except Exception:
                logger.warning("openrouter: skipping malformed catalog entry %r", entry.get("id"))

        version = _catalog_version(data)
        self._capabilities = capabilities
        self._catalog_version = version
        return CatalogResult(capabilities=capabilities, pricing=pricing, version=version)

    # ---- Public API ----------------------------------------------------

    def capabilities_for(self, model: str) -> AdapterCapabilities:
        try:
            return self._capabilities[model]
        except KeyError:
            raise ValueError(
                f"unknown openrouter model: {model!r} (catalog not fetched or model missing)"
            ) from None

    def estimate_input_tokens(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str | None,
    ) -> int:
        # Same ~4 chars/token heuristic as the other adapters.
        text_chars = 0
        if system_prompt:
            text_chars += len(system_prompt)
        for m in messages:
            for block in m.content:
                text_chars += _block_chars(block)
        for tool in tools:
            text_chars += len(tool.description) + len(str(tool.input_schema))
        return max(1, text_chars // 4)

    async def complete(self, request: CanonicalRequest) -> CanonicalResponse:
        task = asyncio.current_task()
        if task is not None:
            self._in_flight[request.request_id] = task
        try:
            return await with_retry(lambda: self._call_once(request), policy=self._retry_policy)
        except asyncio.CancelledError as exc:
            raise CancelledError("request cancelled", request_id=request.request_id) from exc
        finally:
            self._in_flight.pop(request.request_id, None)

    async def cancel(self, request_id: str) -> bool:
        task = self._in_flight.get(request_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def close(self) -> None:
        await self._client.close()

    # ---- Single call ---------------------------------------------------

    async def _call_once(self, request: CanonicalRequest) -> CanonicalResponse:
        tool_map = request.tool_id_map if request.tool_id_map is not None else ToolIdMap()
        wire_messages = _canonical_messages_to_openai(
            request.messages, request.system_prompt, tool_map
        )
        wire_tools = [_tool_to_openai(t) for t in request.tools]
        wire_model = _wire_model_name(request.model)

        kwargs: dict = {
            "model": wire_model,
            "max_completion_tokens": request.max_output_tokens,
            "messages": wire_messages,
        }
        if wire_tools:
            kwargs["tools"] = wire_tools
        if request.stop_sequences:
            kwargs["stop"] = request.stop_sequences
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.output_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "schema": request.output_schema,
                    "strict": True,
                },
            }

        start = time.monotonic()
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except openai.APIStatusError as exc:
            raise _translate_status_error(exc, request.request_id) from exc
        except openai.APIConnectionError as exc:
            raise NetworkError(
                f"openrouter connection error: {exc}", request_id=request.request_id
            ) from exc
        except openai.APITimeoutError as exc:
            raise NetworkError(f"openrouter timeout: {exc}", request_id=request.request_id) from exc
        except httpx.HTTPError as exc:
            raise NetworkError(f"http error: {exc}", request_id=request.request_id) from exc

        latency_ms = int((time.monotonic() - start) * 1000)
        choice = response.choices[0]
        content = _openai_message_to_canonical(choice.message, tool_map)
        usage = _usage_to_canonical(response.usage) if response.usage else TokenUsage(0, 0)
        return CanonicalResponse(
            request_id=request.request_id,
            model=request.model,
            provider=self.name,
            content=content,
            stop_reason=_stop_reason(choice.finish_reason),
            usage=usage,
            latency_ms=latency_ms,
        )


# ---------------------------------------------------------------------------
# Catalog parsing
# ---------------------------------------------------------------------------


def _wire_model_name(canonical: str) -> str:
    """`openrouter:anthropic/claude-sonnet-4` → `anthropic/claude-sonnet-4`."""
    if ":" not in canonical:
        return canonical
    return canonical.split(":", 1)[1]


def _parse_capabilities(entry: dict) -> AdapterCapabilities:
    """Build AdapterCapabilities from an OpenRouter /api/v1/models entry."""
    arch = entry.get("architecture") or {}
    input_modalities = arch.get("input_modalities") or []
    supported = entry.get("supported_parameters") or []

    supports_images = "image" in input_modalities
    supports_tools = "tools" in supported
    supports_structured_output = "response_format" in supported

    top_provider = entry.get("top_provider") or {}
    max_context = entry.get("context_length") or top_provider.get("context_length") or 8_192
    max_output = top_provider.get("max_completion_tokens") or 4_096

    return AdapterCapabilities(
        supports_thinking=False,  # OpenRouter doesn't expose thinking-block semantics uniformly
        supports_images=bool(supports_images),
        supports_tools=bool(supports_tools),
        supports_system_prompt=True,
        supports_structured_output=bool(supports_structured_output),
        supports_streaming=True,
        supports_streaming_tool_calls=bool(supports_tools),
        supports_parallel_tool_calls=bool(supports_tools),
        supports_prompt_caching=False,  # not reliably reportable across underlying providers
        supports_system_messages_in_list=True,
        max_context_tokens=int(max_context),
        max_output_tokens=int(max_output),
        accepted_image_media_types=(
            ["image/png", "image/jpeg", "image/gif", "image/webp"] if supports_images else []
        ),
    )


def _parse_pricing(entry: dict) -> ModelPricing | None:
    """Build ModelPricing from an OpenRouter pricing block. Returns None if
    prices are missing or look unusable (e.g. zero rates that suggest a free
    model — we treat $0 as legitimate, but missing fields skip the entry)."""
    pricing = entry.get("pricing")
    if not isinstance(pricing, dict):
        return None
    prompt_raw = pricing.get("prompt")
    completion_raw = pricing.get("completion")
    if prompt_raw is None or completion_raw is None:
        return None
    try:
        prompt_per_token = Decimal(str(prompt_raw))
        completion_per_token = Decimal(str(completion_raw))
    except Exception:
        return None
    cached_raw = pricing.get("input_cache_read") or pricing.get("cache_read")
    try:
        cached_per_token = Decimal(str(cached_raw)) if cached_raw else Decimal("0")
    except Exception:
        cached_per_token = Decimal("0")
    return ModelPricing(
        input_per_mtok=prompt_per_token * _PER_MTOK,
        output_per_mtok=completion_per_token * _PER_MTOK,
        cached_read_per_mtok=cached_per_token * _PER_MTOK,
        cache_creation_per_mtok=Decimal("0"),
    )


def _catalog_version(data: list[dict]) -> str:
    """Opaque version tag for retroactive reprice (canonical-format §6.4)."""
    import hashlib

    payload = b""
    for entry in data:
        wire_id = entry.get("id") or ""
        pricing = entry.get("pricing") or {}
        payload += f"{wire_id}:{pricing.get('prompt')}:{pricing.get('completion')}|".encode()
    digest = hashlib.sha256(payload).hexdigest()[:12]
    return f"openrouter-{digest}"


def _block_chars(block) -> int:
    from metis.canonical.content import (
        ImageBlock,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
    )

    if isinstance(block, TextBlock):
        return len(block.text)
    if isinstance(block, ToolUseBlock):
        return len(block.name) + len(str(block.input))
    if isinstance(block, ToolResultBlock):
        return sum(_block_chars(b) for b in block.content) + 16
    if isinstance(block, ImageBlock):
        return 1024
    return 0


# ---- Error translation -----------------------------------------------------


def _translate_status_error(exc: openai.APIStatusError, request_id: str) -> AdapterError:
    from metis.adapters.errors import error_for_class

    status = exc.status_code
    body: dict | None = None
    try:
        body = exc.response.json() if exc.response is not None else None
    except Exception:
        body = None
    classification = _classify_openai_response(status, body)
    msg = _provider_message(body) or str(exc)
    retry_after = _retry_after_seconds(exc)
    return error_for_class(
        classification,
        f"openrouter {status}: {msg}",
        provider_status=status,
        provider_message=msg,
        request_id=request_id,
        retry_after_seconds=retry_after,
    )


def _provider_message(body: dict | None) -> str:
    if not body or not isinstance(body, dict):
        return ""
    err = body.get("error")
    if isinstance(err, dict):
        return err.get("message", "")
    return ""


def _retry_after_seconds(exc: openai.APIStatusError) -> float | None:
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    header = resp.headers.get("retry-after") if hasattr(resp, "headers") else None
    if header:
        try:
            return float(header)
        except ValueError:
            return None
    return None


__all__ = ["CatalogResult", "OpenRouterAdapter"]
