"""Password-based authentication provider.

This module duplicates structure with auth/oauth.py and auth/apikey.py:
each provider validates credentials shape, logs an attempt, returns an
AuthResult. The duplication is the refactor target.
"""

from __future__ import annotations

import hashlib


class AuthResult:
    def __init__(self, success: bool, user_id: str | None = None, error: str | None = None) -> None:
        self.success = success
        self.user_id = user_id
        self.error = error

    def __repr__(self) -> str:
        return f"AuthResult(success={self.success}, user_id={self.user_id!r}, error={self.error!r})"


_USERS = {
    "alice": hashlib.sha256(b"hunter2").hexdigest(),
    "bob": hashlib.sha256(b"correct horse battery staple").hexdigest(),
}


class PasswordAuth:
    """Authenticate by username + plaintext password (hashed before compare)."""

    name = "password"

    def authenticate(self, credentials: dict) -> AuthResult:
        # Validation boilerplate (duplicated across providers).
        if not isinstance(credentials, dict):
            self._log("rejected: credentials not a dict")
            return AuthResult(success=False, error="invalid_credentials_shape")
        if "username" not in credentials or "password" not in credentials:
            self._log("rejected: missing username/password")
            return AuthResult(success=False, error="missing_fields")

        username = credentials["username"]
        password = credentials["password"]
        if not isinstance(username, str) or not isinstance(password, str):
            self._log("rejected: username/password not strings")
            return AuthResult(success=False, error="invalid_credentials_shape")

        # Provider-specific check.
        expected = _USERS.get(username)
        if expected is None:
            self._log(f"rejected: unknown user {username!r}")
            return AuthResult(success=False, error="unknown_user")
        if hashlib.sha256(password.encode()).hexdigest() != expected:
            self._log(f"rejected: bad password for {username!r}")
            return AuthResult(success=False, error="bad_password")

        self._log(f"accepted: {username!r}")
        return AuthResult(success=True, user_id=username)

    def _log(self, msg: str) -> None:
        # Stub logger (duplicated across providers).
        print(f"[{self.name}] {msg}")
