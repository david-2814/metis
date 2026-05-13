"""Real-API cross-provider continuity smoke test.

This is the headline test: one session, three providers (Anthropic → OpenAI
→ OpenRouter), with tool use in the first turn. Validates the canonical-
format claim — tool_use_id round-trips through the ToolIdMap across
provider swaps — against actual provider APIs.

Requires:
  - ANTHROPIC_API_KEY
  - OPENAI_API_KEY
  - OPENROUTER_API_KEY

Usage:
    uv run python scripts/smoke_cross_provider.py

Expected cost: ~$0.02 (three turns on cheap models).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from metis_core.adapters.anthropic import AnthropicAdapter
from metis_core.adapters.openai import OpenAIAdapter
from metis_core.adapters.openrouter import OpenRouterAdapter
from metis_core.canonical.content import ToolUseBlock
from metis_core.canonical.messages import Role
from metis_core.events.bus import EventBus
from metis_core.pricing import DEFAULT_PRICE_TABLE
from metis_core.routing import ModelRegistry, RoutingEngine
from metis_core.sessions import InMemorySessionStore, SessionManager
from metis_core.tools.builtins import register_builtins
from metis_core.tools.dispatcher import ToolDispatcher
from metis_core.trace.store import TraceStore

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _check_key(name: str) -> str:
    val = os.environ.get(name, "")
    if not val or "..." in val:
        print(f"ERROR: {name} is not set (or is still a placeholder).", file=sys.stderr)
        sys.exit(2)
    return val


async def main() -> int:
    _load_dotenv(REPO_ROOT / ".env")
    anth_key = _check_key("ANTHROPIC_API_KEY")
    oai_key = _check_key("OPENAI_API_KEY")
    or_key = _check_key("OPENROUTER_API_KEY")

    # ---- Wire up everything (mirrors runtime.setup_runtime, condensed) -----

    bus = EventBus()
    bus.start()
    db_path = REPO_ROOT / ".metis" / "smoke-cross.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    trace = TraceStore(db_path)
    trace.attach_to(bus)

    anth = AnthropicAdapter(api_key=anth_key)
    oai = OpenAIAdapter(api_key=oai_key)
    or_adapter = OpenRouterAdapter(
        api_key=or_key, app_name="metis-smoke", http_referer="https://metis.local"
    )

    registry = ModelRegistry()
    registry.register(model_id="anthropic:claude-haiku-4-5", adapter=anth, aliases=["haiku"])
    registry.register(model_id="anthropic:claude-sonnet-4-6", adapter=anth, aliases=["sonnet"])
    registry.register(model_id="openai:gpt-5-mini", adapter=oai, aliases=["mini", "gpt5-mini"])

    print("Fetching OpenRouter catalog…")
    catalog = await or_adapter.fetch_catalog()
    # Pick a cheap OpenRouter model. Llama 3.1 8B Instruct is consistently
    # available and inexpensive; fall back to any model in the catalog.
    or_model_id = next(
        (m for m in catalog.capabilities if "llama-3.1-8b-instruct" in m or "deepseek-chat" in m),
        None,
    )
    if or_model_id is None:
        # Fallback: just take any model from the catalog.
        or_model_id = sorted(catalog.capabilities.keys())[0]
    registry.register(model_id=or_model_id, adapter=or_adapter, aliases=["or-fast"])

    pricing = DEFAULT_PRICE_TABLE.with_overlay(
        overlay_version=catalog.version, overlay_models=catalog.pricing
    )

    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)
    register_builtins(dispatcher)

    manager = SessionManager(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=InMemorySessionStore(),
        pricing=pricing,
    )

    session = manager.create_session(
        workspace_path=str(REPO_ROOT), active_model="anthropic:claude-haiku-4-5"
    )

    print("=== Cross-provider continuity smoke test ===")
    print(f"Workspace: {REPO_ROOT}")
    print("Models:")
    print("  Turn 1 — Anthropic:  anthropic:claude-haiku-4-5")
    print("  Turn 2 — OpenAI:     openai:gpt-5-mini")
    print(f"  Turn 3 — OpenRouter: {or_model_id}")
    print(f"Session: {session.id}")
    print()

    failures: list[str] = []

    # ---- Turn 1: Anthropic, tool use ---------------------------------------

    print("--- Turn 1 (Anthropic): read README.md")
    result1 = await manager.submit_turn(
        session.id,
        "Read the file README.md in this workspace and summarize §What it is in one sentence.",
    )
    print(f"  text: {result1.assistant_text[:200]}…")
    print(
        f"  [{result1.chosen_model} • ${result1.cost_usd:.6f} • "
        f"{result1.llm_call_count} LLM / {result1.tool_call_count} tool]"
    )
    if result1.tool_call_count == 0:
        failures.append("Turn 1: expected at least one tool call (read_file)")
    print()

    # Check that the session history has a tool_use + tool_result.
    messages = manager._store.get_messages(session.id)
    tool_use_ids = []
    for m in messages:
        if m.role == Role.ASSISTANT:
            for block in m.content:
                if isinstance(block, ToolUseBlock):
                    tool_use_ids.append(block.id)
    print(f"  canonical tool_use ids recorded: {tool_use_ids}")

    # ---- Turn 2: swap to OpenAI ------------------------------------------

    manager.set_active_model(session.id, "mini")
    print("--- Turn 2 (OpenAI): asking what was just read")
    result2 = await manager.submit_turn(
        session.id,
        "What file did you read in the previous turn? Reply with just the filename.",
    )
    print(f"  text: {result2.assistant_text[:200]}…")
    print(
        f"  [{result2.chosen_model} • ${result2.cost_usd:.6f} • "
        f"{result2.llm_call_count} LLM / {result2.tool_call_count} tool]"
    )
    if "README" not in result2.assistant_text and "readme" not in result2.assistant_text.lower():
        failures.append(
            "Turn 2: OpenAI didn't recognize the prior turn's content — "
            "cross-provider history serialization may be broken"
        )
    print()

    # ---- Turn 3: swap to OpenRouter --------------------------------------

    manager.set_active_model(session.id, "or-fast")
    print(f"--- Turn 3 (OpenRouter {or_model_id}): asking again")
    try:
        result3 = await manager.submit_turn(
            session.id,
            "In two words, what topic has this conversation been about?",
        )
        print(f"  text: {result3.assistant_text[:200]}…")
        print(
            f"  [{result3.chosen_model} • ${result3.cost_usd:.6f} • "
            f"{result3.llm_call_count} LLM / {result3.tool_call_count} tool]"
        )
        # We don't assert content for turn 3 strictly — OR models vary widely.
        # Just verifying the swap doesn't crash is the key signal.
    except Exception as exc:
        failures.append(f"Turn 3: OpenRouter swap failed: {type(exc).__name__}: {exc}")
        print(f"  FAILED: {exc}")
    print()

    # ---- Summary ----------------------------------------------------------

    await bus.drain()
    await bus.stop()
    await anth.close()
    await oai.close()
    await or_adapter.close()
    trace.close()

    fresh = manager._store.get_session(session.id)
    print("=== Result ===")
    print(f"Total cost:  ${fresh.cost_so_far_usd:.6f}")
    print(f"Turn count:  {fresh.turn_count}")
    print(f"Messages:    {len(messages)} (+ turn 2/3 additions)")
    print()
    if failures:
        print(f"FAIL — {len(failures)} assertion(s) violated:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — cross-provider mid-session swap is real.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
