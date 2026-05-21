# Changelog

All notable user-facing changes to Metis are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Pre-1.0, minor releases may include breaking changes.

This is the release changelog. For the cross-spec change log (contract-level
changes to the design specs), see [`docs/specs/CHANGES.md`](docs/specs/CHANGES.md).

## [Unreleased]

## [0.1.0] - 2026-05-19

First public release — the GA launch milestone. Metis is a local-first AI dev
agent server: provider-agnostic via a canonical message format, with a layered
routing engine, bounded portable memory, and an event bus feeding a trace store.

### Added

- **Canonical message format** — provider-agnostic `Message` / content-block
  types with full JSON round-tripping, shared across every adapter.
- **Provider adapters** — Anthropic, OpenAI, and OpenRouter, with wire
  translation, error classification, bounded retry, cancellation, and
  streaming. Cross-provider continuity verified mid-session.
- **Routing engine** — 7-slot decision chain (per-message override, manual
  sticky, configured rules, learned patterns, delegation, workspace default,
  global default) with capability validation and per-(provider, model)
  availability tracking.
- **Pattern store** — per-workspace learning of structural fingerprints and
  outcomes that feeds the routing engine.
- **Bounded memory** — byte-budgeted `MEMORY.md` / `USER.md` per workspace,
  with eviction signals and consolidation tools.
- **Event bus and trace store** — closed event catalog, bounded async
  dispatch, and a SQLite-backed trace store with replay and causal-chain
  query. Backup/restore and sliding-window retention included.
- **Evaluator** — heuristic, LLM, and hybrid judges for turn / tool-cycle /
  session / workload subjects, with a shared per-session and per-day budget.
- **Delegation** — a `delegate()` tool that spawns synchronous worker
  sessions, with worker spend rolled up under the planner in analytics.
- **CLI** — `metis chat`, `metis tui`, `metis serve`, `metis gateway`,
  `metis auth`, plus benchmark, evaluate, trial, audit, and trace-admin
  subcommands.
- **Transparent HTTP gateway** — OpenAI- and Anthropic-shape endpoints
  (`/v1/chat/completions`, `/v1/messages`) that route through the engine,
  with per-request authentication and gateway-key lifecycle management.
- **Multi-user attribution** — per-key / per-user / per-team cost and token
  rollups across the analytics surface.
- **Analytics and local dashboard** — savings attribution, quality
  analytics, and per-key cost views.
- **Prompt caching** — universal cache-breakpoint placement on the stable
  prefix, live-validated against the benchmark suite.
- **Savings benchmark suite** — versioned workloads and harness with an
  LLM-judge integration.
- **Credential resolver** — a documented resolution chain (CLI flag → env
  var → `~/.metis/credentials.yaml` → `~/.metis/.env` → keychain) and the
  `metis auth` setup and diagnostics surface.
- **Operations docs** — incident response, SLA template, status page,
  upgrade guide, observability runbook, and a buyer-trial quickstart.
- **Observability** — Prometheus `/metrics` endpoints on the server and
  gateway, plus Helm templates for ServiceMonitor, PrometheusRule, and a
  Grafana dashboard.

[Unreleased]: https://github.com/david-2814/metis/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/david-2814/metis/releases/tag/v0.1.0
