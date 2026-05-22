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
import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

import httpx
import openai

from metis.core.adapters.errors import (
    AdapterError,
    CancelledError,
    NetworkError,
)
from metis.core.adapters.openai import (
    _canonical_messages_to_openai,
    _classify_openai_response,
    _openai_message_to_canonical,
    _stop_reason,
    _stream_openai_compat,
    _tool_to_openai,
    _usage_to_canonical,
)
from metis.core.adapters.protocol import (
    CanonicalRequest,
    CanonicalResponse,
    TokenUsage,
)
from metis.core.adapters.retry import RetryPolicy, with_retry
from metis.core.adapters.tool_id_map import ToolIdMap
from metis.core.canonical.capabilities import AdapterCapabilities
from metis.core.canonical.messages import Message
from metis.core.canonical.tools import ToolDefinition
from metis.core.pricing.table import ModelPricing

logger = logging.getLogger(__name__)


DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_PER_MTOK = Decimal("1000000")

# Upstreams reached via OpenRouter that cache *only* the spans the client
# marks with an explicit `cache_control` breakpoint (Anthropic Claude,
# Google Gemini's explicit path, Alibaba Qwen) — as opposed to implicit
# prefix caching (OpenAI, DeepSeek, …) which needs no markers. OpenRouter's
# /api/v1/models exposes no caching-style signal, so this is a maintained
# wire-id-prefix allowlist; review it when OpenRouter onboards new
# explicit-caching providers. See provider-adapter-contract.md §4.5.2.
EXPLICIT_BREAKPOINT_FAMILIES = ("anthropic/", "google/", "qwen/")


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

    # ---- Streaming -----------------------------------------------------

    async def stream(self, request: CanonicalRequest):
        """Stream via the OpenAI-compatible endpoint (same as OpenAIAdapter)."""
        task = asyncio.current_task()
        if task is not None:
            self._in_flight[request.request_id] = task
        try:
            async for event in _stream_openai_compat(
                client=self._client,
                request=request,
                provider_name=self.name,
                wire_model=_wire_model_name(request.model),
                _on_translate_error=_translate_status_error,
                cache_system_breakpoint=self._wants_cache_breakpoint(request.model),
                extra_body=_provider_routing(),
            ):
                yield event
        except asyncio.CancelledError as exc:
            raise CancelledError("request cancelled", request_id=request.request_id) from exc
        finally:
            self._in_flight.pop(request.request_id, None)

    # ---- Prompt caching ------------------------------------------------

    def _wants_cache_breakpoint(self, model: str) -> bool:
        """True iff `model` is an explicit-breakpoint-family upstream that
        also has cache-read pricing — provider-adapter-contract.md §4.5.2.

        Implicit-cache upstreams (OpenAI, DeepSeek, …) return False: they
        cache the prompt prefix on their own, so attaching a breakpoint
        would be wrong. The capability gate (`supports_prompt_caching`,
        derived from `pricing.input_cache_read`) means the catalog must be
        fetched first; without it no breakpoints are emitted, which is the
        safe default.
        """
        if not _wire_model_name(model).startswith(EXPLICIT_BREAKPOINT_FAMILIES):
            return False
        caps = self._capabilities.get(model)
        return caps is not None and caps.supports_prompt_caching

    # ---- Single call ---------------------------------------------------

    async def _call_once(self, request: CanonicalRequest) -> CanonicalResponse:
        tool_map = request.tool_id_map if request.tool_id_map is not None else ToolIdMap()
        wire_messages = _canonical_messages_to_openai(
            request.messages,
            request.system_prompt,
            tool_map,
            system_prompt_volatile=request.system_prompt_volatile,
            cache_system_breakpoint=self._wants_cache_breakpoint(request.model),
        )
        wire_tools = [_tool_to_openai(t) for t in request.tools]
        wire_model = _wire_model_name(request.model)

        kwargs: dict = {
            "model": wire_model,
            "max_completion_tokens": request.max_output_tokens,
            "messages": wire_messages,
            "extra_body": _provider_routing(),
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


def _provider_routing() -> dict:
    """The OpenRouter `provider` routing object attached to every request,
    delivered through the SDK's `extra_body` escape hatch (the field is not
    in OpenAI's schema).

    `allow_fallbacks: true` keeps OpenRouter's failover to the next-best
    upstream when the primary is unavailable. A *bare* routing object (no
    `order` / `sort` / `only`) leaves OpenRouter's automatic provider sticky
    routing intact, so cache prefixes stay warm — see
    provider-adapter-contract.md §4.5.5 for the trade-off.

    A fresh dict is returned per call so a caller (or the SDK) mutating
    `extra_body` can't leak across requests.
    """
    return {"provider": {"allow_fallbacks": True}}


def _parse_capabilities(entry: dict) -> AdapterCapabilities:
    """Build AdapterCapabilities from an OpenRouter /api/v1/models entry."""
    arch = entry.get("architecture") or {}
    input_modalities = arch.get("input_modalities") or []
    supported = entry.get("supported_parameters") or []

    supports_images = "image" in input_modalities
    supports_tools = "tools" in supported
    supports_structured_output = "response_format" in supported
    supports_prompt_caching = _catalog_supports_caching(entry)

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
        supports_prompt_caching=supports_prompt_caching,
        supports_system_messages_in_list=True,
        max_context_tokens=int(max_context),
        max_output_tokens=int(max_output),
        accepted_image_media_types=(
            ["image/png", "image/jpeg", "image/gif", "image/webp"] if supports_images else []
        ),
    )


