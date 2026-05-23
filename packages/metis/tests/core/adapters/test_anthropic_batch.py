"""Tests for the Anthropic batch submission path (provider-adapter §4.6).

These tests exercise the three new adapter methods (`submit_batch`,
`poll_batch`, `fetch_batch`) against a mocked SDK client. The
`_FakeBatchesClient` below stands in for `anthropic.AsyncAnthropic`'s
`messages.batches` resource — same shape, same method names, same return
field names, so the adapter under test is byte-identical to what it'd
see against the real SDK.

The "cassette-driven" framing in the dispatch doc refers to fixture
JSON; this repo's existing convention (see `test_anthropic_adapter.py`)
is to mock the SDK client with `SimpleNamespace` factories, which serves
the same purpose with less ceremony and no out-of-band fixture files.
"""

from __future__ import annotations

import asyncio
import datetime
from decimal import Decimal
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from metis.core.adapters.anthropic import AnthropicAdapter
from metis.core.adapters.errors import (
    AdapterError,
    AuthError,
    ErrorClass,
    InvalidRequestError,
    ServerError,
)
from metis.core.adapters.protocol import CanonicalRequest, CanonicalResponse, StopReason
from metis.core.adapters.tool_id_map import ToolIdMap
from metis.core.canonical.batch import BatchError, BatchHandle
from metis.core.canonical.content import TextBlock
from metis.core.canonical.messages import Message, MessageMetadata, Role
from metis.core.pricing.table import DEFAULT_PRICE_TABLE

# ---------------------------------------------------------------------------
# SDK stubs
# ---------------------------------------------------------------------------


def _user_request(text: str = "hi", *, request_id: str = "req_1") -> CanonicalRequest:
    return CanonicalRequest(
        request_id=request_id,
        messages=[
            Message(
                id="01HZ",
                session_id="s",
                role=Role.USER,
                content=[TextBlock(text=text)],
                created_at=datetime.datetime.now(datetime.UTC),
                metadata=MessageMetadata(),
            )
        ],
        tools=[],
        system_prompt=None,
        model="anthropic:claude-haiku-4-5",
        max_output_tokens=128,
        tool_id_map=ToolIdMap(),
    )


def _three_requests() -> list[CanonicalRequest]:
    return [
        _user_request("first", request_id="req_a"),
        _user_request("second", request_id="req_b"),
        _user_request("third", request_id="req_c"),
    ]


class _FakeBatch:
    """Stand-in for `anthropic.types.messages.MessageBatch`."""

    def __init__(
        self,
        *,
        batch_id: str = "batch_xyz",
        processing_status: str = "in_progress",
        succeeded: int = 0,
        errored: int = 0,
        expired: int = 0,
        canceled: int = 0,
        processing: int = 0,
    ) -> None:
        self.id = batch_id
        self.processing_status = processing_status
        self.request_counts = SimpleNamespace(
            processing=processing,
            succeeded=succeeded,
            errored=errored,
            canceled=canceled,
            expired=expired,
        )
        self.type = "message_batch"
        self.archived_at = None
        self.cancel_initiated_at = None
        self.created_at = "2026-05-22T00:00:00Z"
        self.ended_at = None
        self.expires_at = "2026-05-23T00:00:00Z"
        self.results_url = None


class _FakeBatchUsage:
    def __init__(self, input_tokens=100, output_tokens=50, cached=0, cache_creation=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cached
        self.cache_creation_input_tokens = cache_creation


def _succeeded_row(custom_id: str, *, text: str = "ok", model: str = "claude-haiku-4-5"):
    """Build a fake `MessageBatchIndividualResponse` for a succeeded entry."""
    message = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=_FakeBatchUsage(input_tokens=200, output_tokens=80),
        model=model,
    )
    result = SimpleNamespace(type="succeeded", message=message)
    return SimpleNamespace(custom_id=custom_id, result=result)


def _errored_row(custom_id: str, *, err_type: str = "invalid_request_error", msg: str = "bad"):
    inner = SimpleNamespace(type=err_type, message=msg)
    result = SimpleNamespace(type="errored", error=SimpleNamespace(type=err_type, error=inner))
    return SimpleNamespace(custom_id=custom_id, result=result)


