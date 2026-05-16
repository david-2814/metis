"""API-key authentication provider.

Duplicates the validation/logging boilerplate from auth/password.py and
auth/oauth.py. Refactor target.
"""

from __future__ import annotations

_API_KEYS = {
    "ak_alice_live_001": "alice",
    "ak_bob_live_002": "bob",
}


class AuthResult:
    def __init__(self, success: bool, user_id: str | None = None, error: str | None = None) -> None:
        self.success = success
        self.user_id = user_id
        self.error = error

    def __repr__(self) -> str:
        return f"AuthResult(success={self.success}, user_id={self.user_id!r}, error={self.error!r})"


class ApiKeyAuth:
    """Authenticate by long-lived API key."""

    name = "apikey"

    def authenticate(self, credentials: dict) -> AuthResult:
        # Validation boilerplate (duplicated across providers).
        if not isinstance(credentials, dict):
            self._log("rejected: credentials not a dict")
            return AuthResult(success=False, error="invalid_credentials_shape")
        if "api_key" not in credentials:
            self._log("rejected: missing api_key")
            return AuthResult(success=False, error="missing_fields")

        api_key = credentials["api_key"]
        if not isinstance(api_key, str):
            self._log("rejected: api_key not a string")
            return AuthResult(success=False, error="invalid_credentials_shape")

        # Provider-specific check.
        if api_key not in _API_KEYS:
            self._log(f"rejected: unknown api_key {api_key!r}")
            return AuthResult(success=False, error="unknown_api_key")

        user_id = _API_KEYS[api_key]
        self._log(f"accepted: {user_id!r}")
        return AuthResult(success=True, user_id=user_id)

    def _log(self, msg: str) -> None:
        # Stub logger (duplicated across providers).
        print(f"[{self.name}] {msg}")
