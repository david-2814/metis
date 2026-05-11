# AGENTS.md

Shared context for any AI agent working in this repo (Claude Code, Cursor, Codex, Aider, etc.). `CLAUDE.md` is a symlink to this file.

## What Metis is

A local-first AI dev agent server. Provider-agnostic via a canonical message format, with three-layer routing (manual / configured rules / learned patterns), bounded portable memory, and an event bus that feeds the trace store. Python core server on localhost; thin clients (TUI first, Tauri later).

**Status:** pre-alpha, design phase. The specs in `docs/specs/` are the source of truth; very little implementation exists yet.

## Read before reasoning about the system

The design is specified before code lands. In order of load-bearing-ness:

1. [docs/project-overview.md](docs/project-overview.md) — vision, principles, architecture, phasing.
2. [docs/specs/canonical-message-format.md](docs/specs/canonical-message-format.md) — the foundational data contract; every other spec depends on it.
3. [docs/specs/event-bus-and-trace-catalog.md](docs/specs/event-bus-and-trace-catalog.md) — observability spine, closed event catalog.
4. [docs/specs/routing-engine.md](docs/specs/routing-engine.md) — model selection pipeline, delegation.
5. [docs/specs/streaming-protocol.md](docs/specs/streaming-protocol.md) — WebSocket protocol.
6. [docs/specs/provider-adapter-contract.md](docs/specs/provider-adapter-contract.md), [docs/specs/tool-dispatcher.md](docs/specs/tool-dispatcher.md), [docs/specs/server-api.md](docs/specs/server-api.md) — component contracts.
7. [docs/specs/CHANGES.md](docs/specs/CHANGES.md) — cross-spec change log; check `pending review` entries before editing dependent specs.

For competitive landscape and prior art: [docs/market-research/synthesis.md](docs/market-research/synthesis.md) and the four per-stream reports alongside it.

## Working norms

- **Specs-first.** Don't propose implementation changes that contradict the specs without flagging the spec impact. If a spec needs to change, draft the spec change first, then the code.
- **Cross-spec discipline.** When a spec change touches a contract, add an entry to `docs/specs/CHANGES.md` (date, change, type, references to verify, status). See the file's header for the format.
- **Solo, part-time owner.** One engineer, ~part-time pace. Scope decisions should favor what one person can land and maintain, not what a team could.
- **Design phase.** Until Phase 1 begins, the right output is usually a doc or a spec edit, not code. Don't scaffold modules that no spec calls for yet.
- **Bounded memory is a feature.** `MEMORY.md` and `USER.md` are intentionally small (~2 KB / ~1.5 KB). Don't propose unbounded growth — the eviction is the point.

## Non-obvious external context

- **LiteLLM is *not* safe as the canonical internal representation.** It has live bugs around `tool_use`, `cache_control`, and thinking-block translation. Write per-vendor adapters against canonical types; use LiteLLM only as an optional egress proxy if at all.
- **agentskills.io is a verified open standard** (Anthropic-originated, multiple implementers). When designing the skill format, conform to it; don't invent fields.
- **Letta is the bounded-memory peer worth studying** (Series A; ships core/archival/recall memory plus agent self-edit tools). Reference its mechanics before redesigning memory primitives.

## Stack and conventions

- **Python 3.13**, `uv` for env and build (`uv_build` backend, see `pyproject.toml`).
- **`msgspec`** for canonical types (not Pydantic).
- **Ruff** lint+format: line length 100, rules `E,F,I,B,UP,RUF`, `E501` ignored.
- **pytest** with `pytest-asyncio` in auto mode; tests live in `tests/` mirroring `src/metis/` structure.
- **mypy** for typing.
- Run tests: `uv run pytest`. Lint: `uv run ruff check`. Format: `uv run ruff format`.

## Repo layout

- `docs/specs/` — load-bearing specs (read first).
- `docs/market-research/` — competitive landscape, verified 2026-05-09.
- `src/metis/` — implementation (mostly empty in design phase).
- `tests/` — pytest suite, mirrors `src/metis/`.
- `README.md` — public-facing overview.

## When in doubt

Ask before refactoring or expanding scope. The owner prefers a one-line clarifying question over a large speculative diff.
