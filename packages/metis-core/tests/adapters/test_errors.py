"""Tests for HTTP/body error classification."""

from __future__ import annotations

import pytest
from metis_core.adapters.errors import (
    AuthError,
    ContextOverflowError,
    ErrorClass,
    InvalidRequestError,
    RateLimitError,
    ServerError,
    classify_anthropic_response,
    classify_http_status,
    error_for_class,
)

# ---- Default HTTP status mapping ----------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, ErrorClass.AUTH),
        (403, ErrorClass.AUTH),
        (408, ErrorClass.NETWORK),
        (413, ErrorClass.CONTEXT_OVERFLOW),
        (429, ErrorClass.RATE_LIMIT),
        (500, ErrorClass.SERVER_ERROR),
        (502, ErrorClass.SERVER_ERROR),
        (529, ErrorClass.SERVER_ERROR),
        (400, ErrorClass.INVALID_REQUEST),
        (404, ErrorClass.INVALID_REQUEST),
    ],
)
def test_classify_http_status_defaults(status, expected):
    assert classify_http_status(status) == expected


# ---- Anthropic-body refinement ------------------------------------------


def test_overloaded_error_body_promotes_to_rate_limit():
    body = {"error": {"type": "overloaded_error", "message": "overloaded"}}
    # Anthropic uses 529 for this; the default would be SERVER_ERROR.
    assert classify_anthropic_response(529, body) == ErrorClass.RATE_LIMIT


def test_rate_limit_error_body():
    body = {"error": {"type": "rate_limit_error", "message": "rate limited"}}
    assert classify_anthropic_response(429, body) == ErrorClass.RATE_LIMIT


def test_authentication_error_body():
    body = {"error": {"type": "authentication_error", "message": "bad key"}}
    # Status alone is enough but body should confirm.
    assert classify_anthropic_response(401, body) == ErrorClass.AUTH


def test_invalid_request_with_context_message_promotes_to_overflow():
    body = {
        "error": {
            "type": "invalid_request_error",
            "message": "prompt is too long: context length exceeded",
        }
    }
    assert classify_anthropic_response(400, body) == ErrorClass.CONTEXT_OVERFLOW


def test_invalid_request_with_tokens_exceeds_message_promotes_to_overflow():
    body = {
        "error": {
            "type": "invalid_request_error",
            "message": "Requested 250000 tokens exceeds maximum",
        }
    }
    assert classify_anthropic_response(400, body) == ErrorClass.CONTEXT_OVERFLOW


def test_invalid_request_otherwise_stays_invalid():
    body = {"error": {"type": "invalid_request_error", "message": "bad shape"}}
    assert classify_anthropic_response(400, body) == ErrorClass.INVALID_REQUEST


def test_api_error_body_keeps_server_error():
    body = {"error": {"type": "api_error", "message": "internal"}}
    assert classify_anthropic_response(500, body) == ErrorClass.SERVER_ERROR


def test_missing_body_uses_default():
    assert classify_anthropic_response(429, None) == ErrorClass.RATE_LIMIT


# ---- error_for_class factory --------------------------------------------


def test_error_factory_rate_limit_carries_retry_after():
    err = error_for_class(
        ErrorClass.RATE_LIMIT,
        "rate limited",
        retry_after_seconds=12.5,
    )
    assert isinstance(err, RateLimitError)
    assert err.retry_after_seconds == 12.5
    assert err.retryable is True


def test_error_factory_returns_specific_subclasses():
    assert isinstance(error_for_class(ErrorClass.AUTH, "x"), AuthError)
    assert isinstance(error_for_class(ErrorClass.SERVER_ERROR, "x"), ServerError)
    assert isinstance(error_for_class(ErrorClass.CONTEXT_OVERFLOW, "x"), ContextOverflowError)
    assert isinstance(error_for_class(ErrorClass.INVALID_REQUEST, "x"), InvalidRequestError)


def test_server_error_is_retryable():
    err = error_for_class(ErrorClass.SERVER_ERROR, "boom")
    assert err.retryable is True


def test_auth_error_is_not_retryable():
    err = error_for_class(ErrorClass.AUTH, "no key")
    assert err.retryable is False
