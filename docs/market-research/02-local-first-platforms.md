# Local-First / Self-Hosted AI Platforms (non-IDE)

*Verified 2026-05-09. GitHub repo metadata verified live via `gh api`; feature claims from each project's public docs.*

This stream excludes coding-IDE competitors (Cursor, Cline, Aider, etc.) — those are covered in [01-coding-agents.md](01-coding-agents.md). This is the broader self-hosted / local stack.

## Per-product matrix

| Product | Stars | Surface | Local models | Remote providers | Agent loop? | Tools/MCP | Memory | Multi-provider routing | License | URL |
|---|---|---|---|---|---|---|---|---|---|---|
| **Open WebUI** | 136,297 | Web (self-hosted) | Ollama-native, llama.cpp via OAI-compatible | OpenAI-compatible APIs | Limited — "tools" + "functions" + pipelines, mostly single-turn function calls; not a true autonomous loop | Custom Python "tools/functions"; MCP via OpenAPI bridge ("MCPO") | Per-conversation; basic "Memories" feature | Manual model switch | OSS (BSD-3 w/ branding clause) | openwebui.com |
| **LibreChat** | 36,785 | Web (self-hosted) | Ollama, OAI-compatible | OpenAI, Anthropic, Bedrock, Vertex, Groq, Mistral, OpenRouter | Yes — "Agents" with multi-step tool calls | OpenAPI Actions, Code Interpreter (paid), MCP servers | Per-conversation; recent "Memory" feature | Manual; per-agent endpoint | OSS (MIT) | librechat.ai |
| **Jan** | 42,443 | Desktop (Tauri-like) + local server | llama.cpp/Cortex bundled | OpenAI, Anthropic, Groq, etc. via extensions | Partial — assistants + tool extensions; not strong autonomous loop | Extensions/MCP added in 2025 | Per-thread | Manual | OSS (AGPL) | jan.ai |
| **AnythingLLM** | 59,776 | Desktop + Web (self-hosted) | Ollama, LM Studio, LocalAI, llama.cpp | OpenAI, Anthropic, Bedrock, Gemini | Yes — "@agent" mode w/ tool calls | Built-in skills (web search, scrape, SQL), custom skills, MCP servers | Per-workspace RAG + thread | Manual per-workspace | OSS (MIT, w/ commercial add-ons) | anythingllm.com |
| **LM Studio** | n/a (closed) | Desktop + local OAI-compatible server | MLX, llama.cpp native | None native (it IS the provider) | Recent: tool-use + MCP host added (2024–25) | MCP client support | Per-chat | n/a | Proprietary, free | lmstudio.ai |
| **Big-AGI** | 6,967 | Web | Ollama, LocalAI | Most major APIs | Limited — "Beam" multi-model, persona-driven; not deep tool loops | Some browser/code tools | Per-chat | Yes (Beam) | OSS (MIT) | big-agi.com |
| **Letta (ex-MemGPT)** | 22,570 | Server + SDK + ADE web | via Ollama/vLLM | OpenAI, Anthropic, etc. | Yes — stateful agent loops are the core thesis | Tool registration, MCP | **Hierarchical bounded memory (core/archival/recall)** | Yes, per-agent | OSS (Apache 2.0) + cloud | letta.com |
| **Open Interpreter** | 63,441 | CLI + local server | Ollama, llama.cpp via LiteLLM | All via LiteLLM | Yes — original "do-anything-on-your-computer" agent loop | Shell/Python execution; MCP recently | Conversation only | Yes via LiteLLM | OSS (AGPL); **last push 2025-05-27 — stalled ~12 months** | openinterpreter.com |
| **gptme** | 4,294 | CLI (+ small web UI) | Ollama, llama.cpp via OpenAI-compat | OpenAI, Anthropic, Gemini, etc. | Yes — terminal agent loop (shell, files, browser, Python, patch) | Built-in tools; MCP support added | Local conversation logs (markdown) | Yes | OSS (MIT) | gptme.org |
| **LocalAI** | 46,160 | Server (OAI-compatible) | Massive (LLM/vision/voice/image) | n/a (it's a backend) | No — inference engine, not an agent | Just an API | n/a | n/a | OSS (MIT) | localai.io |
| **GPT4All** | 77,365 | Desktop | Bundled GGUF | Limited | No agent loop; chat + LocalDocs RAG | None real | Per-chat | No | OSS (MIT) | gpt4all.io |
| **PrivateGPT** | 57,211 | Server + minimal UI | llama.cpp/Ollama | Optional | No — RAG over docs (last push 2025-05-27 — stagnating) | None | Vector index | No | OSS (Apache 2.0) | privategpt.dev |
| **Ollama** | 171,064 | CLI + local server | Native (GGUF) | n/a | No — model runtime only; tool-call API surface exposed for clients | n/a | n/a | n/a | OSS (MIT) | ollama.com |

### "Self-hosted but not really" call-outs

- **Open Interpreter** — last pushed 2025-05-27; effectively stalled ~12 months. Treat as historical.
- **Letta** has a self-hostable OSS core, but the polished ADE/agent runtime is pushed toward Letta Cloud.
- **PrivateGPT** repo also pushed 2025-05-27 — also stagnating.
- **Open WebUI** "tools" are mostly single function-call passes; not an autonomous agent harness despite marketing.
- **Big-AGI** "deploy on-prem or cloud" — local model support is one Ollama URL field, no orchestration.

## What actually overlaps with metis's positioning?

metis = local-first dev assistant with skills + bounded memory + multi-provider routing + canonical message format. Filtering:

- **Genuine overlap (agent loop + tools + persistent state on user's box):** Letta, gptme, AnythingLLM, LibreChat (Agents), Open Interpreter (historical).
- **Just chat UIs for local models (not real overlap):** Open WebUI, Jan, GPT4All, Big-AGI, LM Studio, PrivateGPT, LocalAI, Ollama.

## Closest two

1. **gptme** — closest spiritually. CLI-first local agent, markdown-logged sessions, shell/file/patch tools, multi-provider via litellm-style abstraction, MCP support, OSS. Differs from metis on: no skills standard, no bounded-memory model, no multi-surface client/server split.
2. **Letta** — closest on the *memory* axis. Hierarchical bounded memory (core/archival/recall) is exactly the design pattern metis needs to study; provider-agnostic; tool/MCP capable. Differs: server/cloud-leaning, not dev-tool focused, no skills-as-markdown concept.

(AnythingLLM is a strong third — agent skills, MCP, multi-provider, desktop+server — but it's RAG-workspace shaped, not dev-assistant shaped.)

## Things metis can learn

- **Letta's memory hierarchy** (core block / archival / recall + self-edit tools) — directly applicable to metis's bounded-memory design; their "memory blocks as editable context" pattern is shipping.
- **gptme's markdown-as-source-of-truth session log** — sessions are replayable plain files; aligns with metis's markdown+git sync model.
- **AnythingLLM's "skills" pane** — ergonomic UX for enabling/disabling agent capabilities per workspace; useful pattern for agentskills.io toggles in the TUI/dashboard.
- **LibreChat's provider abstraction + per-agent endpoint binding** — clean canonical-message bridging across OpenAI/Anthropic/Bedrock/Vertex; good prior art for metis's canonical message format and routing.
- **Open WebUI's "Pipelines" filter chain** — pre/post-LLM hooks (PII redaction, logging) — a pattern metis could expose for trace/skill middleware without baking it into the loop.
- **MCPO bridge (Open WebUI)** — exposing MCP servers as OpenAPI for non-MCP clients; useful since metis Phase 3 is MCP-centric and clients differ.
- **Jan's local-first desktop branding** — the privacy/offline narrative is winning shelf space; metis can borrow positioning language.

## Strategic takeaway

The non-IDE local-AI market is dominated by chat UIs, not dev agents. The dev-agent niche on the local-first side is thin (gptme + the dormant Open Interpreter), so metis has real room. Competitive pressure on metis comes from coding-IDE tools (separate stream), not from this category — *except* on memory architecture, where Letta is meaningfully ahead and worth tracking.
