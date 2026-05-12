# Coding Agents

*Verified 2026-05-09. GitHub stars, licenses, and last-push dates verified via `gh` API; pricing/feature claims from each project's public docs.*

## Competitor table

| Product | Surface | Providers / Local | Memory & Skills | Mid-session swap / routing | License & Price | Stars | URL |
|---|---|---|---|---|---|---|---|
| **Claude Code** | CLI/TUI | Anthropic only (+ Bedrock/Vertex passthrough). No local. | `CLAUDE.md`, slash-commands, hooks, Skills (filesystem-loaded MD bundles) | `/model` swap; no auto-routing | Proprietary, requires Anthropic login; usage-billed | 121,976 | github.com/anthropics/claude-code |
| **OpenAI Codex CLI** | CLI/TUI | OpenAI primarily; some BYO | `AGENTS.md` | Model flag; no auto-routing | Apache-2.0 wrapper, OpenAI key required | 81,340 | github.com/openai/codex |
| **Gemini CLI** | CLI/TUI | Google Gemini; OpenAI-compatible endpoints | `GEMINI.md` | Manual flag | Apache-2.0; free tier via Google login | 103,520 | github.com/google-gemini/gemini-cli |
| **Cline** | VS Code ext | Anthropic, OpenAI, Bedrock, Vertex, Ollama, OpenRouter, LM Studio | `.clinerules/`, Memory Bank pattern (community) | Per-mode model picker; "Plan/Act" two-model split | Apache-2.0; BYO key | 61,558 | github.com/cline/cline |
| **Roo Code** | VS Code ext (Cline fork) | Same as Cline + custom modes | `.roorules`, Custom Modes, Boomerang/orchestrator delegation | Yes — *per-mode* model assignment, orchestrator delegates subtasks | Apache-2.0; BYO key | 23,959 | github.com/RooCodeInc/Roo-Code |
| **Aider** | CLI | LiteLLM-backed → ~all providers; Ollama yes | `CONVENTIONS.md`, repo map, chat history | `/model` swap; "architect+editor" two-tier (planner + cheaper applier) | Apache-2.0; BYO key | 44,575 | github.com/Aider-AI/aider |
| **Goose (Block)** | CLI + desktop (Tauri) | Anthropic, OpenAI, Google, Ollama, Databricks, Bedrock, OpenRouter | `.goosehints`, Recipes, MCP extensions | "Lead/worker" model split; manual swap | Apache-2.0; BYO key | 44,829 | github.com/block/goose |
| **OpenCode (sst)** | CLI/TUI + client/server | 75+ providers via Models.dev; Ollama/LM Studio | `AGENTS.md`, agents folder | Yes — explicit per-agent model, server architecture | MIT; BYO key | 157,462 | github.com/sst/opencode |
| **Crush (Charmbracelet)** | TUI | Anthropic, OpenAI, Groq, Ollama, OpenRouter | `CRUSH.md` | Manual switch | FSL-1.1-MIT; BYO key | 24,056 | github.com/charmbracelet/crush |
| **Continue.dev** | VS Code/JetBrains ext + CLI | Broad incl. Ollama; "Hub" for sharing configs | `rules/`, blocks, `config.yaml` | Multi-model (chat/edit/autocomplete roles) | Apache-2.0; free OSS, paid Hub/Enterprise | 33,057 | github.com/continuedev/continue |
| **Plandex** | CLI/TUI + server | OpenAI, Anthropic, OpenRouter; Ollama via OR | Plan branches, context layers | Built-in "model packs" mapping tiers → roles | MIT; OSS + cloud tier (last push Oct 2025 — slowing) | 15,344 | github.com/plandex-ai/plandex |
| **gptme** | CLI/TUI | OpenAI, Anthropic, OpenRouter, local (llama.cpp/Ollama) | Persistent log, tools as Python | Manual | MIT; BYO key | 4,294 | github.com/gptme/gptme |
| **Open Interpreter** | CLI/REPL | Multi via LiteLLM; local OK | Profiles | Manual | AGPL-3.0; BYO key (last push 2025-05-27 — stalled) | 63,441 | github.com/OpenInterpreter/open-interpreter |
| **Tabby** | Self-hosted server + IDE plugins | Local-first GPU inference; cloud optional | Repo indexing | N/A (completion-centric) | Source-available; free self-host + paid tiers | 33,491 | github.com/TabbyML/tabby |
| **Zed (Zed AI)** | Standalone IDE | Anthropic, OpenAI, Ollama, OpenRouter | Slash-commands, rules | Per-thread model | GPL/AGPL mix; freemium hosted models | 82,308 | github.com/zed-industries/zed |
| **Cursor** | Standalone IDE (VSCode fork) | Anthropic, OpenAI, Gemini, xAI; **no Ollama** | `.cursorrules`, Memories, AGENTS.md | Yes (Auto-route + manual) | Proprietary; freemium ($20/mo Pro) | n/a (closed) | cursor.com |
| **Windsurf (Codeium)** | Standalone IDE | Hosted models; limited BYO | Memories, rules | Cascade auto-routes | Proprietary; freemium (Cognition acquired 2024) | n/a | windsurf.com |
| **GitHub Copilot Workspace** | Web + IDE | OpenAI/Anthropic via GH | Repo-bound spec/plan/impl flow | Model picker (added 2024+) | Proprietary; $10–39/mo | n/a | github.com/features/copilot |
| **JetBrains AI Assistant / Junie** | JetBrains IDEs | OpenAI, Anthropic, Google, local via LM Studio/Ollama | Project rules | Yes | Proprietary; subscription | n/a | jetbrains.com/ai |
| **Amazon Q Developer** | IDE + CLI | Bedrock (Claude/Nova) | Q rules | Limited | Proprietary; freemium | n/a | aws.amazon.com/q/developer |
| **Tabnine** | IDE plugins | Hosted + on-prem; local "Protected" model | Personalization on team code | Limited | Proprietary; freemium + Enterprise | n/a | tabnine.com |