def _expired_row(custom_id: str):
    result = SimpleNamespace(type="expired")
    return SimpleNamespace(custom_id=custom_id, result=result)


def _canceled_row(custom_id: str):
    result = SimpleNamespace(type="canceled")
    return SimpleNamespace(custom_id=custom_id, result=result)


class _AsyncRowIterator:
    """Async iterator over a fixed list of rows (matches SDK results stream)."""

    def __init__(self, rows: list) -> None:
        self._rows = list(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._rows:
            raise StopAsyncIteration
        return self._rows.pop(0)


class _FakeBatchesClient:
    """Stand-in for `anthropic.AsyncAnthropic.messages.batches`."""

    def __init__(
        self,
        *,
        create_return: _FakeBatch | None = None,
        retrieve_returns: list[_FakeBatch] | None = None,
        results_rows: list | None = None,
        create_error: Exception | None = None,
        retrieve_error: Exception | None = None,
        results_error: Exception | None = None,
    ) -> None:
        self._create_return = create_return or _FakeBatch()
        self._retrieve_returns = list(retrieve_returns or [self._create_return])
        self._results_rows = results_rows or []
        self._create_error = create_error
        self._retrieve_error = retrieve_error
        self._results_error = results_error

        self.create_calls: list[dict] = []
        self.retrieve_calls: list[str] = []
        self.results_calls: list[str] = []

    async def create(self, *, requests, **_):
        self.create_calls.append({"requests": list(requests)})
        if self._create_error is not None:
            raise self._create_error
        return self._create_return

    async def retrieve(self, batch_id, **_):
        self.retrieve_calls.append(batch_id)
        if self._retrieve_error is not None:
            raise self._retrieve_error
        if self._retrieve_returns:
            return self._retrieve_returns.pop(0)
        # Reuse the create-return for stability if not enough explicit returns.
        return self._create_return

    async def results(self, batch_id, **_):
        self.results_calls.append(batch_id)
        if self._results_error is not None:
            raise self._results_error
        return _AsyncRowIterator(self._results_rows)


class _FakeMessagesClient:
    def __init__(self, batches: _FakeBatchesClient | None = None) -> None:
        self.batches = batches or _FakeBatchesClient()


class _FakeClient:
    def __init__(self, messages: _FakeMessagesClient | None = None) -> None:
        self.messages = messages or _FakeMessagesClient()
        self.closed = False

    async def close(self):
        self.closed = True


def _make_status_error(status: int, body: dict | None = None):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages/batches")
    response = httpx.Response(
        status_code=status,
        json=body or {},
        request=request,
    )
    return anthropic.APIStatusError(
        message=f"status {status}",
        response=response,
        body=body,
    )


# ---------------------------------------------------------------------------
# Capability flag
# ---------------------------------------------------------------------------


def test_claude_4x_models_declare_batch_support():
    """At least one anthropic:* row declares `supports_batch_api=True`
    (acceptance criterion). All three Claude 4.x models share the same
    caps row in v1."""
    adapter = AnthropicAdapter(client=_FakeClient())
    for model in (
        "anthropic:claude-haiku-4-5",
        "anthropic:claude-sonnet-4-6",
        "anthropic:claude-opus-4-7",
    ):
        caps = adapter.capabilities_for(model)
        assert caps.supports_batch_api is True, f"{model} should declare batch support"


# ---------------------------------------------------------------------------
# submit_batch
# ---------------------------------------------------------------------------


async def test_submit_batch_returns_handle_with_input_custom_ids():
    fake_batches = _FakeBatchesClient(create_return=_FakeBatch(batch_id="batch_001"))
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))

    requests = _three_requests()
    handle = await adapter.submit_batch(requests)

    assert isinstance(handle, BatchHandle)
    assert handle.provider == "anthropic"
    assert handle.batch_id == "batch_001"
    assert handle.request_count == 3
    assert handle.custom_ids == ("req_a", "req_b", "req_c")
    assert handle.submitted_at_ms > 0


