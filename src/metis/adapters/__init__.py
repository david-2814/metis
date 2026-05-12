"""Provider adapters.

See docs/specs/provider-adapter-contract.md.
"""

from metis.adapters.errors import (
    AdapterError,
    AuthError,
    CancelledError,
    ContextOverflowError,
    ErrorClass,
    InvalidRequestError,
    NetworkError,
    RateLimitError,
    ServerError,
)
from metis.adapters.protocol import (
    CanonicalRequest,
    CanonicalResponse,
    ProviderAdapter,
    StopReason,
    TokenUsage,
)
from metis.adapters.retry import RetryPolicy
from metis.adapters.tool_id_map import ToolIdMap

__all__ = [
    "AdapterError",
    "AuthError",
    "CancelledError",
    "CanonicalRequest",
    "CanonicalResponse",
    "ContextOverflowError",
    "ErrorClass",
    "InvalidRequestError",
    "NetworkError",
    "ProviderAdapter",
    "RateLimitError",
    "RetryPolicy",
    "ServerError",
    "StopReason",
    "TokenUsage",
    "ToolIdMap",
]
