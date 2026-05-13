"""Smoke test: drive SessionManager end-to-end against the real API.

Reads ANTHROPIC_API_KEY from .env (or environment). Runs two scripted
turns against the metis repo itself as the workspace, prints assistant
responses + costs, then shuts down cleanly.

Usage:
    uv run python scripts/smoke.py [--model haiku|sonnet|opus]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from metis_core.adapters.anthropic import AnthropicAdapter
from metis_core.events.bus import EventBus, EventFilter, Subscription
from metis_core.events.envelope import Event
from metis_core.pricing import DEFAULT_PRICE_TABLE
from metis_core.routing import ModelRegistry, RoutingEngine
from metis_core.sessions import InMemorySessionStore, SessionManager
from metis_core.tools.builtins import register_builtins
from metis_core.tools.dispatcher import ToolDispatcher
from metis_core.trace.store import TraceStore

ANTHROPIC_MODELS = {
    "anthropic:claude-opus-4-7": ["opus", "deep"],
    "anthropic:claude-sonnet-4-6": ["sonnet", "balanced"],
    "anthropic:claude-haiku-4-5": ["haiku", "fast"],
}

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Metis smoke test.")
    parser.add_argument("--model", default="haiku", help="alias or canonical id")
    args = parser.parse_args()

    _load_dotenv(REPO_ROOT / ".env")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or "sk-ant-..." in api_key:
        print("ANTHROPIC_API_KEY not set (or still the placeholder).", file=sys.stderr)
        return 1

    bus = EventBus()
    bus.start()

    # Capture event log for the final summary.
    event_log: list[Event] = []

    async def collector(e: Event) -> None:
        event_log.append(e)

    bus.subscribe(
        Subscription(filter=EventFilter(), handler=collector, name="smoke-log", fast_path=True)
    )

    db_path = REPO_ROOT / ".metis" / "smoke-trace.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()  # fresh trace per smoke run
    trace = TraceStore(db_path)
    trace.attach_to(bus)

    adapter = AnthropicAdapter(api_key=api_key)
    registry = ModelRegistry()
    for model_id, aliases in ANTHROPIC_MODELS.items():
        registry.register(model_id=model_id, adapter=adapter, aliases=aliases)

    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)
    register_builtins(dispatcher)

    manager = SessionManager(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=InMemorySessionStore(),
        pricing=DEFAULT_PRICE_TABLE,
    )

    resolved = registry.resolve_alias(args.model)
    if resolved is None:
        print(f"unknown model: {args.model}", file=sys.stderr)
        return 1

    session = manager.create_session(workspace_path=str(REPO_ROOT), active_model=resolved)

    print("=== Metis smoke test ===")
    print(f"Workspace: {REPO_ROOT}")
    print(f"Model:     {resolved}")
    print(f"Session:   {session.id}")
    print(f"Trace:     {db_path}")
    print()

    turns = [
        "What files are in this workspace's docs/specs directory?",
        "Read docs/specs/canonical-message-format.md and summarize §1 (Purpose) in one sentence.",
    ]

    exit_code = 0
    for i, prompt in enumerate(turns, 1):
        print(f"--- Turn {i}: {prompt}")
        try:
            result = await manager.submit_turn(session.id, prompt)
        except Exception as exc:
            print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            exit_code = 1
            break
        print()
        print(result.assistant_text)
        print()
        print(
            f"  [model={result.chosen_model} cost=${result.cost_usd:.6f} "
            f"llm={result.llm_call_count} tools={result.tool_call_count} "
            f"in={result.input_tokens} out={result.output_tokens} "
            f"wall={result.wall_time_seconds:.2f}s]"
        )
        print()

    await bus.drain()
    await bus.stop()
    await adapter.close()

    # Summary
    print("=== Session totals ===")
    fresh = manager._store.get_session(session.id)
    print(f"Turns:      {fresh.turn_count}")
    print(f"Total cost: ${fresh.cost_so_far_usd:.6f}")
    type_counts: dict[str, int] = {}
    for e in event_log:
        type_counts[e.type] = type_counts.get(e.type, 0) + 1
    print(f"\nEvents emitted ({len(event_log)} total):")
    for t in sorted(type_counts):
        print(f"  {t}: {type_counts[t]}")

    trace.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
