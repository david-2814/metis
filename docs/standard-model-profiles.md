# Standard Model Profiles

**Last updated:** 2026-05-12

Curated "what this model is good for" tags that Metis ships as a default for known models. These appear in `metis chat` / `metis tui` `/models` output and on the HTTP `GET /models` response as `task_profile`.

These tags are **recommendations, not enforcement.** Routing rules (per [`routing-engine.md §5`](specs/routing-engine.md)) remain the customization layer customers use to actually shape model selection for their workload. The tags exist to:

- Make `/models` informative for new users who haven't written any rules yet.
- Give every model a starting answer to "what is this useful for?"
- Provide a writeable slot the Phase 2.5 pattern store can later populate with *learned* tags (so the same display surface scales from curated → user-customized → data-driven).

## Where the data lives

[`packages/metis-core/src/metis_core/routing/profiles.py`](../packages/metis-core/src/metis_core/routing/profiles.py) — two sources:

- `STANDARD_TASK_PROFILES: dict[str, list[str]]` — exact-match dict, keyed by canonical model id. Used for natively adapted models (Anthropic, OpenAI). Curated by hand; tested.
- `OPENROUTER_PROFILE_PATTERNS: list[tuple[re.Pattern, list[str]]]` — ordered regex patterns matched in sequence (first match wins). Used as a heuristic for OpenRouter mirrors of common upstream models.

The resolver function `standard_profile_for(model_id)` tries exact match first, then OpenRouter patterns, then returns an empty list.

[`packages/metis-core/src/metis_core/routing/registry.py`](../packages/metis-core/src/metis_core/routing/registry.py) — `ModelEntry.task_profile: tuple[str, ...]`. The registered runtime value.

[`apps/cli/src/metis_cli/runtime.py`](../apps/cli/src/metis_cli/runtime.py) — fetches the curated tags via `standard_profile_for(model_id)` at registration time.

## Vocabulary (v1 — intentionally free-form)

Lowercase, hyphen-separated, short. The list below is descriptive of the v1 set, not prescriptive — new tags are fine when they earn their keep.

| Tag | Meaning |
|---|---|
| `deep-reasoning` | Multi-step analysis, careful chains of thought. Premium tier work. |
| `architecture` | System design, structural code review, high-stakes planning. |
| `security-review` | Careful code/policy review where thoroughness matters more than speed. |
| `long-context` | Large input windows (>100k tokens). |
| `coding` | General code writing, edits, small refactors. The everyday workhorse tag. |
| `refactoring` | Code transformation — renaming, restructuring, type changes. |
| `debugging` | Narrow problem investigation; reproducing and fixing specific bugs. |
| `tool-use` | Reliable function/tool calling. Matters for agentic workflows. |
| `balanced` | General-purpose; no strong specialization. |
| `commits` | Commit message writing. Cheap, fast turnarounds. |
| `summarization` | Condensing content (PR descriptions, change summaries, long logs). |
| `quick-edits` | Small targeted changes that don't need deep context. |
| `cheap-bulk` | High-volume work where cost per turn matters more than peak quality. |

Tags that don't add information beyond `AdapterCapabilities` are intentionally absent — e.g. there is no `vision` tag because `supports_images` already says that.

## v1 assignments

| Model | Tags | Rationale |
|---|---|---|
| `anthropic:claude-opus-4-7` | `deep-reasoning`, `architecture`, `security-review`, `long-context` | Top-tier reasoning; cost justifies it for hard tasks. |
| `anthropic:claude-sonnet-4-6` | `coding`, `refactoring`, `debugging`, `tool-use`, `balanced` | Coding default; strong on tool use; reasonable price/quality. |
| `anthropic:claude-haiku-4-5` | `commits`, `summarization`, `quick-edits`, `cheap-bulk` | Fast, cheap; perfect for the high-volume long tail. |
| `openai:gpt-5` | `coding`, `tool-use`, `balanced` | Strong general-purpose; cheaper than Sonnet for many flows. |
| `openai:gpt-5-mini` | `cheap-bulk`, `summarization`, `quick-edits` | OpenAI's haiku equivalent — cheap throughput. |

## OpenRouter heuristics

OpenRouter exposes hundreds of upstream models through one provider prefix. Tagging each one by hand is unsustainable, so for OpenRouter ids we fall through to regex pattern matching after exact-match lookup. Patterns are ordered most-specific first; the first match wins.

Coverage (May 2026):