async def test_submit_batch_translates_each_request_to_messages_create_kwargs():
    fake_batches = _FakeBatchesClient()
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))

    requests = _three_requests()
    # Set a system prompt + temperature on the first to confirm those
    # flow through the shared `_assemble_messages_create_kwargs` helper.
    requests[0].system_prompt = "you are helpful"
    requests[0].temperature = 0.7
    await adapter.submit_batch(requests)

    # Each `requests=` row carries the `{custom_id, params}` shape Anthropic
    # documents at §4.6.3.
    wire_requests = fake_batches.create_calls[0]["requests"]
    assert len(wire_requests) == 3
    for i, wire in enumerate(wire_requests):
        assert wire["custom_id"] == requests[i].request_id
        params = wire["params"]
        # Wire model stripped of canonical prefix (`anthropic:` prefix is
        # purely a Metis-side namespace; Anthropic accepts the suffix).
        assert params["model"] == "claude-haiku-4-5"
        assert params["max_tokens"] == 128
        # `stream` must not leak into the batch params — batch is not a
        # streaming surface.
        assert "stream" not in params
    # System prompt + temperature on the first request flowed through.
    first_params = wire_requests[0]["params"]
    assert first_params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first_params["temperature"] == 0.7


async def test_submit_batch_rejects_empty_input():
    adapter = AnthropicAdapter(client=_FakeClient())
    with pytest.raises(InvalidRequestError):
        await adapter.submit_batch([])


async def test_submit_batch_rejects_duplicate_custom_ids():
    adapter = AnthropicAdapter(client=_FakeClient())
    requests = [
        _user_request("a", request_id="dup"),
        _user_request("b", request_id="dup"),
    ]
    with pytest.raises(InvalidRequestError):
        await adapter.submit_batch(requests)


async def test_submit_batch_translates_upstream_4xx_to_adapter_error():
    fake_batches = _FakeBatchesClient(
        create_error=_make_status_error(401, {"error": {"type": "authentication_error"}})
    )
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))
    with pytest.raises(AuthError):
        await adapter.submit_batch(_three_requests())


# ---------------------------------------------------------------------------
# poll_batch
# ---------------------------------------------------------------------------


def _handle_for(batch_id: str = "batch_xyz", request_count: int = 3) -> BatchHandle:
    return BatchHandle(
        provider="anthropic",
        batch_id=batch_id,
        submitted_at_ms=0,
        request_count=request_count,
        custom_ids=tuple(f"req_{i}" for i in range(request_count)),
    )


async def test_poll_batch_reports_in_progress():
    fake_batches = _FakeBatchesClient(
        retrieve_returns=[_FakeBatch(processing_status="in_progress", processing=3)]
    )
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))
    status = await adapter.poll_batch(_handle_for())
    assert status == "in_progress"


async def test_poll_batch_reports_completed_with_succeeded_only():
    fake_batches = _FakeBatchesClient(
        retrieve_returns=[_FakeBatch(processing_status="ended", succeeded=3)]
    )
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))
    status = await adapter.poll_batch(_handle_for())
    assert status == "completed"


async def test_poll_batch_reports_completed_with_mixed_results():
    fake_batches = _FakeBatchesClient(
        retrieve_returns=[_FakeBatch(processing_status="ended", succeeded=2, errored=1)]
    )
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))
    status = await adapter.poll_batch(_handle_for())
    # Mixed success/error → completed; per-row failures surface inside fetch.
    assert status == "completed"


async def test_poll_batch_reports_expired_when_all_expired():
    fake_batches = _FakeBatchesClient(
        retrieve_returns=[_FakeBatch(processing_status="ended", expired=3)]
    )
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))
    status = await adapter.poll_batch(_handle_for())
    assert status == "expired"


async def test_poll_batch_reports_failed_when_all_errored_no_successes():
    fake_batches = _FakeBatchesClient(
        retrieve_returns=[_FakeBatch(processing_status="ended", errored=3)]
    )
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))
    status = await adapter.poll_batch(_handle_for())
    assert status == "failed"


# ---------------------------------------------------------------------------
# fetch_batch
# ---------------------------------------------------------------------------


