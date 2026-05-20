"""Known LLM provider registry table (spec §6.2).

Adding a new provider is one row here. The resolver consults this table
both for the env-var mapping (`ANTHROPIC_API_KEY` etc.) and for
`metis auth test` validation pings.
"""

from __future__ import annotations

from metis.core.credentials.protocol import ProviderSpec

# Map: canonical provider name → ProviderSpec.
#
# The validate_endpoint tuples encode the cheapest reasonable identity
# probe for each provider:
#   - Anthropic: POST messages with 1 max_token on haiku (~$0.000001 per call;
#     the only paid validation in this table; see spec §9 Q2)
#   - OpenAI:    GET /v1/models (free; lists available models for the key)
#   - OpenRouter: GET /api/v1/auth/key (free; returns key metadata)
KNOWN_PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        env_var="ANTHROPIC_API_KEY",
        validate_endpoint=(
            "POST",
            "https://api.anthropic.com/v1/messages",
            {
                "model": "claude-haiku-4-5",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "."}],
            },
        ),
        auth_header_name="x-api-key",
        auth_header_value_template="{key}",
        extra_headers={
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    ),
    "openai": ProviderSpec(
        env_var="OPENAI_API_KEY",
        validate_endpoint=("GET", "https://api.openai.com/v1/models", None),
        auth_header_name="Authorization",
        auth_header_value_template="Bearer {key}",
    ),
    "openrouter": ProviderSpec(
        env_var="OPENROUTER_API_KEY",
        validate_endpoint=("GET", "https://openrouter.ai/api/v1/auth/key", None),
        auth_header_name="Authorization",
        auth_header_value_template="Bearer {key}",
    ),
}


def provider_names() -> list[str]:
    """Stable iteration order for the CLI surface."""
    return list(KNOWN_PROVIDERS.keys())