| Pattern (most-specific first) | Tags applied |
|---|---|
| `openrouter:anthropic/.*opus` | `deep-reasoning, architecture, security-review, long-context` |
| `openrouter:anthropic/.*sonnet` | `coding, refactoring, debugging, tool-use, balanced` |
| `openrouter:anthropic/.*haiku` | `commits, summarization, quick-edits, cheap-bulk` |
| `openrouter:openai/o\d+-mini` | `deep-reasoning, cheap-bulk` |
| `openrouter:openai/o\d+` | `deep-reasoning, architecture` |
| `openrouter:openai/gpt-5-mini` | `cheap-bulk, summarization, quick-edits` |
| `openrouter:openai/gpt-5-nano` | `cheap-bulk, quick-edits` |
| `openrouter:openai/gpt-5` | `coding, tool-use, balanced` |
| `openrouter:openai/gpt-4o-mini` | `cheap-bulk, quick-edits` |
| `openrouter:openai/gpt-4o` | `coding, tool-use, balanced` |
| `openrouter:openai/gpt-4-turbo` | `coding, tool-use, long-context` |
| `openrouter:openai/gpt-4` | `coding, balanced` |
| `openrouter:openai/gpt-3\.5` | `cheap-bulk, quick-edits` |
| `openrouter:google/gemini-.*-pro` | `long-context, balanced, coding` |
| `openrouter:google/gemini-.*-flash` | `cheap-bulk, quick-edits, long-context` |
| `openrouter:google/gemini-` | `balanced, long-context` |
| `openrouter:deepseek/.*coder` | `coding, cheap-bulk` |
| `openrouter:deepseek/.*r1` | `deep-reasoning, cheap-bulk` |
| `openrouter:deepseek/.*chat` | `coding, tool-use, cheap-bulk` |
| `openrouter:deepseek/` | `coding, cheap-bulk` |
| `openrouter:meta-llama/.*405b` | `balanced, long-context` |
| `openrouter:meta-llama/.*70b` | `balanced, cheap-bulk` |
| `openrouter:meta-llama/.*8b` | `cheap-bulk, quick-edits` |
| `openrouter:meta-llama/.*vision` | `balanced, long-context` |
| `openrouter:meta-llama/` | `balanced, cheap-bulk` |
| `openrouter:mistralai/codestral` | `coding` |
| `openrouter:mistralai/.*large` | `balanced, long-context` |
| `openrouter:mistralai/.*7b` | `cheap-bulk, quick-edits` |
| `openrouter:mistralai/` | `balanced` |
| `openrouter:qwen/.*coder` | `coding` |
| `openrouter:qwen/.*72b` | `balanced, long-context` |
| `openrouter:qwen/` | `balanced` |
| `openrouter:x-ai/grok` | `coding, balanced` |
| `openrouter:cohere/command-r-plus` | `balanced, long-context` |
| `openrouter:cohere/` | `balanced` |

Models that don't match any pattern get **no profile** — better to say nothing than mislead. Customers fill those in via routing rules.

**Heuristic caveats.** Pattern matching is opinion expressed as regex. If OpenRouter routes a request to a fork or fine-tune that behaves differently from the upstream model the pattern thinks it is, the tags will be slightly wrong. Three failure modes worth knowing:

1. **Vendor-renamed variants.** When DeepSeek ships `deepseek-v4-reasoning` the pattern would tag it as plain `coding` (the generic `deepseek/` fallback) rather than the `deep-reasoning` it deserves. Fix: add a more specific pattern when the model becomes notable.
2. **Drift over generations.** `gpt-3.5` is tagged `cheap-bulk` because that's what it's still useful for — but `gpt-3.5` was once everyone's default. The tags reflect the model's *current* position in the price/quality landscape, not historical role.
3. **Fine-tunes lying about their base.** `openrouter:someone/llama-3.1-70b-rude-edition` would inherit the standard 70b tags. The fine-tune may not actually be good at any of those tasks.

For tasks where the curated tag is wrong, customers should override via routing rules — see below.

## How customers customize (Layer 2)

The routing engine's yaml policy already lets customers encode "task → model" mappings:

```yaml
# ~/.metis/routing.yaml
rules:
  - name: "fast for commits"
    when:
      message_matches: "^/commit"
    use: anthropic:claude-haiku-4-5
  - name: "openrouter deepseek for SQL"
    when:
      file_extensions_in_context: [".sql"]
    use: openrouter:deepseek/deepseek-chat-v3.1
```

These are the **actual cost-shaping decisions** — the standard profiles are just a starting frame. Customers don't override the curated tags; they override the routing decisions the tags would inform. The pattern store (Phase 2.5) will eventually surface learned tags from outcomes, at which point a model can carry multiple sources of tags (curated / user / learned), distinguished in the UI.

## Adding to the curated set

When adding a new model to the standard registration list:

1. Add the canonical model id to `ANTHROPIC_MODELS` / `OPENAI_MODELS` in [`cli/runtime.py`](../apps/cli/src/metis_cli/runtime.py).
2. Add an entry to `STANDARD_TASK_PROFILES` in [`routing/profiles.py`](../packages/metis-core/src/metis_core/routing/profiles.py).
3. Pick tags from the existing vocabulary unless none fit; introducing a new tag is fine but check it isn't a synonym of an existing one.
4. Update the table above.

Tests in [`packages/metis-core/tests/routing/test_profiles.py`](../packages/metis-core/tests/routing/test_profiles.py) enforce that every curated model has a non-empty profile, tag vocabulary is consistent, and unknown models return empty profiles.

## Open questions

- **Should Phase 2.5 learned tags shadow or coexist with curated tags?** A model's display tags become a union of curated + user-via-rules + learned. The UI needs to make the source distinguishable so trust doesn't erode (a learned "deep-reasoning" tag on a model that just got lucky on three turns shouldn't look the same as a curated one).
- **Curated set portability across customers.** Right now the curated dict is baked into the code. A future config layer (`~/.metis/profiles.yaml` overlay) could let customers replace the *defaults* rather than just override them via rules. Deferred — the current rule layer covers all the customer-facing use cases.
- **Migrating from free-form to enum.** Once the vocabulary stabilizes (probably after Phase 2.5), snap to a closed enum so the dashboard / analytics can build typed views.
