"""Metis: local-first AI dev agent.

This package consolidates four logical components:

- ``metis.core``: canonical types, event bus, adapters, routing, tools,
  memory, sessions, pricing, skills, trace store, evaluator, patterns.
- ``metis.server``: HTTP/WebSocket agent server (Starlette + uvicorn).
- ``metis.gateway``: transparent OpenAI/Anthropic-shape HTTP gateway.
- ``metis.cli``: REPL, TUI, and the ``metis`` console-script entry point.
"""
