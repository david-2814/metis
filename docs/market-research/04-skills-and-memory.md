# Skills, Rules, and Memory Ecosystems

*Verified 2026-05-09. agentskills.io spec verified directly; comparative tables drawn from each project's docs and verified GitHub state.*

## agentskills.io: real or aspirational?

**Real and surprisingly broad.** Verified spec at `https://agentskills.io/specification`. **Originated by Anthropic, released as an open standard**, governed via the `agentskills/agentskills` GitHub org and a Discord.

The spec is minimal and stable: a `SKILL.md` with YAML frontmatter (`name` ≤64 chars, `description` ≤1024 chars, optional `license` / `compatibility` / `metadata` / `allowed-tools`), plus optional `scripts/`, `references/`, `assets/` dirs. Loading is **progressive disclosure** (~100 token metadata at startup, full body on activation, resources on demand). A `skills-ref` validator is published.

**Adoption is wide and real**, not a logo wall. Listed implementers include Anthropic Claude / Claude Code, **OpenAI Codex, Google Gemini CLI, GitHub Copilot, VS Code, Cursor, JetBrains Junie, OpenHands, OpenCode, Goose (Block), Roo Code, Mistral Vibe, Databricks Genie, Snowflake Cortex Code, Letta, Kiro, Factory, Amp, Laravel Boost, Spring AI, Firebender, TRAE (ByteDance), Qodo**, plus smaller agents (Mux, Emdash, Ona, Workshop, fast-agent, nanobot, pi, VT Code, Autohand, Agentman, Command Code, Piebald, Google AI Edge Gallery). That's a near-complete map of the coding-agent space.

**For metis, betting on this format is the right call** — it's the de facto interop layer.

What's *not* there: a canonical marketplace/registry. Discovery still happens via GitHub repos and per-vendor catalogs (Anthropic's `anthropics/skills`, vendor-specific dirs). **Marketplace is the open lane.**

## Skills / rules formats — comparative table

| Tool | Format | Portable? | Bounded? | Curation | License/$ | Adoption |
|---|---|---|---|---|---|---|
| **agentskills.io** SKILL.md | MD + YAML frontmatter, dir bundle | Yes (the whole point) | Per-skill, recommended <500 lines | Author-curated | Open spec, Anthropic-originated | ~35+ implementers verified May 2026 |
| **Claude Code** `.claude/skills`, `CLAUDE.md`, `.claude/agents` | MD files | Skills now = agentskills format; CLAUDE.md is Claude-specific but copy/pasteable | CLAUDE.md unbounded (user problem) | User | Proprietary client, free tier | Tens of millions of devs via Claude |
| **Cursor Rules** (`.cursor/rules/*.mdc`) | MDC (frontmatter + MD), scoped globs | Cursor-specific syntax; legacy `.cursorrules` deprecated | No size limit | User | Proprietary | Largest IDE-AI install base; cursor.directory community rules hub |
| **Cline** `.clinerules/` | MD files in dir | Cline-specific, MD is portable | None | User | Apache-2.0 client | Popular VS Code extension |
| **Continue.dev** `config.yaml` + assistants | YAML config, MD blocks | Config-portable across Continue | None | User | Apache-2.0 | Mid-tier |
| **Aider** `CONVENTIONS.md` | Plain MD | Fully portable | None | User | Apache-2.0 | Niche but loyal |
| **Copilot** `.github/copilot-instructions.md` + `*.instructions.md` | MD with applyTo globs | MD portable, schema GH-specific | None | User | Proprietary | GitHub-scale; **also now ships agentskills support** |
| **Custom GPTs / Projects** | Proprietary system prompt + files | Locked to OpenAI | 8KB instructions cap | User | Proprietary | GPT Store launched 2024; **largely a graveyard** |
| **OpenAI Assistants API** | API-set instructions string | API-bound | Token cap | Programmatic | Pay-per-use | Being deprecated in favor of Responses / Codex skills |

**Closest competitor to metis's framing** ("portable skills + learning agent"): **Goose (Block)** and **Letta** combine agentskills support with persistent memory — Goose has skill-running + recipes, Letta has stateful memory blocks plus skills support. Neither does **task fingerprint embeddings → cold-start routing**, which is where metis differentiates. Cursor + Memories does ad-hoc memory but no fingerprint routing. **Command Code** advertises "continuously learns your coding taste" with reinforcement learning — closest in *narrative*, opaque in *substance*.

## Memory systems — comparative table