async def test_fetch_batch_round_trip_preserves_order_and_custom_ids():
    """Acceptance criterion: submit 3 requests, poll until completed,
    fetch — result list is same length, same order, custom_ids preserved.
    """
    fake_batches = _FakeBatchesClient(
        create_return=_FakeBatch(batch_id="batch_001"),
        retrieve_returns=[
            _FakeBatch(batch_id="batch_001", processing_status="in_progress", processing=3),
            _FakeBatch(batch_id="batch_001", processing_status="ended", succeeded=3),
            _FakeBatch(batch_id="batch_001", processing_status="ended", succeeded=3),
        ],
        # Deliberately reverse upstream order to verify the adapter
        # reorders by handle.custom_ids.
        results_rows=[
            _succeeded_row("req_c", text="third"),
            _succeeded_row("req_a", text="first"),
            _succeeded_row("req_b", text="second"),
        ],
    )
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))

    requests = _three_requests()
    handle = await adapter.submit_batch(requests)

    # Poll until completed (one in_progress → one ended).
    assert await adapter.poll_batch(handle) == "in_progress"
    assert await adapter.poll_batch(handle) == "completed"

    results = await adapter.fetch_batch(handle)

    assert len(results) == 3
    assert [r.request_id for r in results] == ["req_a", "req_b", "req_c"]
    assert all(isinstance(r, CanonicalResponse) for r in results)
    assert all(r.provider == "anthropic" for r in results)


async def test_fetch_batch_stamps_pricing_mode_batch_on_successful_results():
    fake_batches = _FakeBatchesClient(
        create_return=_FakeBatch(batch_id="b", processing_status="ended", succeeded=2),
        retrieve_returns=[_FakeBatch(batch_id="b", processing_status="ended", succeeded=2)],
        results_rows=[_succeeded_row("x"), _succeeded_row("y")],
    )
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))
    handle = BatchHandle(
        provider="anthropic",
        batch_id="b",
        submitted_at_ms=0,
        request_count=2,
        custom_ids=("x", "y"),
    )
    results = await adapter.fetch_batch(handle)
    for r in results:
        assert isinstance(r, CanonicalResponse)
        assert r.usage.pricing_mode == "batch"


async def test_fetch_batch_cost_matches_batch_rates_when_present():
    """`Usage.cost_usd` matches `ModelPricing.batch_rates` (50% of sync)."""
    # Use the default price table's haiku-4-5 row: sync rates 1.00/5.00,
    # batch rates 0.50/2.50. A succeeded row with 200 in / 80 out tokens
    # should cost 200*0.50/1e6 + 80*2.50/1e6 = 0.0001 + 0.0002 = 0.0003.
    fake_batches = _FakeBatchesClient(
        retrieve_returns=[_FakeBatch(processing_status="ended", succeeded=1)],
        results_rows=[_succeeded_row("only", model="claude-haiku-4-5")],
    )
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))
    handle = BatchHandle(
        provider="anthropic",
        batch_id="b",
        submitted_at_ms=0,
        request_count=1,
        custom_ids=("only",),
    )
    results = await adapter.fetch_batch(handle)
    assert len(results) == 1
    response = results[0]
    assert isinstance(response, CanonicalResponse)
    cost = DEFAULT_PRICE_TABLE.compute_cost("anthropic:claude-haiku-4-5", response.usage)
    # Batch rates: 200 input @ 0.50/M + 80 output @ 2.50/M = 0.0001 + 0.0002
    assert cost == Decimal("0.0003")
    # Sanity: sync rates would have been 0.0006 (2x higher).
    sync_usage = type(response.usage)(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cached_input_tokens=response.usage.cached_input_tokens,
        cache_creation_input_tokens=response.usage.cache_creation_input_tokens,
        pricing_mode="sync",
    )
    assert DEFAULT_PRICE_TABLE.compute_cost("anthropic:claude-haiku-4-5", sync_usage) == Decimal(
        "0.0006"
    )


