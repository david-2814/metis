"""Trace-store write-throughput baseline (Wave 13, docs/operations/trace-performance.md).

Drives the bus + TraceStore with synthetic `llm.call_completed` events,
measures end-to-end events/second, and reports CPU vs disk vs WAL
posture. Single-process, no network — the goal is the trace-store hot
path, not the agent loop or the gateway.

Usage:
    uv run python scripts/bench_trace_throughput.py                # default 50k events
    uv run python scripts/bench_trace_throughput.py --events 200000
    uv run python scripts/bench_trace_throughput.py --bus-only     # skip TraceStore (bus ceiling)
    uv run python scripts/bench_trace_throughput.py --raw-sqlite   # bypass bus + msgspec (SQLite ceiling)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import resource
import sqlite3
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import msgspec
from metis_core.events.bus import EventBus
from metis_core.events.envelope import Actor
from metis_core.events.payloads import LLMCallCompleted, make_event
from metis_core.trace.store import TraceStore


# Synthetic event template. Held outside the loop so we measure
# steady-state emit + write, not msgspec.Struct construction overhead.
def _build_event(i: int):
    return make_event(
        type="llm.call_completed",
        session_id=f"sess_{i % 100}",
        turn_id=f"turn_{i}",
        parent_event_id=None,
        actor=Actor.SYSTEM,
        payload=LLMCallCompleted(
            model="anthropic:claude-sonnet-4-6",
            provider="anthropic",
            input_tokens=1500,
            output_tokens=400,
            cached_input_tokens=1200,
            cache_creation_input_tokens=0,
            cost_usd=0.0042,
            pricing_version="2026-05-01",
            latency_ms=850,
            stop_reason="end_turn",
            produced_tool_calls=0,
            produced_thinking_blocks=0,
            gateway_key_id=f"gw_{i % 100}",
            inbound_shape="anthropic",
            user_id=f"user_{i % 200}",
            team_id=f"team_{i % 20}",
            parent_session_id=None,
        ),
        timestamp=datetime.now(UTC),
    )


async def _bench_bus_plus_trace(events: int, db_path: Path) -> dict:
    """Full path: bus.emit -> async dispatch -> TraceStore.handle."""
    bus = EventBus(queue_size=max(events * 2, 2048))
    bus.start()
    store = TraceStore(db_path)
    handle = store.attach_to(bus)

    cpu_start = time.process_time()
    rusage_start = resource.getrusage(resource.RUSAGE_SELF)
    wall_start = time.perf_counter()
    for i in range(events):
        bus.emit(_build_event(i))
    enqueue_end = time.perf_counter()
    await bus.drain()
    wall_end = time.perf_counter()
    cpu_end = time.process_time()
    rusage_end = resource.getrusage(resource.RUSAGE_SELF)

    bus.unsubscribe(handle)
    await bus.stop()
    wal_bytes = store.wal_size_bytes()
    db_bytes = Path(db_path).stat().st_size
    store.close()

    return {
        "scenario": "bus+trace",
        "events": events,
        "wall_seconds": wall_end - wall_start,
        "enqueue_seconds": enqueue_end - wall_start,
        "drain_seconds": wall_end - enqueue_end,
        "cpu_seconds": cpu_end - cpu_start,
        "user_cpu_delta_s": rusage_end.ru_utime - rusage_start.ru_utime,
        "system_cpu_delta_s": rusage_end.ru_stime - rusage_start.ru_stime,
        "block_input_ops": rusage_end.ru_inblock - rusage_start.ru_inblock,
        "block_output_ops": rusage_end.ru_oublock - rusage_start.ru_oublock,
        "wal_bytes_after": wal_bytes,
        "db_bytes_after": db_bytes,
    }


async def _bench_bus_only(events: int) -> dict:
    """Bus emit + drain with NO subscriber — pure bus/dispatch ceiling."""
    bus = EventBus(queue_size=max(events * 2, 2048))
    bus.start()

    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    for i in range(events):
        bus.emit(_build_event(i))
    enqueue_end = time.perf_counter()
    await bus.drain()
    wall_end = time.perf_counter()
    cpu_end = time.process_time()

    await bus.stop()
    return {
        "scenario": "bus-only",
        "events": events,
        "wall_seconds": wall_end - wall_start,
        "enqueue_seconds": enqueue_end - wall_start,
        "drain_seconds": wall_end - enqueue_end,
        "cpu_seconds": cpu_end - cpu_start,
    }


def _bench_raw_sqlite(events: int, db_path: Path) -> dict:
    """Direct INSERT loop — SQLite ceiling, no bus, no msgspec.Struct construction."""
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA wal_autocheckpoint = 8192")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS bench_events (
          id TEXT PRIMARY KEY,
          timestamp_us INTEGER NOT NULL,
          payload_json TEXT NOT NULL
        );
        """
    )

    # Pre-encode one canonical payload so we measure pure INSERT, not
    # serialization. Fair representation of the steady state since the
    # payload encode is the same call done by TraceStore.write.
    payload = msgspec.json.encode(
        {
            "model": "anthropic:claude-sonnet-4-6",
            "provider": "anthropic",
            "input_tokens": 1500,
            "output_tokens": 400,
            "cached_input_tokens": 1200,
            "cache_creation_input_tokens": 0,
            "cost_usd": 0.0042,
            "latency_ms": 850,
        }
    ).decode("utf-8")
    now_us = int(time.time() * 1_000_000)

    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    for i in range(events):
        # Mimic ULID-ish 26-char id; uniqueness via i suffix.
        conn.execute(
            "INSERT INTO bench_events (id, timestamp_us, payload_json) VALUES (?, ?, ?)",
            (f"01HX0000000000000000000{i:09d}"[-26:], now_us + i, payload),
        )
    wall_end = time.perf_counter()
    cpu_end = time.process_time()
    conn.close()

    return {
        "scenario": "raw-sqlite",
        "events": events,
        "wall_seconds": wall_end - wall_start,
        "cpu_seconds": cpu_end - cpu_start,
    }


