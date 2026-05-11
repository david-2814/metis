# Competitive Landscape Synthesis

*Compiled 2026-05-09. Entry point for the per-stream reports in this folder: [coding agents](01-coding-agents.md), [local-first platforms](02-local-first-platforms.md), [routing layers](03-routing-layers.md), [skills and memory](04-skills-and-memory.md).*

## Headline

The "TUI/CLI multi-provider coding agent" lane is crowded — OpenCode (157k stars), Claude Code (122k), Gemini CLI (104k), Codex (81k), Cline (62k), Goose (45k), Aider (45k), Roo Code (24k), Crush (24k). Server-client architecture, BYO key, Ollama support, and multi-provider routing are **table stakes**, not differentiators.

metis's defensible wedge is the trio of:

1. **Bounded, agent-curated memory** with hard byte budgets (~2KB MEMORY.md, ~1.5KB USER.md)
2. **Lossless canonical message format** that round-trips Anthropic's `cache_control`, `thinking`, citations, and `tool_use` blocks across providers
3. **Task-fingerprint pattern learning** → cold-start routing recommendations and auto-derived skills

No competitor combines all three. The architecture pitch alone won't differentiate post-OpenCode — the **learning/memory mechanics have to be the headline**.

## The closest competitors, ranked

| Rank | Product | Why it's close | Where metis differs |
|---|---|---|---|
| 1 | **OpenCode (sst)** — 157k★, MIT | Already a server/client + TUI architecture, multi-provider via Models.dev, mid-session swap | No bounded memory, no fingerprint learning, no canonical-IR claim |
| 2 | **Goose (Block)** — 45k★, Apache-2.0 | True multi-provider (incl. Ollama), Recipes ≈ skills, lead/worker model split, Tauri desktop | Recipes are author-written, not auto-derived; no bounded memory |
| 3 | **Roo Code** — 24k★, Apache-2.0 | Per-mode model assignment + orchestrator delegation = exactly metis's "agent-decided delegation across tiers" | VS Code-only, no canonical IR, unbounded `.roorules` |
| 4 | **Aider** — 45k★, Apache-2.0 | `architect+editor` two-tier ≈ metis's planner/worker; `CONVENTIONS.md` ≈ skills; `/model` swap | CLI-only (no server), two tiers not three, no skills standard |
| 5 | **Letta** (memory side) — 23k★, Apache-2.0 + cloud, Series A | Hierarchical bounded memory (core/archival/recall) with agent self-edit tools — closest prior art for metis's memory model | Not a coding agent, server/cloud-leaning |
| 6 | **gptme** — 4k★, MIT | CLI-first local agent, markdown-logged sessions, multi-provider, MCP — closest spiritual match on the local side | No skills standard, no bounded memory, no server/client split |

## Verified facts that matter

**agentskills.io is real and broadly adopted.** Anthropic-originated open standard. Spec at `agentskills.io/specification`: `SKILL.md` with YAML frontmatter, optional `scripts/` / `references/` / `assets/` dirs, progressive disclosure (~100 token metadata at startup, full body on activation). ~35+ implementers verified May 2026 including Anthropic, **OpenAI Codex, Google Gemini CLI, GitHub Copilot, Cursor, JetBrains Junie, OpenCode, Goose, Roo Code, Letta**. Betting on this format is the right call — it's the de facto interop layer.

**No successful agent-skills marketplace exists.** GPT Store hit ~3M GPTs but is a graveyard (broken discovery, no quality bar, abandoned wrappers). Cursor Directory is healthy but unmonetized. Anthropic's `anthropics/skills` is a small curated repo, not a marketplace. Open lane, but lessons: curation > volume; sandboxing `scripts/` trust is the hard part.

**Bounded-curated memory is genuinely contrarian.** The dominant pattern is "vector-store-everything-and-RAG-it-back" (mem0, Zep, Cognee, most LangChain memory) — they treat eviction as a bug. Only **Letta** (bounded core blocks with agent self-edit tools) and **Anthropic's memory tool** (file-based, no enforced cap) treat eviction as a feature. metis's hard byte budgets are tighter than both. "Eviction is a feature" is a real wedge.

