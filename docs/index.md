---
hide:
  - toc
---

# Metis

**A local-first AI dev agent for your terminal.** Provider-agnostic, cost-aware,
and self-improving. Your codebase stays on your machine; your prompts and traces
don't leave it.

```text
$ uv run metis dev .
metis> help me debug src/parser.py        # uses your default model (sonnet)
metis> @haiku summarize what you just did # route one turn to a cheaper model
metis> /cost                              # per-turn USD breakdown
metis> /model haiku                       # sticky switch for the rest of the session
```

[:material-rocket-launch: Quick start](#quick-start){ .md-button .md-button--primary }
[:material-book-open: Project overview](project-overview.md){ .md-button }
[:material-source-branch: GitHub](https://github.com/david-2814/metis){ .md-button }

---

## Why Metis

=== ":material-swap-horizontal: Provider-agnostic"

    One canonical message format. Three adapters — Anthropic, OpenAI,
    OpenRouter. Switch models mid-session and tool-use round-trips just work.
    Adding a provider is writing an adapter, not refactoring the system.

    ```bash
    metis> @opus design a refactor strategy
    metis> @sonnet implement the first step
    metis> @haiku write the test fixtures
    ```

=== ":material-notebook-edit: Bounded memory you can git-diff"

    `MEMORY.md` (~2 KB) and `USER.md` (~1.5 KB) per workspace, agent-curated.
    Plain Markdown on disk under `<workspace>/.metis/`. Soft cap emits an
    eviction signal; hard cap rejects the write so the agent has to consolidate.
    Edit, version, and sync via git.

    ```text
    .metis/
    ├── MEMORY.md        # workspace memory (agent-curated)
    ├── USER.md          # user preferences
    ├── routing.yaml     # optional per-workspace routing rules
    └── trust.yaml       # tool-confirmation policy
    ```

=== ":material-routes: Explainable routing"

    Per-message `@alias` → sticky `/model` → workspace yaml rules → learned
    patterns → workspace default → global default. Every turn emits one
    `route.decided` event with the full seven-slot chain trace. No silent
    overrides.

    ```yaml
    # .metis/routing.yaml
    rules:
      - when: { tool_only: true }
        choose: haiku
      - when: { content_type: code, files_touched_gte: 3 }
        choose: sonnet
    ```

=== ":material-currency-usd: Cost in real time"

    Per-turn input/output/cached tokens computed in Decimal USD (not
    provider-rounded floats), versioned so historical traces can be re-priced.
    Per-key, per-user, per-team rollups via `/analytics/cost`.

    ```text
    metis> /cost
    turn 1  sonnet  $0.0023  (3,420 in / 412 out)
    turn 2  haiku   $0.0001  (   18 in /  64 out)
    session total:  $0.0024
    ```

---

## Quick start

Two-minute path from clone to first chat. Requires Python 3.13 and
[uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/david-2814/metis && cd metis
uv sync                                       # resolves the workspace

echo "ANTHROPIC_API_KEY=sk-ant-..." > .env    # or OPENAI / OPENROUTER

uv run metis dev .                            # start a dev session
```

`metis dev` is the advised command; `metis chat` is a kept alias — interchangeable.

Sanity-check the loop against the real API in under a minute (~$0.015 with
haiku):

```bash
uv run python scripts/smoke.py --model haiku
```

Prefer a TUI? `uv run metis tui .` opens the Textual app over the same loop.
Want a server? `uv run metis serve . --port 8421` exposes HTTP + WebSocket.

---

## Three ways to use Metis

<div class="grid cards" markdown>

-   :material-console: **As a local CLI agent**

    ---

    Run `metis dev` or `metis tui` against any workspace. Tools, memory,
    routing, evaluator — full agent loop on localhost.

    [:octicons-arrow-right-24: Project overview](project-overview.md)

-   :material-swap-horizontal: **As a drop-in gateway**

    ---

    Point Claude Code, Cursor, or any SDK at a Metis gateway URL. Same client,
    cost-stamped traces per user/team/key, no client code changes.

    [:octicons-arrow-right-24: Gateway client quickstart](gateway-client-quickstart.md)

-   :material-server: **As a self-hosted team gateway**

    ---

    kind cluster + helm install + per-key cost rollup, automated end-to-end.
    SLA template, status-page recipe, SOC2 readiness audit included.

    [:octicons-arrow-right-24: Buyer trial in < 1 hour](operations/quickstart.md)

</div>

---

## How the doc site is organized

This site is built from the [`docs/`](https://github.com/david-2814/metis/tree/main/docs)
tree in the repo with [mkdocs-material](https://squidfunk.github.io/mkdocs-material/).
Four top-level sections in the nav:

- **Getting Started** — onboarding paths, first-run quickstarts, the savings demo.
- **Specs** — the component contracts. The design is specified before code lands;
  [canonical message format](specs/canonical-message-format.md) is the load-bearing
  data contract, [event bus & trace catalog](specs/event-bus-and-trace-catalog.md)
  the observability spine, [routing engine](specs/routing-engine.md) the model
  selection pipeline. The [change log](specs/CHANGES.md) tracks every cross-spec
  edit.
- **Operations** — playbooks an SRE will read before signing: incident response,
  SLA template, status-page recipe, SOC2 readiness, trace-store performance.
- **Reference** — [known issues](KNOWN_ISSUES.md) (the watchlist of "looks fine
  but is subtly wrong") and the [market research synthesis](market-research/synthesis.md).

Every page on this site has an **Edit this page** and **View source** action in
the top-right. Both point at the exact file backing the page on GitHub. Spec
edits go through a PR; the change log lives at
[specs/CHANGES.md](specs/CHANGES.md).

---

## Project status

Phase 1 + Phase 2 + Phase 2.5 + Phase 3 shipped. Wave 16 reached the GA launch
milestone for the first paid cohort. **1841 tests passing**.

Validated cost-savings headline: **delegation at 8.3% – 26.1% better
cost-per-quality** (19.9% midpoint) across three independent A3 runs on the
fan-out workload. Slot-4 model selection is a proof-of-mechanism from §A3-rev3;
the task-domain wedge is deferred post-GA. See
[savings demo](savings-demo.md) for the full evidence.

## Running this site locally

```bash
# One-shot preview with mkdocs-material installed on demand:
uv run --with mkdocs-material mkdocs serve

# Or via Docker (mirrors the gateway shape; serves on 127.0.0.1:8423):
docker compose --profile docs up docs
```

Both bind loopback-only by default. The site is pure-static once built
(`mkdocs build` writes to `site/`), so any static host works for production.