def _format_result(result: dict) -> str:
    n = result["events"]
    wall = result["wall_seconds"]
    cpu = result["cpu_seconds"]
    eps = n / wall if wall > 0 else float("inf")
    cpu_share = cpu / wall if wall > 0 else 0.0
    out = [
        f"  scenario:           {result['scenario']}",
        f"  events:             {n:,}",
        f"  wall:               {wall:.3f}s",
        f"  events/sec:         {eps:,.0f}",
        f"  cpu_seconds:        {cpu:.3f}s",
        f"  cpu_share_of_wall:  {cpu_share:.2%}  (low = waiting on disk; high = CPU-bound)",
    ]
    if "enqueue_seconds" in result:
        out.append(f"  enqueue_seconds:    {result['enqueue_seconds']:.3f}s")
        out.append(f"  drain_seconds:      {result['drain_seconds']:.3f}s")
    if "wal_bytes_after" in result:
        out.append(f"  wal_bytes_after:    {result['wal_bytes_after']:,}")
        out.append(f"  db_bytes_after:     {result['db_bytes_after']:,}")
    if "block_output_ops" in result:
        out.append(
            f"  rusage block_out:   {result['block_output_ops']}  (0 on macOS; populated on Linux)"
        )
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--events", type=int, default=50_000)
    p.add_argument("--bus-only", action="store_true", help="Skip TraceStore subscriber")
    p.add_argument(
        "--raw-sqlite",
        action="store_true",
        help="Bypass bus + msgspec; measure SQLite INSERT ceiling",
    )
    p.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="DB path (default: tempfile, deleted after run)",
    )
    args = p.parse_args()

    db_dir = tempfile.mkdtemp(prefix="metis-bench-")
    db_path = Path(args.db_path) if args.db_path else Path(db_dir) / "trace.db"
    print(f"# trace-throughput bench (events={args.events:,}, db={db_path})")
    print(f"# python={sys.version.split()[0]} sqlite={sqlite3.sqlite_version}")
    print()

    if args.raw_sqlite:
        result = _bench_raw_sqlite(args.events, db_path)
    elif args.bus_only:
        result = asyncio.run(_bench_bus_only(args.events))
    else:
        result = asyncio.run(_bench_bus_plus_trace(args.events, db_path))

    print(_format_result(result))
    # Cleanup unless caller pinned a path.
    if args.db_path is None:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(str(db_path) + suffix)
            except FileNotFoundError:
                pass
        os.rmdir(db_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
