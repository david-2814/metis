"""Metis transparent HTTP gateway.

See `docs/specs/gateway.md` and `docs/specs/deployment-shape.md`.

v1 vertical slice: OpenAI-shape inbound (`POST /v1/chat/completions`, sync),
per-key authentication mapping to a single workspace, routing via the existing
`metis_core.routing` engine, and trace-event emission with `gateway_key_id`
attribution. SSE streaming and Anthropic-shape inbound are follow-up agents.
"""
