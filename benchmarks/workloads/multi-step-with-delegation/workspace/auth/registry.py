"""Provider registry: route an auth request to the right provider."""

from __future__ import annotations

from auth.apikey import ApiKeyAuth
from auth.oauth import OAuthAuth
from auth.password import PasswordAuth


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers = {
            "password": PasswordAuth(),
            "oauth": OAuthAuth(),
            "apikey": ApiKeyAuth(),
        }

    def get(self, name: str):
        if name not in self._providers:
            raise KeyError(f"unknown auth provider: {name!r}")
        return self._providers[name]

    def authenticate(self, provider_name: str, credentials: dict):
        return self.get(provider_name).authenticate(credentials)
