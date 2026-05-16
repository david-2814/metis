# Dev-tools company case study template

> Vertical-specific template for a paid customer whose users are developers:
> IDE extensions, CI/CD products, code-review tools, observability platforms,
> internal platform teams, or SDK-heavy engineering organizations.

Remove this note before publication.

---

# {{Customer name or anonymous shape}} - {{one-line outcome}}

{{Two-sentence summary. Name the developer workflow, the tool surface, the time
window, and the cost-per-quality or attribution outcome.}}

**Customer:** {{public name, or "a {{N}}-developer dev-tools company"}}
**Deployment:** {{local docker / k8s / internal platform cluster}}
**Client mix:** {{Claude Code / Cursor / SDK / CI bots}}
**Trial window:** {{YYYY-MM-DD}} to {{YYYY-MM-DD}}
**Headline:** {{headline result}}

---

## 1. Starting Point

{{One paragraph. Describe the developer workflow and why ordinary provider
console reporting was not enough.}}

Details to capture:

- Which clients were routed through the gateway.
- Whether CI or shared bots used a single key.
- Whether prompts involved code edits, code review, test generation, incident
  triage, docs, or migration work.
- Any known hard prompt classes where the team expected sonnet/opus to matter.

## 2. What They Needed To Prove

{{Two or three bullets.}}

Example:

- Per-user and per-team cost attribution across mixed developer clients.
- Confidence that the gateway preserved provider-native tool-use behavior.
- A cost-per-quality comparison between pinned baseline and routed traffic.

## 3. Trial Design

{{Describe the run. Be explicit about whether this was live developer traffic,
scripted benchmark prompts, or both.}}

| Area | Choice |
|---|---|
| Traffic source | {{live dev traffic / benchmark fixture / both}} |
| Baseline | {{baseline_short}} |
| Quality signal | {{pytest / code-review rubric / LLM judge / none}} |
| Attribution tags | {{user_id policy}}, {{team_id policy}} |
| Publication posture | {{public / anonymous / private}} |

Use `benchmarks/customers/templates/workload.yaml` for scripted runs. Keep the
customer-specific workload private unless the buyer approves publication.

## 4. Results

| Metric | Result |
|---|---:|
| Spend | ${{total_spend_usd}} |
| Baseline repriced spend | ${{baseline_repriced_usd}} |
| Savings vs {{baseline_short}} | {{savings_pct}} |
| LLM calls | {{llm_calls}} |
| Quality verdicts | {{quality_count}} |
| Cost / quality | {{cost_per_quality}} |

Per-user rollup:

| User placeholder | Spend | Calls | Note |
|---|---:|---:|---|
| {{user_001}} | ${{...}} | {{...}} | {{workflow shape}} |
| {{user_002}} | ${{...}} | {{...}} | {{workflow shape}} |

If the public version is anonymous, use the deterministic placeholders from
`metis customer-report --anonymize`.

## 5. Developer Experience Notes

{{What changed for the engineers? Keep this concrete: env var flip, no client
code changes, visible latency impact, model override behavior, or failed setup
step.}}

Example:

- Developers kept the same Claude Code / Cursor workflow; only the base URL and
  key changed.
- CI bot traffic was separated from human developer traffic after the day-3
  check-in exposed a shared-key outlier.

## 6. Where Metis Was Not The Right Lever

{{Name the miss. For dev-tools buyers, credible misses include: every code path
needed the same larger model, no usable quality verdicts, or a short-session
workflow where caching did not pay off.}}

## 7. Buyer Quote

> "{{Specific quote. Prefer an operational observation over praise.}}"
>
> - {{Name, title, customer or anonymous title}}

## 8. Reproduction Notes

```bash
metis customer-report \
  --workspace /workspace \
  --customer-tier paid \
  --since "{{window_start_iso}}" \
  --until "{{window_end_iso}}" \
  --db-path benchmarks/customers/{{customer_slug}}/snapshots/metis.db \
  --out /tmp/{{customer_slug}}-buyer.html

metis customer-report \
  --workspace /workspace \
  --customer-tier paid \
  --since "{{window_start_iso}}" \
  --until "{{window_end_iso}}" \
  --db-path benchmarks/customers/{{customer_slug}}/snapshots/metis.db \
  --format json \
  --anonymize \
  --out /tmp/{{customer_slug}}-anonymized.json
```

## 9. Approval

- Buyer approver: {{name / title}}
- Approved numbers: {{yes/no}}
- Approved quote: {{yes/no}}
- Publication mode: {{public / anonymous / private only}}