def _catalog_supports_caching(entry: dict) -> bool:
    """Per provider-adapter-contract.md §4.5.2: OpenRouter's /api/v1/models
    exposes no dedicated caching capability flag and `supported_parameters`
    omits `cache_control`. The presence of `pricing.input_cache_read` (cache
    reads are separately priced) is the only machine-readable signal that
    prompt caching pays off for a model — so it drives the honest per-model
    `supports_prompt_caching` declaration."""
    pricing = entry.get("pricing")
    return isinstance(pricing, dict) and pricing.get("input_cache_read") is not None


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
    # Cache *writes* are separately priced for explicit-breakpoint upstreams
    # (Anthropic, Gemini); absent on implicit-cache models — default to 0.
    # See provider-adapter-contract.md §4.5.2.
    write_raw = pricing.get("input_cache_write") or pricing.get("cache_write")
    try:
        write_per_token = Decimal(str(write_raw)) if write_raw else Decimal("0")
    except Exception:
        write_per_token = Decimal("0")
    return ModelPricing(
        input_per_mtok=prompt_per_token * _PER_MTOK,
        output_per_mtok=completion_per_token * _PER_MTOK,
        cached_read_per_mtok=cached_per_token * _PER_MTOK,
        cache_creation_per_mtok=write_per_token * _PER_MTOK,
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
    from metis.core.canonical.content import (
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
    from metis.core.adapters.errors import error_for_class

    status = exc.status_code
    body: dict | None = None
    try:
        body = exc.response.json() if exc.response is not None else None
    except Exception:
        body = None
    classification = _classify_openai_response(status, body)
    msg = _provider_message(body) or str(exc)
    retry_after = _retry_after_seconds(exc)
    # Log the full upstream body separately from the surfaced message so
    # users have a place to dig when the composed error isn't enough.
    # WARN level so it shows up in default logging but doesn't drown out
    # normal traffic; the file destination is configured by the CLI/server
    # runtime per `METIS_LOG_FILE`.
    logger.warning(
        "openrouter adapter error: status=%d request_id=%s composed=%r body=%r",
        status,
        request_id,
        msg,
        body,
    )
    return error_for_class(
        classification,
        f"openrouter {status}: {msg}",
        provider_status=status,
        provider_message=msg,
        request_id=request_id,
        retry_after_seconds=retry_after,
    )


def _provider_message(body: dict | None) -> str:
    """Compose the most informative error message we can from OpenRouter's body.

    OpenRouter is an aggregator: when an upstream inference provider rejects
    the request, OpenRouter returns a 400 with a generic top-level message
    (typically ``"Provider returned error"``) and stashes the *real* reason
    in ``error.metadata``. The shape varies but commonly looks like::

        {
          "error": {
            "message": "Provider returned error",
            "code": 400,
            "metadata": {
              "raw": "{\\"error\\":{\\"message\\":\\"Function calling not supported\\",...}}",
              "provider_name": "Fireworks"
            }
          }
        }

    Without surfacing ``metadata.raw``, users see ``"Provider returned error"``
    and have no idea which upstream rejected them or why. This helper
    composes a message like::

        Provider returned error (Fireworks: Function calling not supported)

    so the failure mode is diagnostic out of the box.

    Backward compatibility: when there's no ``metadata`` (a direct
    OpenRouter-level error, not an upstream passthrough), behavior is
    identical to the prior implementation — just returns ``error.message``.
    """
    if not body or not isinstance(body, dict):
        return ""
    err = body.get("error")
    if not isinstance(err, dict):
        return ""
    base = err.get("message", "") or ""
    metadata = err.get("metadata")
    if not isinstance(metadata, dict):
        return base

    provider_name = metadata.get("provider_name") or ""
    upstream_msg = _extract_raw_upstream_message(metadata.get("raw"))
    request_id = _extract_raw_request_id(metadata.get("raw"))

    # Glue the parts: prefer "(Provider: message [req: ...])", degrade
    # gracefully when one or more pieces are missing.
    body_parts = []
    if upstream_msg:
        body_parts.append(upstream_msg)
    if request_id:
        body_parts.append(f"[req: {request_id}]")
    body = " ".join(body_parts)

    if provider_name and body:
        suffix = f"({provider_name}: {body})"
    elif provider_name:
        suffix = f"(via {provider_name})"
    elif body:
        suffix = f"(upstream: {body})"
    else:
        return base

    return f"{base} {suffix}" if base else suffix


def _extract_raw_upstream_message(raw: object) -> str:
    """Extract the upstream provider's actual error message from
    ``error.metadata.raw``.

    OpenRouter stores this in inconsistent forms:
    - A dict with ``{"error": {"message": ...}}`` (most providers, normalized).
    - A JSON-encoded **string** of that shape (some providers, opaque pass-through).
    - A plain string (rare; legacy or stringly-typed providers).

    Returns the most specific message we can find, or an empty string.
    """
    if raw is None:
        return ""
    if isinstance(raw, dict):
        return _message_from_error_dict(raw)
    if isinstance(raw, str):
        # Try JSON-decoding; fall back to the raw string if it isn't JSON.
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
        if isinstance(parsed, dict):
            extracted = _message_from_error_dict(parsed)
            return extracted or raw
        return raw
    return str(raw)


def _extract_raw_request_id(raw: object) -> str:
    """Pull a request id from an upstream provider body. Used in support
    tickets — if a user reports an error, the request id lets us (or the
    upstream provider) trace it.

    Field name varies by provider: `request_id` (AtlasCloud, some Alibaba),
    `requestId` (camelCase variants), `id` (OpenAI-shape error.id), or
    `x-request-id` (header-style payload). Empty string if none found.
    """
    if raw is None:
        return ""
    if isinstance(raw, dict):
        return _request_id_from_dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return ""
        if isinstance(parsed, dict):
            return _request_id_from_dict(parsed)
    return ""


def _request_id_from_dict(d: dict) -> str:
    candidate_keys = ("request_id", "requestId", "x-request-id", "id")
    err = d.get("error")
    if isinstance(err, dict):
        for key in candidate_keys:
            value = err.get(key)
            if value:
                return str(value)
    for key in candidate_keys:
        value = d.get(key)
        if value:
            return str(value)
    return ""


def _message_from_error_dict(d: dict) -> str:
    """Pull the human-facing error string out of an upstream provider's body.

    Upstream OpenAI-shaped providers nest under ``error.message`` (OpenAI,
    Anthropic, Fireworks, Together). Some Asian providers (e.g. AtlasCloud,
    some Alibaba endpoints) use ``msg`` at the top level. FastAPI-style
    services use ``detail``. Try each in priority order at both nesting
    levels and return the first non-empty hit.

    Returns ``""`` when nothing matches — the caller falls back to the raw
    body so we never silently drop a useful error.
    """
    candidate_keys = ("message", "msg", "detail", "description")
    err = d.get("error")
    if isinstance(err, dict):
        for key in candidate_keys:
            value = err.get(key)
            if value:
                return str(value)
    for key in candidate_keys:
        value = d.get(key)
        if value:
            return str(value)
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