async def test_fetch_batch_warns_and_falls_back_when_batch_rates_missing(caplog):
    """When `ModelPricing.batch_rates is None`, the price table logs WARN
    and falls back to sync rates (correctness preserved, savings lost)."""
    from metis.core.adapters.protocol import TokenUsage
    from metis.core.pricing.table import ModelPricing, PriceTable

    table = PriceTable(
        version="t",
        models={
            "anthropic:claude-test": ModelPricing(
                input_per_mtok=Decimal("1.00"),
                output_per_mtok=Decimal("5.00"),
                # No batch_rates.
            )
        },
    )
    usage = TokenUsage(input_tokens=200, output_tokens=80, pricing_mode="batch")
    with caplog.at_level("WARNING"):
        cost = table.compute_cost("anthropic:claude-test", usage)
    # Sync rates: 200*1.00/1e6 + 80*5.00/1e6 = 0.0002 + 0.0004 = 0.0006.
    assert cost == Decimal("0.0006")
    assert any("batch_rates is None" in rec.message for rec in caplog.records)
    # Second call doesn't log again (per-model dedup).
    caplog.clear()
    with caplog.at_level("WARNING"):
        table.compute_cost("anthropic:claude-test", usage)
    assert not any("batch_rates is None" in rec.message for rec in caplog.records)


async def test_fetch_batch_expired_row_surfaces_as_batch_error():
    """Expired entries surface as `BatchError(SERVER_ERROR, retryable=True)`
    per §4.6.6 (the spec names it PROVIDER_TRANSIENT; the closed
    ErrorClass uses SERVER_ERROR for the same semantics — see batch.py)."""
    fake_batches = _FakeBatchesClient(
        retrieve_returns=[_FakeBatch(processing_status="ended", expired=2)],
        results_rows=[_expired_row("a"), _expired_row("b")],
    )
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))
    handle = BatchHandle(
        provider="anthropic",
        batch_id="b",
        submitted_at_ms=0,
        request_count=2,
        custom_ids=("a", "b"),
    )
    results = await adapter.fetch_batch(handle)
    assert len(results) == 2
    for r in results:
        assert isinstance(r, BatchError)
        assert r.error_class == ErrorClass.SERVER_ERROR
        assert r.retryable is True


async def test_fetch_batch_errored_row_classifies_invalid_request():
    fake_batches = _FakeBatchesClient(
        retrieve_returns=[_FakeBatch(processing_status="ended", errored=1, succeeded=1)],
        results_rows=[
            _succeeded_row("ok"),
            _errored_row("bad", err_type="invalid_request_error", msg="bad params"),
        ],
    )
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))
    handle = BatchHandle(
        provider="anthropic",
        batch_id="b",
        submitted_at_ms=0,
        request_count=2,
        custom_ids=("ok", "bad"),
    )
    results = await adapter.fetch_batch(handle)
    assert isinstance(results[0], CanonicalResponse)
    assert isinstance(results[1], BatchError)
    assert results[1].error_class == ErrorClass.INVALID_REQUEST
    assert results[1].retryable is False
    assert "bad params" in results[1].error_message


async def test_fetch_batch_errored_row_classifies_rate_limit_as_retryable():
    fake_batches = _FakeBatchesClient(
        retrieve_returns=[_FakeBatch(processing_status="ended", errored=1)],
        results_rows=[_errored_row("a", err_type="rate_limit_error", msg="slow down")],
    )
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))
    handle = BatchHandle(
        provider="anthropic",
        batch_id="b",
        submitted_at_ms=0,
        request_count=1,
        custom_ids=("a",),
    )
    results = await adapter.fetch_batch(handle)
    assert isinstance(results[0], BatchError)
    assert results[0].error_class == ErrorClass.RATE_LIMIT
    assert results[0].retryable is True


async def test_fetch_batch_missing_row_surfaces_as_transient_batch_error():
    """If upstream omits a custom_id from the results stream, the
    adapter fills in a `BatchError(SERVER_ERROR, retryable=True)` for
    that slot so the caller's list is always same-length, same-order."""
    fake_batches = _FakeBatchesClient(
        retrieve_returns=[_FakeBatch(processing_status="ended", succeeded=1)],
        # Only one row, but the handle expects three.
        results_rows=[_succeeded_row("middle")],
    )
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))
    handle = BatchHandle(
        provider="anthropic",
        batch_id="b",
        submitted_at_ms=0,
        request_count=3,
        custom_ids=("missing_a", "middle", "missing_c"),
    )
    results = await adapter.fetch_batch(handle)
    assert len(results) == 3
    assert isinstance(results[0], BatchError) and results[0].custom_id == "missing_a"
    assert isinstance(results[1], CanonicalResponse) and results[1].request_id == "middle"
    assert isinstance(results[2], BatchError) and results[2].custom_id == "missing_c"
    assert results[0].error_class == ErrorClass.SERVER_ERROR
    assert results[0].retryable is True


