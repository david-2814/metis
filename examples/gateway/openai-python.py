"""Minimal openai-python client pointed at the Metis gateway.

Requires: `pip install openai` and a running gateway with a valid key.
Run with: METIS_GATEWAY_TOKEN=gw_... uv run python examples/gateway/openai-python.py
"""

from __future__ import annotations

import os
import sys

from openai import OpenAI

GATEWAY_URL = os.environ.get("METIS_GATEWAY_URL", "http://127.0.0.1:8422/v1")
TOKEN = os.environ.get("METIS_GATEWAY_TOKEN")
if not TOKEN:
    sys.exit("set METIS_GATEWAY_TOKEN to the gw_... key printed by `metis gateway issue-key`")

client = OpenAI(base_url=GATEWAY_URL, api_key=TOKEN, max_retries=0)

resp = client.chat.completions.create(
    model="haiku",
    max_tokens=64,
    messages=[{"role": "user", "content": "Say hello in one word."}],
)

print(resp.choices[0].message.content)
print(f"usage: {resp.usage.model_dump() if resp.usage else 'none'}")
