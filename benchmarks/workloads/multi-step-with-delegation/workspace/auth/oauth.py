"""OAuth-token authentication provider.

Duplicates the validation/logging boilerplate from auth/password.py and
auth/apikey.py. Refactor target.
"""

from __future__ import annotations

_TOKEN_REGISTRY = {
    "tok_alice_001": "alice",
    "tok_bob_002": "bob",
    "tok_expired_999": None,  # token exists but is expired
}


class AuthResult:
    def __init__(self, success: bool, user_id: str | None = None, error: str | None = None) -> None:
        self.success = success
        self.user_id = user_id
        self.error = error

    def __repr__(self) -> str:
        return f"AuthResult(success={self.success}, user_id={self.user_id!r}, error={self.error!r})"


class OAuthAuth:
    """Authenticate by OAuth bearer token."""

    name = "oauth"

    def authenticate(self, credentials: dict) -> AuthResult:
        # Validation boilerplate (duplicated across providers).
        if not isinstance(credentials, dict):
            self._log("rejected: credentials not a dict")
            return AuthResult(success=False, error="invalid_credentials_shape")
        if "token" not in credentials:
            self._log("rejected: missing token")
            return AuthResult(success=False, error="missing_fields")

        token = credentials["token"]
        if not isinstance(token, str):
            self._log("rejected: token not a string")
            return AuthResult(success=False, error="invalid_credentials_shape")

        # Provider-specific check.
        if token not in _TOKEN_REGISTRY:
            self._log(f"rejected: unknown token {token!r}")
            return AuthResult(success=False, error="unknown_token")
        user_id = _TOKEN_REGISTRY[token]
        if user_id is None:
            self._log(f"rejected: expired token {token!r}")
            return AuthResult(success=False, error="expired_token")

        self._log(f"accepted: {user_id!r}")
        return AuthResult(success=True, user_id=user_id)

    def _log(self, msg: str) -> None:
        # Stub logger (duplicated across providers).
        print(f"[{self.name}] {msg}")
