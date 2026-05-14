"""HTTP endpoint modules for the Metis gateway.

Each module owns one inbound-shape surface:

- `anthropic` — `POST /v1/messages` (Anthropic Messages API, sync + SSE).
- (OpenAI inbound stays in `metis_gateway.app` / `translators.py` for now.)
"""
