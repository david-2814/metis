"""Minimal anthropic-python client pointed at the Metis gateway.

Requires: `pip install anthropic` and a running gateway with a valid key.
Run with: METIS_GATEWAY_TOKEN=gw_... uv run python examples/gateway/anthropic-python.py
"""

from __future__ import annotations

import os
import sys

from anthropic import Anthropic

GATEWAY_URL = os.environ.get("METIS_GATEWAY_URL", "http://127.0.0.1:8422")
TOKEN = os.environ.get("METIS_GATEWAY_TOKEN")
if not TOKEN:
    sys.exit("set METIS_GATEWAY_TOKEN to the gw_... key printed by `metis gateway issue-key`")

client = Anthropic(base_url=GATEWAY_URL, api_key=TOKEN, max_retries=0)

resp = client.messages.create(
    model="haiku",
    max_tokens=64,
    messages=[{"role": "user", "content": "Say hello in one word."}],
)

text = "".join(block.text for block in resp.content if block.type == "text")
print(text)
print(f"usage: input={resp.usage.input_tokens} output={resp.usage.output_tokens}")