**LiteLLM does NOT solve the canonical-format problem.** Open issues from the last few weeks prove it:

- `#27512` (2026-05-09) — Anthropic Messages retry drops thinking blocks
- `#27469` (2026-05-08) — `tool_call.function.arguments` lost in OpenAI→Anthropic conversion (regression in v1.83.7)
- `#15601` — thinking blocks missing on requests with tool calls
- `#26625` / `#20418` / `#20485` — Bedrock + Vertex `cache_control` placement broken
- `#26937` — citations on Bedrock Converse not supported

Recommendation: keep metis's typed adapters per provider; treat Anthropic blocks as authoritative internal shape. Use LiteLLM (optionally) only as transport below adapters for users who want a single key endpoint.

**Vercel AI SDK is the highest "lunch-eat" risk.** Cleanest typed message abstraction in TS (24k★), but TS-only, no built-in agent loop *yet*, and has its own thinking-block bugs (#13430, #13703 still open Mar–Apr 2026). If they ship a typed `Agent` with delegation primitives, they compete on the SDK side.

**Standalone routing-as-a-service is fading.** Not Diamond's Python SDK was archived Dec 2025; RouteLLM (LMSYS) hasn't pushed since Aug 2024. The router-without-an-agent thesis is weakening — which means metis's `delegate(tier, task, context)` as an *in-loop* primitive is novel and underexploited.

## The genuine gaps metis can own

1. **Bounded byte-budgeted memory** — every competitor uses unbounded markdown (`CLAUDE.md`, `AGENTS.md`, `.clinerules`) that silently bloats context. Hard budgets + agent curation is novel.
2. **Auto-derived skills** from past task patterns — Goose Recipes and Claude Skills are author-written; nobody auto-generates skills from successful runs.
3. **Three-tier routing** (manual → YAML → agent-delegated) with **role-attributed cost** (planner vs worker) — Aider has two tiers without role attribution; nobody exposes delegation as a first-class agent tool.
4. **Lossless canonical IR** with all Anthropic-native blocks round-tripping — every router has bug-of-the-week problems on this; only Vercel AI SDK is close on shape (and it's TS-only with its own bugs).
5. **Replays surviving provider changes** — depends on (4); no router product offers this because their IR is lossy.
6. **Plain-git-remote sync of memory + skills** — Cursor uses proprietary cloud, Continue Hub is closed, most tools don't sync at all.

## Where metis risks being a me-too

- **"TUI multi-provider agent with markdown rules"** is saturated — at least 8 credible options. Yet-another TUI is not a wedge.
- **Server/client split** stopped being differentiating after OpenCode hit 157k stars.
- **Provider catalog** (Anthropic + OpenAI + Ollama + OpenRouter) is *behind* the field on day one — Cline, Roo, Goose, OpenCode all already ship Ollama and 5+ providers.
- **MCP support** is implicit table stakes by 2026 — not in metis's pitch (Phase 3), which is a gap to close, not a feature to claim.

## Strategic implications

- Lead the public narrative with **memory + skill-learning + cost-attributed delegation**, not architecture.
- Adopt **agentskills.io from day one** — verified standard, 35+ implementers, free distribution surface.
- Don't depend on LiteLLM for canonical IR — write per-vendor adapters and treat Anthropic's content blocks as the internal shape.
- Pull **Ollama and MCP support forward** from Phase 3 to Phase 1 if possible — they're table stakes.
- Watch Letta's memory architecture closely (core blocks + self-edit tools); it's the validated prior art.
- Watch Vercel AI SDK — if they ship an Agent abstraction, they're the most credible "ate metis's lunch" candidate.

## Methodology note

Four parallel research streams ran on 2026-05-09. Each agent verified GitHub stars, last-push dates, license, and (where applicable) live open-issue text via the GitHub API (`gh`) — those numbers are authoritative as of the date above. SaaS pricing, funding rounds, and feature claims drawn from each project's public docs and may be stale. Re-verify before citing externally.
