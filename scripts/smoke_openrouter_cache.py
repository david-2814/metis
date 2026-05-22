"""Smoke test: prove the OpenRouter prompt-cache wiring fires end-to-end.

Drives a 3-turn agent loop against an Anthropic Claude model *routed via
OpenRouter* (canonical id ``openrouter:anthropic/claude-...``), using the
natural Metis system prompt so SessionManager's context-assembler §5.1
minimum-cacheable-prefix padding lifts the stable prefix above the upstream
cache floor automatically.

OpenRouter reaches Anthropic with the OpenAI wire shape. Anthropic is an
*explicit-breakpoint* cache family (see ``EXPLICIT_BREAKPOINT_FAMILIES`` in
``adapters/openrouter.py``): nothing is cached unless the request carries a
``cache_control`` marker. The OpenRouter adapter emits that marker on the
stable system segment only when ``_wants_cache_breakpoint()`` is True — i.e.
the wire id is in an explicit-breakpoint family *and* the catalog reports
``input_cache_read`` pricing for the model. This script proves that path
works against the real API and measures the cost delta vs a control run with
the breakpoint disabled.

Asserts:
- Cached run, turn 2+: ``cached_input_tokens > 0`` (the cache is being read).

Reports:
- whether the cache fired, the turn-2+ hit rate, and the measured cost with
  caching vs the caching-disabled control run.

If the cache does NOT fire, runs a raw probe (direct httpx POST, bypassing the
Metis adapter) that dumps OpenRouter's full ``usage`` object so the root cause
can be told apart: wrong wire shape, non-sticky provider routing, or usage
simply not reported on the wire.

Cost: < $0.05 per run (3 cached turns + 3 control turns on the cheapest
Anthropic haiku in the OpenRouter catalog). Needs OPENROUTER_API_KEY.

Usage:
    uv run python scripts/smoke_openrouter_cache.py

============================  MEASURED RESULT  =============================
Run: 2026-05-21 against the live OpenRouter API.
Model: openrouter:anthropic/claude-3.5-haiku  (cheapest *current* haiku in the
       catalog; in=$0.80/Mtok out=$4.00/Mtok cache-read=$0.08/Mtok
       cache-write=$1.00/Mtok). The legacy `claude-3-haiku` is cheaper still
       but its OpenRouter Amazon-Bedrock upstream rejects prompt caching
       outright ("unsupported model") — see `_LEGACY_HAIKU_WIRE_IDS`.

1) THE CACHE WIRING FIRES. The OpenRouter `cache_control` breakpoint reaches
   Anthropic and is honored end-to-end:
     - Turn-2+ hit rate: 96.9%  (11016 / 11368 prompt tokens read from cache).
     - All cache-hit turns read the full ~5508-token stable prefix.
     - Turn-1 cold start (observed on a cache-cold run) reports
       cache_creation_input_tokens = 5508 — cache-write accounting works too.
   Provider-true cost per cache-hit turn $0.000601 vs $0.004558 uncached
   => 86.8% cheaper per cache-hit turn. Whole 3-turn session: ~50% cheaper
   cold-start (turn 1 pays the one-time write premium), ~87% warm-start; the
   write premium amortizes as a session reuses the prefix over more turns.

2) FINDING — Metis MIS-PRICES cached calls (bug is in metis-core, not here).
   `PriceTable.compute_cost` reported the cached pass at $0.014996, i.e. MORE
   than the $0.013674 caching-OFF control, even though the cache demonstrably
   fires. Root cause: OpenRouter speaks the OpenAI wire shape where
   `usage.prompt_tokens` is the TOTAL prompt count (it already includes the
   cache-read/-write tokens); `openai.py:_usage_to_canonical` copies that
   total into `TokenUsage.input_tokens`; `pricing/table.py:compute_cost` then
   sums input + cached + created as if disjoint (the Anthropic-native
   contract), double-billing the cached portion. Every OpenRouter/OpenAI
   cached call is over-reported in trace / `/analytics/cost` /
   `/analytics/savings`. Suggested fix: subtract cached+created from
   `input_tokens` in `_usage_to_canonical` (or in `compute_cost`).
============================================================================
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import msgspec
from metis.core.adapters.openrouter import OpenRouterAdapter, _wire_model_name
from metis.core.events.bus import EventBus, EventFilter, Subscription
from metis.core.events.envelope import Event
from metis.core.pricing import DEFAULT_PRICE_TABLE
from metis.core.routing import ModelRegistry, RoutingEngine
from metis.core.sessions import InMemorySessionStore, SessionManager
from metis.core.tools.builtins import register_builtins
from metis.core.tools.dispatcher import ToolDispatcher

REPO_ROOT = Path(__file__).resolve().parent.parent

# Hard ceiling — abort before this is reached so a misbehaving run can't run
# the bill up. The expected spend is ~$0.02-0.04.
COST_CEILING_USD = 0.09

TURNS = [
    "Reply with the single word OK and nothing else.",
    "Now reply with the single word THANKS and nothing else.",
    "Finally reply with the single word DONE and nothing else.",
]


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# Legacy Claude 3-generation haiku. The catalog reports `input_cache_read`
# pricing for it, but OpenRouter routes it to the Amazon Bedrock upstream,
# which rejects the request outright: "You invoked an unsupported model or
# your request did not allow prompt caching." Bedrock only honors prompt
# caching for Claude 3.5+ — so the catalog's cache-read price is NOT a
# reliable per-upstream caching signal. The task asks for the cheapest
# *current* haiku, so the Claude-3-gen model is excluded here.
_LEGACY_HAIKU_WIRE_IDS = frozenset({"anthropic/claude-3-haiku"})


def _pick_haiku_model(catalog) -> str | None:
    """Cheapest *current* ``openrouter:anthropic/...haiku...`` model that the
    catalog says supports prompt caching (i.e. carries ``input_cache_read``
    pricing).

    Sorted by input rate so we burn as little budget as possible; the
    explicit-breakpoint path is identical across current Anthropic haiku
    revisions. ``~``-prefixed variant ids are skipped: their wire name
    (``~anthropic/...``) does not match the adapter's explicit-breakpoint
    family check, so no breakpoint would be emitted at all.
    """
    candidates: list[tuple[float, str]] = []
    for model_id, caps in catalog.capabilities.items():
        if not model_id.startswith("openrouter:anthropic/"):
            continue
        if "haiku" not in model_id.lower():
            continue
        if not caps.supports_prompt_caching:
            continue
        if _wire_model_name(model_id) in _LEGACY_HAIKU_WIRE_IDS:
            continue
        priced = catalog.pricing.get(model_id)
        if priced is None:
            continue
        candidates.append((float(priced.input_per_mtok), model_id))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


async def _run_pass(
    manager: SessionManager,
    model_id: str,
    label: str,
    prior_spend: float,
) -> float:
    """Run TURNS against a fresh session; return cumulative spend.

    Spend is summed from the synchronous ``TurnResult.cost_usd`` (not the bus
    events, which arrive asynchronously) so the cost ceiling is enforced
    turn-by-turn.
    """
    print(f"\n=== Pass: {label} ===")
    session = manager.create_session(workspace_path=str(REPO_ROOT), active_model=model_id)
    spent = prior_spend
    for i, prompt in enumerate(TURNS, 1):
        result = await manager.submit_turn(session.id, prompt)
        spent += float(result.cost_usd)
        print(
            f"  turn {i}: reply={result.assistant_text!r} "
            f"cost=${result.cost_usd:.6f} in={result.input_tokens} out={result.output_tokens}"
        )
        if spent > COST_CEILING_USD:
            raise RuntimeError(
                f"cost ceiling ${COST_CEILING_USD:.2f} exceeded (${spent:.4f}) — aborting"
            )
    return spent


def _cache_fields(event: Event) -> tuple[int, int, int, float]:
    """(input_tokens, cached_input_tokens, cache_creation_input_tokens, cost_usd)."""
    p = event.payload
    return (
        int(p["input_tokens"]),
        int(p["cached_input_tokens"]),
        int(p["cache_creation_input_tokens"]),
        float(p["cost_usd"]),
    )


def _provider_true_cost(events: list[Event], rates) -> float:
    """Recompute cost the way the upstream actually bills it.

    OpenRouter speaks the OpenAI wire shape, where ``usage.prompt_tokens`` is
    the *total* prompt count — it already includes the cache-read and
    cache-write tokens. ``openai.py:_usage_to_canonical`` copies that total
    into ``input_tokens`` verbatim. But ``PriceTable.compute_cost`` assumes the
    Anthropic-native convention (``input_tokens`` = the uncached remainder, a
    bucket disjoint from cached/created) and adds all three buckets — so for
    an OpenRouter cached call the cached/written tokens are billed twice.

    This helper subtracts the overlap to get the genuinely-fresh token count
    and prices each bucket exactly once.
    """
    total = 0.0
    for e in events:
        p = e.payload
        prompt = int(p["input_tokens"])
        cached = int(p["cached_input_tokens"])
        written = int(p["cache_creation_input_tokens"])
        output = int(p["output_tokens"])
        fresh = max(0, prompt - cached - written)
        total += (
            fresh * float(rates.input_per_mtok)
            + cached * float(rates.cached_read_per_mtok)
            + written * float(rates.cache_creation_per_mtok)
            + output * float(rates.output_per_mtok)
        ) / 1_000_000.0
    return total


async def _raw_cache_probe(api_key: str, wire_model: str) -> None:
    """Bypass the Metis adapter: hand-build a deliberately cacheable request
    and POST it twice back-to-back, dumping OpenRouter's full ``usage`` object.

    This is the root-cause splitter when the agent-loop path shows no cache
    read on turn 2:

    - Both probe calls report zero cache tokens anywhere -> the cache genuinely
      isn't firing (wrong wire shape reaching Anthropic, or OpenRouter routed
      the second call to a different upstream so the prefix went cold).
    - Probe call 2 reports cache tokens (under *any* field name) -> the cache
      fires; the agent-loop path lost them in adapter usage parsing — compare
      the field names below against ``openai.py:_usage_to_canonical``.
    """
    print("\n--- Raw OpenRouter cache probe (bypasses the Metis adapter) ---")
    # ~4k-token deterministic stable block — comfortably over Anthropic's
    # minimum cacheable prefix for the haiku tier.
    block = (
        "Metis OpenRouter prompt-cache diagnostic probe. This paragraph is "
        "deterministic filler so the cached prefix clears the upstream model's "
        "minimum cacheable size on the haiku tier. "
    )
    stable = block * 120
    payload = {
        "model": wire_model,
        "max_tokens": 8,
        "temperature": 0,
        # `provider` is the OpenRouter routing object the adapter also sends.
        "provider": {"allow_fallbacks": True},
        # Ask OpenRouter for full usage accounting explicitly — if the probe
        # surfaces cache tokens *only* with this flag set, the adapter (which
        # does not send it) is the gap.
        "usage": {"include": True},
        "messages": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": stable,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
            {"role": "user", "content": "Reply with OK."},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = "https://openrouter.ai/api/v1/chat/completions"
    async with httpx.AsyncClient(timeout=60.0) as client:
        for attempt in (1, 2):
            try:
                resp = await client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                print(f"  probe call {attempt}: HTTP error: {exc}")
                continue
            try:
                body = resp.json()
            except Exception:
                print(f"  probe call {attempt}: status={resp.status_code} body={resp.text[:400]!r}")
                continue
            if not isinstance(body, dict):
                print(f"  probe call {attempt}: unexpected body {body!r}")
                continue
            usage = body.get("usage")
            provider = body.get("provider")
            print(
                f"  probe call {attempt}: status={resp.status_code} upstream_provider={provider!r}"
            )
            print(f"    raw usage = {json.dumps(usage, indent=2, default=str)}")


async def main() -> int:
    _load_dotenv(REPO_ROOT / ".env")
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key or "..." in api_key:
        print("OPENROUTER_API_KEY not set (or still the placeholder).", file=sys.stderr)
        return 1

    bus = EventBus()
    bus.start()

    completed: list[Event] = []

    async def collector(e: Event) -> None:
        if e.type == "llm.call_completed":
            completed.append(e)

    bus.subscribe(
        Subscription(filter=EventFilter(), handler=collector, name="or-cache-smoke", fast_path=True)
    )

    adapter = OpenRouterAdapter(
        api_key=api_key, app_name="metis-smoke", http_referer="https://metis.local"
    )

    print("=== OpenRouter prompt-cache smoke test ===")
    print("Fetching OpenRouter catalog…")
    catalog = await adapter.fetch_catalog()

    model_id = _pick_haiku_model(catalog)
    if model_id is None:
        print(
            "No Anthropic haiku model with cache-read pricing in the OpenRouter "
            "catalog — cannot validate the cache wiring.",
            file=sys.stderr,
        )
        await adapter.close()
        await bus.stop()
        return 1

    real_caps = adapter._capabilities[model_id]
    rates = catalog.pricing[model_id]
    print(f"Model:   {model_id}")
    print(
        f"Rates:   in=${rates.input_per_mtok}/Mtok out=${rates.output_per_mtok}/Mtok "
        f"cache-read=${rates.cached_read_per_mtok}/Mtok "
        f"cache-write=${rates.cache_creation_per_mtok}/Mtok"
    )
    print("Mode:    natural Metis system prompt (relies on context-assembler §5.1 padding)")

    registry = ModelRegistry()
    registry.register(model_id=model_id, adapter=adapter, aliases=["haiku"])
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

    exit_code = 0
    try:
        # --- Control pass: caching OFF -------------------------------------
        # Force `supports_prompt_caching=False` on the chosen model so the
        # adapter's `_wants_cache_breakpoint()` returns False and emits NO
        # `cache_control` marker. With no breakpoint Anthropic does no caching
        # at all — this is the genuine caching-disabled baseline. Run it first
        # so the cached pass that follows starts from a cold cache.
        adapter._capabilities[model_id] = msgspec.structs.replace(
            real_caps, supports_prompt_caching=False
        )
        spend = await _run_pass(manager, model_id, "control (caching OFF)", 0.0)
        # Drain before slicing: `llm.call_completed` events reach `completed`
        # asynchronously, so slicing without a drain races the bus.
        await bus.drain()
        control_events = list(completed)

        # --- Cached pass: caching ON ---------------------------------------
        adapter._capabilities[model_id] = real_caps
        cached_start = len(completed)
        await _run_pass(manager, model_id, "cached (caching ON)", spend)
        await bus.drain()
        cached_events = completed[cached_start:]
    except Exception as exc:
        print(f"\nFAILED during turn execution: {type(exc).__name__}: {exc}", file=sys.stderr)
        await bus.drain()
        await bus.stop()
        await adapter.close()
        return 1

    await bus.stop()

    # --- Analysis ----------------------------------------------------------
    print("\n=== Cache effectiveness ===")
    if len(cached_events) < 2 or len(control_events) < 2:
        print(
            f"FAILED: expected >= 2 llm.call_completed events per pass "
            f"(control={len(control_events)}, cached={len(cached_events)})."
        )
        await adapter.close()
        return 1

    print("Cached pass, per turn:")
    for i, e in enumerate(cached_events, 1):
        inp, cached, creation, cost = _cache_fields(e)
        print(
            f"  turn {i}: input_tokens={inp} cached_input_tokens={cached} "
            f"cache_creation_input_tokens={creation} metis_cost=${cost:.6f}"
        )

    _, t1_cached, t1_creation, _ = _cache_fields(cached_events[0])
    if t1_creation > 0:
        t1_role = f"cold start — wrote {t1_creation} tokens into the cache"
    elif t1_cached > 0:
        t1_role = (
            f"warm — read {t1_cached} cached tokens (a recent run's prefix is "
            "still inside the ~5-min cache TTL)"
        )
    else:
        t1_role = "no cache activity"

    # Turn-2+ aggregate hit rate — always cache reads, independent of whether
    # turn 1 was a cold write or a warm cross-run read. For the OpenAI wire
    # shape `input_tokens` is the total prompt count, so cached / input_tokens
    # is a true ratio.
    later = cached_events[1:]
    later_input = sum(_cache_fields(e)[0] for e in later)
    later_cached = sum(_cache_fields(e)[1] for e in later)
    hit_rate = (later_cached / later_input) if later_input else 0.0
    cache_fired = later_cached > 0

    # Two cost views: what Metis's PriceTable reports, and the provider-true
    # cost (the latter corrects the OpenAI-wire double-bill — see
    # `_provider_true_cost`).
    metis_cached = sum(_cache_fields(e)[3] for e in cached_events)
    metis_control = sum(_cache_fields(e)[3] for e in control_events)
    true_cached = _provider_true_cost(cached_events, rates)
    true_control = _provider_true_cost(control_events, rates)
    true_saved = true_control - true_cached
    true_saved_pct = (true_saved / true_control * 100.0) if true_control else 0.0

    # Steady-state: provider-true cost of one cache-HIT turn vs one uncached
    # control turn. Reproducible regardless of turn-1 warmth — this is the
    # headline saving number.
    control_per_turn = true_control / len(control_events)
    hit_per_turn = _provider_true_cost(later, rates) / max(1, len(later))
    steady_saved_pct = (1.0 - hit_per_turn / control_per_turn) * 100.0 if control_per_turn else 0.0

    print()
    print(f"Cache fired (turn 2+ read):  {cache_fired}")
    print(
        f"Turn-2+ hit rate:            {hit_rate * 100:.1f}%  ({later_cached}/{later_input} tokens)"
    )
    print(f"Turn 1:                      {t1_role}")
    print()
    print("Steady-state per-turn cost (provider-true):")
    print(f"  uncached control turn:     ${control_per_turn:.6f}")
    print(f"  cache-hit turn:            ${hit_per_turn:.6f}")
    print(f"  -> {steady_saved_pct:.1f}% cheaper per cache-hit turn")
    print()
    print("Whole-pass cost delta — provider-true (cached/written tokens billed once):")
    print(f"  caching ON  (cached):      ${true_cached:.6f}")
    print(f"  caching OFF (control):     ${true_control:.6f}")
    print(
        f"  delta:                     ${-true_saved:+.6f}  "
        f"({-true_saved_pct:+.1f}% — negative = caching cheaper)"
    )
    print()
    print("Whole-pass cost delta — as Metis PriceTable.compute_cost / analytics report it:")
    print(f"  caching ON  (cached):      ${metis_cached:.6f}")
    print(f"  caching OFF (control):     ${metis_control:.6f}")

    # --- Load-bearing assertion: did the cache fire? ----------------------
    if not cache_fired:
        print(
            "\nFAILED: turn 2+ cached_input_tokens == 0 — the OpenRouter "
            "prompt-cache wiring did NOT fire end-to-end.",
            file=sys.stderr,
        )
        # Dig into the root cause rather than just reporting failure.
        await _raw_cache_probe(api_key, _wire_model_name(model_id))
        print(
            "\nRoot-cause guide:\n"
            "  * Probe calls both show zero cache tokens -> cache genuinely not "
            "firing. Either the `cache_control` marker isn't reaching Anthropic "
            "(wire shape — check openai.py:_openai_system_message), or OpenRouter "
            "routed the second call to a different upstream and the prefix went "
            "cold (sticky routing — check openrouter.py:_provider_routing).\n"
            "  * Probe call 2 shows cache tokens under some field -> the cache "
            "fires but the adapter drops them in usage parsing. Compare the "
            "probe's `usage` field names against openai.py:_usage_to_canonical "
            "(it reads prompt_tokens_details.cached_tokens / .cache_write_tokens).",
            file=sys.stderr,
        )
        await adapter.close()
        return 1

    # Cache fired — the wiring works. But surface the cost-accounting bug the
    # measurement exposes: Metis reports caching as *more* expensive.
    if metis_cached > true_cached * 1.05:
        print(
            "\n"
            "+============================== FINDING ==============================+\n"
            "| The OpenRouter cache wiring FIRES, but Metis MIS-PRICES cached calls.|\n"
            "+=====================================================================+\n"
            f"  Metis PriceTable reports the cached pass at ${metis_cached:.6f} — "
            f"{'MORE' if metis_cached > metis_control else 'less'} than the "
            f"${metis_control:.6f} control,\n"
            "  even though the cache demonstrably fires. Root cause:\n"
            "    * OpenRouter speaks the OpenAI wire shape: `usage.prompt_tokens`\n"
            "      is the TOTAL prompt count and already includes cache-read and\n"
            "      cache-write tokens. `openai.py:_usage_to_canonical` copies it\n"
            "      straight into `TokenUsage.input_tokens`.\n"
            "    * `pricing/table.py:compute_cost` assumes the Anthropic-native\n"
            "      convention (input_tokens = uncached remainder, disjoint from\n"
            "      cached / created) and SUMS all three buckets — so the cached\n"
            "      and written tokens are billed twice.\n"
            "  Effect: every OpenRouter (and OpenAI) cached call is over-billed in\n"
            "  the trace / `/analytics/cost` / `/analytics/savings` surface; the\n"
            "  cache makes calls cheaper at the provider but Metis reports the\n"
            "  opposite. The provider-true delta above shows the real saving.\n"
            "  Fix belongs in metis-core (not this script): either subtract the\n"
            "  overlap in `_usage_to_canonical` so `input_tokens` is the uncached\n"
            "  remainder (matching the Anthropic adapter's contract), or make\n"
            "  `compute_cost` subtract cached+created from input_tokens.\n",
            file=sys.stderr,
        )

    print(
        f"\nPASSED — OpenRouter prompt-cache wiring fires end-to-end "
        f"({hit_rate * 100:.1f}% turn-2+ hit rate). "
        f"Total spend ${true_cached + true_control:.6f}."
    )
    await adapter.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