| System | What | Format | Portable | Bounded? | Curation | License/$ |
|---|---|---|---|---|---|---|
| **Letta (MemGPT)** | Stateful agent runtime with **bounded core memory blocks** (persona/human) + archival vector store | DB-backed (Postgres) | Export via API | **Yes, core blocks have char limits**; archival unbounded | **Agent self-edits via tools** (closest analog to metis MEMORY.md/USER.md) | Apache-2.0 + paid cloud; Series A funded |
| **mem0** | "Memory layer" — extracts facts from conversations into vector + graph store | DB-backed (Qdrant/Neo4j) | API export | **Unbounded vector store**; "smart" dedup/decay claimed | LLM-curated facts, append-heavy | Apache-2.0 + paid cloud |
| **Zep / Graphiti** | Temporal **knowledge graph** memory | DB-backed (Neo4j-like) | API export | Unbounded graph | Auto-extracted entities | Apache-2.0 (Graphiti) + paid Zep cloud |
| **LangMem / LangChain memory** | Abstractions: buffer, summary, vector | Library, BYO store | Code-portable | Whatever store you pick — usually unbounded | Mostly append/summarize | MIT |
| **Anthropic memory tool** (Claude) | Tool letting Claude read/write a `memories/` directory of MD files | **File-based, MD** | Yes — it's just files | User-set; agent self-curates | **Agent-curated** | Free with API |
| **ChatGPT memory** | Hidden bullet list injected into system prompt | Proprietary | No (export to text only) | Soft cap (~few KB shown to user) | Mixed; user can edit | Consumer feature |
| **Cognee** | Knowledge-graph memory with ECL pipeline | DB-backed | API | Unbounded | Auto-extract | Apache-2.0 |
| **AstraDB / Cassandra memory** | Vector store marketed as memory | DB | DB-portable | Unbounded | Append-only | DataStax commercial |

## Bounded-memory positioning

**This is genuinely contrarian.** The dominant pattern is **"vector-store-everything-and-RAG-it-back"** — mem0, Zep, Cognee, most LangChain memory. They call eviction a *bug*. Only two systems treat eviction as a *feature*:

1. **Letta's core blocks** — bounded character-limited blocks the agent edits via tools, with overflow to archival. This is the most direct prior art for metis's MEMORY.md/USER.md and validates the design with a Series-A-funded company.
2. **Anthropic's memory tool** — file-based, agent-curated, but no enforced size cap; metis's **explicit ~2KB / ~1.5KB budgets are tighter and more opinionated**.

metis's differentiation: **per-workspace, dual-file (workspace + user), file-based-and-grep-able, hard-budgeted**. The "file-based and portable" angle aligns with where Anthropic moved post-2025; the "hard budget" angle aligns with Letta. Combining both in a local-first coding agent is a defensible niche.

## Marketplace prior art

- **Custom GPT Store (Jan 2024)**: launched with fanfare, hit ~3M GPTs, but **revenue sharing was tiny, discovery is broken, and most GPTs are abandoned thin wrappers**. Cautionary tale.
- **Cursor Directory** (`cursor.directory`): community rules hub, healthy traffic, but **no monetization, no quality bar, lots of dead/duplicate rules**.
- **Anthropic's `anthropics/skills` repo**: curated handful of high-quality skills; not a true marketplace.

**No one has shipped a successful paid agent-skills marketplace yet.** Open lane for metis, but the lessons are: (1) curation > volume, (2) execution sandboxing/trust is the hard part for `scripts/`-bearing skills, (3) discovery without quality signals = dead store.

## Bottom line for metis

- **agentskills.io is the right standard to bet on** — verified, Anthropic-originated, ~35 implementers including OpenAI, Google, GitHub, JetBrains.
- **FTS5 on-demand search is uncommon and useful** — most clients linearly scan all skill metadata.
- **Bounded curated memory has exactly one well-funded peer (Letta)**; everyone else is unbounded vector slop. Lean into "eviction is a feature."
- **Task-fingerprint cold-start routing has no direct competitor** — closest narrative is Command Code's "taste-learning," substance unclear.
- **Marketplace: viable but only if you solve curation + sandboxed-script trust**, both of which the GPT Store failed at.

**Risks:** Cursor / Claude Code / Copilot can ship local-first equivalents of bounded memory in a quarter; metis's moat is execution speed + opinionated defaults + the FTS5/fingerprint stack working together, not any single piece.

## Sources

- [Agent Skills Overview](https://agentskills.io)
- [Agent Skills Specification](https://agentskills.io/specification)