### Marketing claims that don't survive scrutiny

- **Cursor** markets "use any model" but does not support Ollama / local models.
- **Windsurf** is fully cloud after the 2024 Cognition deal, despite "local indexing" marketing.
- **Goose** and **OpenCode** are the most legitimately provider-agnostic OSS agents.
- "Multi-provider" in many smaller tools (gptme, Plandex) is just LiteLLM/OpenRouter passthrough.

## Closest 3 competitors to metis

1. **OpenCode (sst)** — 157k stars, MIT, already a server/client architecture with TUI clients, multi-provider via Models.dev, mid-session swap. The most direct architectural twin and the biggest threat to metis's "local server + thin clients" pitch.
2. **Goose (Block)** — Tauri desktop + CLI, true multi-provider including Ollama, Recipes (skill-like), lead/worker model split. Block's funding gives distribution muscle.
3. **Aider** — CLI not server/client, but its architect+editor two-tier routing, `CONVENTIONS.md`, and `/model` swap are the closest *philosophical* match to metis's layered routing + skills story.

**Roo Code** is a near-fourth: per-mode model assignment + orchestrator delegation is exactly metis's "agent-decided delegation across tiers."

## Genuine gap metis can own

- **Bounded, structured memory with explicit byte budgets** (~2KB MEMORY.md, ~1.5KB USER.md). Every competitor uses unbounded markdown (`CLAUDE.md`, `AGENTS.md`, `.clinerules`) that silently bloats context. A *budgeted* memory contract is novel.
- **Pattern learning from past tasks → accumulated skills** as first-class artifacts. Goose Recipes and Claude Skills are author-written; metis claims auto-derived skills, which nobody ships well.
- **Three-tier routing (manual → YAML rules → agent-delegated)** is more structured than Aider's two-tier or Roo's mode picker.
- **Per-turn cost tracking** as a core UX surface (some have it as logs; few make it primary).
- **Sync via plain git remote** for memory/skills — most tools either don't sync or use a proprietary cloud (Cursor, Continue Hub).

## Already crowded / me-too risk

- **"TUI multi-provider agent with markdown rules"** is *saturated*: Crush, OpenCode, Aider, gptme, Goose CLI, Codex, Gemini CLI, Claude Code all overlap. Yet-another TUI alone is not a wedge.
- **Server/client split** is no longer differentiating after OpenCode's 157k-star traction.
- **Provider-agnostic adapters** is table stakes; "Anthropic + OpenAI first, Ollama later" is *behind* the field on day one — Cline/Roo/Goose/OpenCode already ship Ollama.
- **MCP support** is implicit table stakes by 2026; not mentioned in metis's pitch — a gap to close, not a differentiator to claim.

## Bottom line

metis's defensible wedge is the *memory discipline + auto-skill accretion + cost-aware tiered routing* triangle, delivered through a server that any client (TUI today, Tauri/web later) can attach to. The architecture pitch alone won't differentiate post-OpenCode; the learning/memory mechanics have to be the headline.
