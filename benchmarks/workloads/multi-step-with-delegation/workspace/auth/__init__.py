"""Tiny authentication module with three providers.

The three provider classes (PasswordAuth, OAuthAuth, ApiKeyAuth) all expose
an `authenticate(credentials)` method with duplicated validation and
logging boilerplate. The refactor target: extract a shared `AuthProvider`
Protocol or ABC that all three implement, then collapse the duplicated
boilerplate into a single base or helper.
"""

from auth.apikey import ApiKeyAuth
from auth.oauth import OAuthAuth
from auth.password import PasswordAuth
from auth.registry import ProviderRegistry

__all__ = ["ApiKeyAuth", "OAuthAuth", "PasswordAuth", "ProviderRegistry"]
