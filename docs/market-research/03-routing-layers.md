# Multi-Provider LLM Routing Layers

*Verified 2026-05-09. State verified via the GitHub API (`gh`) — stars, last-push dates, and open-issue text are current. Funding figures and SaaS pricing reflect prior knowledge.*

## Per-product scan

| Product | Category | Canonical fmt? | Routing | Cost trk | Mid-sess swap | Local | License | Stars |
|---|---|---|---|---|---|---|---|---|
| **LiteLLM** (BerriAI) | Proxy + SDK | OpenAI-shape lossy bridge; `cache_control` / thinking blocks **leak**, see issues #27512, #26916, #27469, #15601 | User rules, fallback, load-balance | Per req/provider/key/team | Yes via model-list, but state can corrupt across swaps | Yes (Ollama, vLLM) | MIT-ish (NOASSERTION) | 46.3k |
| **OpenRouter** | Aggregator/proxy | OpenAI-only wire format; native Anthropic blocks pass via `extra_body`; thinking parts often flattened | Auto fallback, "auto" model, price/latency rules | Per req | Yes (just change `model`) | No (cloud only) | Closed SaaS | n/a |
| **Portkey** | Gateway | OpenAI-shape; routing rules over conditions; tool-call shape mostly normalized | Conditional rules, fallback, A/B, load-bal | Per req/team | Yes | Limited | OSS gateway MIT + SaaS | 11.7k |
| **Helicone** | Observability + AI Gateway (newer) | Mostly pass-through proxy; minimal normalization | Caching, fallback; routing is light | Per req/user/session | Yes (passthrough) | Via base URL | Apache-2.0 + SaaS, YC W23 | 5.6k |
| **Vercel AI SDK** | TS SDK abstraction | Strong unified `ModelMessage` / parts; but reasoning/thinking **breaks across providers** (#7729, #13430, #13703 open) | None (manual) | Hooks via telemetry | Yes (swap `model`) | Via providers | Apache-2.0 | 24.1k |
| **RouteLLM** (LMSYS) | Research framework | Research only; not a serializer | Learned classifier (BERT/MF) for strong↔weak | No | N/A | N/A | Apache-2.0; **last push Aug 2024 — effectively stalled** | 4.9k |
| **Not Diamond** | Managed routing SaaS | None — wraps your existing client | Learned router as a service | Some | Yes | No | SaaS; **Python SDK repo archived Dec 2025** — pivoting or fading | 90 (archived) |
| **Martian** | Routing SaaS | None public | "Model mapping" learned router | SaaS metering | Yes | No | Closed SaaS; no public repo found | n/a |
| **Unify AI** | Router SaaS | Pass-through | Quality/cost/latency router | Yes | Yes | No | SaaS; OSS repo essentially empty (4 stars) | n/a |
| **OpenPipe** | Fine-tune + proxy | OpenAI shape; logs traffic to fine-tune | Logging-led, not routing-first | Yes | Yes | Self-host vLLM | MIT + SaaS | 2.8k |
| **Glama / Requesty** | Aggregators | Pass-through OpenAI-style | Catalog + fallback | Yes | Yes | No | SaaS | n/a |

URLs: litellm.ai, openrouter.ai, portkey.ai, helicone.ai, ai-sdk.dev, github.com/lm-sys/RouteLLM, notdiamond.ai, withmartian.com, unify.ai, openpipe.ai.

## Tool-use & block fidelity — the critical finding

**LiteLLM does NOT solve metis's canonical-format problem.** Open issues from the last few weeks prove it:

- **#27512** (2026-05-09): Anthropic Messages retry **drops thinking blocks**.
- **#27469** (2026-05-08): tool_call `function.arguments` **lost** during OpenAI→Anthropic response conversion (regression in v1.83.7).
- **#26916**, **#24985**: Anthropic↔OpenAI bridge **collapses thinking blocks to text** in multi-turn.
- **#15601**: Anthropic thinking blocks **missing on requests with tool calls**.
- **#26625**, **#20418**, **#20485**: Bedrock + Vertex prompt-caching `cache_control` placement broken.
- **#26937**: Citations on Bedrock Converse — not supported.

Vercel AI SDK has the best-shaped abstraction in TS but the same class of bugs — reasoning parts orphaned in `pruneMessages` (#13430), openai-compatible thinking validation (#13703) — both still open, March–April 2026. Anthropic prompt caching with `streamText` was broken into late 2025 (#11077 closed Dec 2025).

Bottom line: **every router has the canonical-format bug metis was built to avoid**, and they fix it by chasing tickets, not by design.

## Recommendation

**Do not adopt LiteLLM as a dependency for the message-prep layer.** It's an OpenAI-shape bridge with a bug-of-the-week problem on exactly the surfaces metis treats as load-bearing (tool_use round-trip, `cache_control` placement, thinking blocks across turns, citations). Keeping metis's own typed adapters per provider — and treating Anthropic blocks as the authoritative internal shape — is correct.

**Consider LiteLLM as an *optional* egress proxy** for users who want a single API key endpoint or org-level cost dashboards, sitting *below* metis's adapter (metis emits provider-native, LiteLLM proxies). Same for Portkey/Helicone.

**OpenRouter / Glama / Requesty** are useful as *catalog* sources (pricing, model availability) but should not own serialization.

## What's actually novel in metis

1. **Canonical message format with lossless round-trip of Anthropic-native blocks** (cache_control, thinking, citations, tool_use) and per-provider serializers. No commodity router does this correctly today.
2. **`delegate(tier, task, context)` as a tool** — planner/worker delegation expressed inside the agent loop, with cost attributed to *role* not just model. RouteLLM is offline classifier research; Not Diamond/Martian are external services that don't see agent structure. Nobody ships agent-internal delegation.
3. **Replays surviving provider changes** — depends on (1). LiteLLM's logs are wire-format, so replays die when a provider changes shape.
4. **Local-first routing** with the same canonical format — none of the SaaS routers prioritize this.

Already commoditized: provider catalogs, fallback on 5xx, simple cost dashboards, latency-based routing, OpenAI-shape proxying.

## Lunch-eating risk

**Highest:** Vercel AI SDK + AI Gateway adding an agent loop with tools. They have 24k stars, the best TS abstraction, and Vercel has been visibly pushing into agents. If they ship a typed `Agent` with delegation primitives they'd compete with metis on the SDK side — but they have local-first as a non-priority and their own thinking-block bugs.

**Medium:** LiteLLM adding a managed agent runtime on top of the proxy — they have the distribution (46k stars) but their codebase is a serialization minefield; an agent layer there would inherit the bugs.

**Low:** OpenRouter, Portkey, Helicone, Not Diamond, Martian, Unify — incentive structure (per-token margin or routing fees) discourages becoming an opinionated agent. Not Diamond's archived Python SDK and RouteLLM's stalled repo (no push since Aug 2024) suggest the standalone-router thesis is weakening.

## Verdict

metis's canonical-format moat is real and underexploited by the routing layer. The risk is not a router becoming an agent — it's an *agent SDK* (Vercel AI) becoming a better abstraction. Keep investing in lossless block fidelity and the `delegate` primitive; those are the two things no commodity router will replicate without a redesign.
