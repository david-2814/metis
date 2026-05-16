# Content platform case study template

> Vertical-specific template for a paid customer whose core workflows involve
> editorial, creator, marketplace, marketing, SEO, moderation, or knowledge-base
> content. Use only numbers generated from the buyer's trace DB.

Remove this note before publication.

---

# {{Customer name or anonymous shape}} - {{one-line outcome}}

{{Two-sentence summary. Name the content workflow, the trial window, the
baseline, and the headline result.}}

**Customer:** {{public name, or "a {{N}}-person content platform"}}
**Deployment:** {{Docker compose / k8s / internal gateway}}
**Workflow:** {{editorial assist / moderation / support content / SEO / creator tooling}}
**Trial window:** {{YYYY-MM-DD}} to {{YYYY-MM-DD}}
**Headline:** {{headline result}}

---

## 1. Starting Point

{{One paragraph. What content workflow was already using LLMs? Was spend tied
to editorial, support, moderation, or product teams? What quality bar mattered:
human approval, factuality, policy compliance, tone, or throughput?}}

Details to collect:

- Content type and volume.
- Whether prompts included private customer data, policy documents, or brand
  guidelines.
- Current provider and model default.
- Existing review workflow and pass/fail criteria.

## 2. What They Wanted

{{Two or three bullets.}}

Example:

- Attribute LLM spend by editorial function or product area.
- Route routine rewrite/summarization work to a cheaper model while keeping
  policy-sensitive turns on the stronger model.
- Produce a case-study-safe artifact that does not expose private content.

## 3. Trial Design

{{Describe live traffic vs scripted fixture. For content workflows, be clear
about redaction, anonymization, and whether prompts are publishable.}}

| Area | Choice |
|---|---|
| Traffic source | {{live content workflow / anonymized fixture / both}} |
| Baseline | {{baseline_short}} |
| Quality signal | {{human editorial approval / policy rubric / LLM judge / none}} |
| Sensitive data posture | {{none / redacted / private only}} |
| Publication posture | {{public / anonymous / private}} |

For public drafts, generate the anonymized JSON companion and write the case
study from placeholders rather than raw customer identifiers.

## 4. Results

| Metric | Result |
|---|---:|
| Spend | ${{total_spend_usd}} |
| Baseline repriced spend | ${{baseline_repriced_usd}} |
| Savings vs {{baseline_short}} | {{savings_pct}} |
| LLM calls | {{llm_calls}} |
| Quality verdicts | {{quality_count}} |
| Cost / quality | {{cost_per_quality}} |

Rollup to include:

| Group | Spend | Calls | Content class |
|---|---:|---:|---|
| {{team_001_or_user_001}} | ${{...}} | {{...}} | {{moderation / editorial / support}} |
| {{team_002_or_user_002}} | ${{...}} | {{...}} | {{moderation / editorial / support}} |

## 5. Content Quality Notes

{{What happened to quality? Use the buyer's rubric language. Do not imply
editorial acceptance if the trace DB has no verdicts.}}

Example:

- Routine summarization stayed above the editorial acceptance threshold.
- Policy-sensitive moderation prompts remained pinned to the larger model.
- No quality verdicts were wired during the trial, so the public story is cost
  attribution only.

## 6. Privacy And Redaction

{{Name exactly what was removed before publication: customer names, article
titles, user handles, policy snippets, private URLs, or prompt text.}}

Public case studies should reference `metis customer-report --anonymize` output
and avoid pasting raw prompts unless the buyer approves them.

## 7. Where Metis Did Not Help

{{Credibility section. Examples: no savings on policy-critical turns, no quality
signal, short sessions where caching did not matter, or a human review queue
that remained the bottleneck.}}

## 8. Buyer Quote

> "{{Specific quote from the buyer.}}"
>
> - {{Name, title, customer or anonymous title}}

## 9. Reproduction Notes

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

## 10. Approval

- Buyer approver: {{name / title}}
- Approved numbers: {{yes/no}}
- Approved quote: {{yes/no}}
- Publication mode: {{public / anonymous / private only}}
