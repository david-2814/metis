"""Provider adapters.

See docs/specs/provider-adapter-contract.md.
"""

from metis.core.adapters.errors import (
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
from metis.core.adapters.protocol import (
    CanonicalRequest,
    CanonicalResponse,
    ProviderAdapter,
    StopReason,
    TokenUsage,
)
from metis.core.adapters.retry import RetryPolicy
from metis.core.adapters.tool_id_map import ToolIdMap

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