async def test_fetch_batch_raises_when_still_in_progress():
    fake_batches = _FakeBatchesClient(
        retrieve_returns=[_FakeBatch(processing_status="in_progress", processing=3)]
    )
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))
    with pytest.raises(ServerError):
        await adapter.fetch_batch(_handle_for())


async def test_fetch_batch_raises_when_results_stream_empty():
    """A completed batch that returns zero rows is a batch-level failure
    (vs a per-row failure) and raises `AdapterError` per §4.6.6."""
    fake_batches = _FakeBatchesClient(
        retrieve_returns=[_FakeBatch(processing_status="ended", succeeded=3)],
        results_rows=[],
    )
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))
    with pytest.raises(ServerError):
        await adapter.fetch_batch(_handle_for())


async def test_fetch_batch_rejects_handle_for_other_provider():
    """Single representative test for provider-mismatch guarding;
    `submit_batch` and `poll_batch` use the same `_ensure_provider_matches`
    helper so testing one method covers all three."""
    adapter = AnthropicAdapter(client=_FakeClient())
    foreign_handle = BatchHandle(
        provider="openai",
        batch_id="b",
        submitted_at_ms=0,
        request_count=1,
        custom_ids=("x",),
    )
    with pytest.raises(InvalidRequestError):
        await adapter.fetch_batch(foreign_handle)
    # Same guard fires on poll_batch.
    with pytest.raises(InvalidRequestError):
        await adapter.poll_batch(foreign_handle)


async def test_fetch_batch_succeeded_row_preserves_text_content():
    fake_batches = _FakeBatchesClient(
        retrieve_returns=[_FakeBatch(processing_status="ended", succeeded=1)],
        results_rows=[_succeeded_row("only", text="hello batch")],
    )
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))
    handle = BatchHandle(
        provider="anthropic",
        batch_id="b",
        submitted_at_ms=0,
        request_count=1,
        custom_ids=("only",),
    )
    results = await adapter.fetch_batch(handle)
    response = results[0]
    assert isinstance(response, CanonicalResponse)
    assert len(response.content) == 1
    assert isinstance(response.content[0], TextBlock)
    assert response.content[0].text == "hello batch"
    assert response.stop_reason == StopReason.END_TURN


# ---------------------------------------------------------------------------
# Default-implementation guarantees on adapters that don't override
# ---------------------------------------------------------------------------


async def test_default_submit_batch_raises_not_implemented_on_dummy_adapter():
    """The base Protocol's default implementation raises
    NotImplementedError so existing adapters that don't override (OpenAI,
    OpenRouter) still satisfy the structural shape but fail loudly when
    called."""

    from metis.core.adapters.protocol import ProviderAdapter

    class _Dummy(ProviderAdapter):
        name = "dummy"

        async def complete(self, request):  # type: ignore[override]
            raise NotImplementedError

        def stream(self, request):  # type: ignore[override]
            raise NotImplementedError

        def estimate_input_tokens(self, messages, tools, system_prompt):  # type: ignore[override]
            return 0

        async def cancel(self, request_id):  # type: ignore[override]
            return False

        async def close(self):  # type: ignore[override]
            pass

        def capabilities_for(self, model):  # type: ignore[override]
            raise NotImplementedError

    dummy = _Dummy()
    with pytest.raises(NotImplementedError):
        await dummy.submit_batch([])
    with pytest.raises(NotImplementedError):
        await dummy.poll_batch(_handle_for())
    with pytest.raises(NotImplementedError):
        await dummy.fetch_batch(_handle_for())


# ---------------------------------------------------------------------------
# BatchHandle msgspec round-trip
# ---------------------------------------------------------------------------


