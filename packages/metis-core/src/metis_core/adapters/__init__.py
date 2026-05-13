"""Provider adapters.

See docs/specs/provider-adapter-contract.md.
"""

from metis_core.adapters.errors import (
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
from metis_core.adapters.protocol import (
    CanonicalRequest,
    CanonicalResponse,
    ProviderAdapter,
    StopReason,
    TokenUsage,
)
from metis_core.adapters.retry import RetryPolicy
from metis_core.adapters.tool_id_map import ToolIdMap

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
