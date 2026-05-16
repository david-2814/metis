# Startup SaaS case study template

> Vertical-specific template for a paid startup SaaS customer. Use this when the
> buyer's workflow is product engineering, support tooling, or in-app AI
> development. Keep the draft private until the buyer approves every number and
> every quote.

Remove this note before publication.

---

# {{Customer name or anonymous shape}} - {{one-line outcome}}

{{Two-sentence summary. Name the SaaS workflow, the trial window, the
baseline, and the headline result. Example shape: "{{Customer}} routed
{{llm_calls}} engineering-assistant calls through Metis over {{window_length}}.
Cost per quality landed at {{cost_per_quality}} while preserving the support
automation acceptance bar."}}

**Customer:** {{public name, or "a {{N}}-person B2B SaaS company"}}
**Deployment:** {{Docker compose / kind / production k8s}}
**Trial window:** {{YYYY-MM-DD}} to {{YYYY-MM-DD}}
**Baseline:** {{baseline_short}}
**Headline:** {{headline cost-per-quality or attribution result}}

---

## 1. Starting Point

{{One paragraph. What did the SaaS team already have in production? Name the
dev tools, provider mix, rough monthly spend, and whether support/product/eng
teams shared keys.}}

Useful facts to collect:

- Developer count and teams in the trial.
- Current LLM spend by provider, if they can share it.
- Where the AI workflow sits: internal dev loop, support queue, product feature,
  CI automation, or data tooling.
- What "good output" meant before Metis: tests, human review, customer-support
  deflection, or accepted PRs.

## 2. What They Wanted

{{Two or three bullets. Separate must-haves from nice-to-haves.}}

Example:

- Attribute spend by product team without minting one upstream provider key per
  team.
- Keep sonnet-quality output on edge-case product tasks while routing routine
  implementation work to cheaper models.
- Produce an audit-ready usage artifact for finance and security review.

## 3. What They Ran

{{Describe the exact customer workload. Link to the private
`benchmarks/customers/{{customer_slug}}/workload.yaml` while drafting; remove
or anonymize the path before publication.}}

Methodology checklist:

- Trial ran through the transparent gateway, not a synthetic estimate.
- Report generated with `metis customer-report --customer-tier paid`.
- Public draft generated from the anonymized companion
  `metis customer-report --anonymize --format json`.
- Quality verdict source: {{pytest|human review|LLM judge|none}}.
- Buyer-approved publication posture: {{public|anonymous}}.

## 4. Numbers

| Metric | Result |
|---|---:|
| Spend | ${{total_spend_usd}} |
| Baseline repriced spend | ${{baseline_repriced_usd}} |
| Savings vs {{baseline_short}} | {{savings_pct}} |
| LLM calls | {{llm_calls}} |
| Quality verdicts | {{quality_count}} |
| Cost / quality | {{cost_per_quality}} |

Per-team rollup:

| Team | Spend | Calls | Note |
|---|---:|---:|---|
| {{team_001}} | ${{...}} | {{...}} | {{what drove spend}} |
| {{team_002}} | ${{...}} | {{...}} | {{what drove spend}} |

Source: trace snapshot at {{snapshot_window}}, rendered through
`metis customer-report`. If the public version is anonymous, use the
deterministic placeholders from the anonymized JSON report.

## 5. What Changed

{{One paragraph. Tie the result back to a SaaS operating concern: shipping
features, support load, finance visibility, or compliance review.}}

Example angles:

- Finance got per-team attribution without a provider-console export.
- The engineering team saw which workflow class needed the larger model.
- A support automation prompt had enough quality verdicts to keep running.

## 6. Where Metis Did Not Help

{{Credibility section. Name any workflow class where routing did not move the
number, where quality was unavailable, or where the team kept a pinned model.}}

## 7. Customer Quote

> "{{Specific quote from the buyer. Avoid generic praise.}}"
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