def test_batch_handle_msgspec_roundtrip():
    import msgspec

    handle = BatchHandle(
        provider="anthropic",
        batch_id="batch_42",
        submitted_at_ms=1716_345_000_000,
        request_count=3,
        custom_ids=("a", "b", "c"),
    )
    encoded = msgspec.json.encode(handle)
    decoded = msgspec.json.decode(encoded, type=BatchHandle)
    assert decoded == handle


def test_batch_error_msgspec_roundtrip():
    import msgspec

    err = BatchError(
        custom_id="x",
        error_class=ErrorClass.SERVER_ERROR,
        error_message="timed out",
        retryable=True,
    )
    encoded = msgspec.json.encode(err)
    decoded = msgspec.json.decode(encoded, type=BatchError)
    assert decoded == err


# ---------------------------------------------------------------------------
# Cross-method integration: end-to-end "submit → poll → fetch" cycle
# ---------------------------------------------------------------------------


async def test_full_cycle_submit_poll_fetch_three_requests():
    """End-to-end submit → poll-until-completed → fetch on 3 requests.

    Validates the §4.6 acceptance bar: a 3-request batch can be
    round-tripped through the adapter and the returned list preserves
    length and order and stamps `pricing_mode='batch'` on every
    successful result.
    """
    fake_batches = _FakeBatchesClient(
        create_return=_FakeBatch(batch_id="batch_e2e"),
        retrieve_returns=[
            _FakeBatch(batch_id="batch_e2e", processing_status="in_progress", processing=3),
            _FakeBatch(batch_id="batch_e2e", processing_status="in_progress", succeeded=1),
            _FakeBatch(batch_id="batch_e2e", processing_status="ended", succeeded=3),
            _FakeBatch(batch_id="batch_e2e", processing_status="ended", succeeded=3),
        ],
        results_rows=[
            _succeeded_row("req_a", text="A"),
            _succeeded_row("req_b", text="B"),
            _succeeded_row("req_c", text="C"),
        ],
    )
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))

    requests = _three_requests()
    handle = await adapter.submit_batch(requests)
    assert handle.request_count == 3

    # Poll until status is terminal.
    while True:
        status = await adapter.poll_batch(handle)
        if status not in ("queued", "in_progress"):
            break
        await asyncio.sleep(0)  # cooperative yield in case of long loops
    assert status == "completed"

    results = await adapter.fetch_batch(handle)
    assert [r.request_id for r in results] == ["req_a", "req_b", "req_c"]
    assert [r.content[0].text for r in results] == ["A", "B", "C"]
    for r in results:
        assert isinstance(r, CanonicalResponse)
        assert r.usage.pricing_mode == "batch"
        # Cost via the default price table uses batch rates (50% of sync).
        cost = DEFAULT_PRICE_TABLE.compute_cost(r.model or "anthropic:claude-haiku-4-5", r.usage)
        # Same input/output token shape across rows.
        assert cost > 0


# ---------------------------------------------------------------------------
# Wire mapping detail: tool_id_map + cache breakpoints don't break batch
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Error translation on the batch endpoint
# ---------------------------------------------------------------------------


async def test_batch_methods_translate_transport_errors_to_network_error():
    """Both `submit_batch` and `poll_batch` route transport exceptions
    (httpx + the SDK's APIConnectionError) through the shared
    NetworkError translation."""
    fake_batches = _FakeBatchesClient(create_error=httpx.ReadTimeout("upstream slow"))
    adapter = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches)))
    with pytest.raises(AdapterError) as exc:
        await adapter.submit_batch(_three_requests())
    assert exc.value.error_class == ErrorClass.NETWORK

    fake_batches2 = _FakeBatchesClient(
        retrieve_error=anthropic.APIConnectionError(
            message="boom", request=httpx.Request("GET", "https://api.anthropic.com")
        )
    )
    adapter2 = AnthropicAdapter(client=_FakeClient(messages=_FakeMessagesClient(fake_batches2)))
    with pytest.raises(AdapterError) as exc:
        await adapter2.poll_batch(_handle_for())
    assert exc.value.error_class == ErrorClass.NETWORK
