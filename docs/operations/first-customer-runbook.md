# First customer runbook

> Internal operator checklist for the first paid customer. This starts after
> the buyer says "yes" in the concierge flow and ends with a buyer-approved
> report or case-study draft. It pairs with
> [`concierge-onboarding.md`](concierge-onboarding.md), which owns the 7-day
> trial cadence.

**Audience:** the person running the first paid customer by hand.

**Rule:** numbers come from the trace DB. Notes may explain the story; they do
not create the numbers.

---

## 0. Confirm The Publication Posture

Before creating artifacts, ask which mode the buyer is willing to consider:

| Mode | What can leave the private customer folder |
|---|---|
| `private` | Buyer report only; no public case study |
| `anonymous_ok` | Anonymized metrics and shape description |
| `public_ok` | Named case study after explicit sign-off |

Record the answer in
`benchmarks/customers/{{customer_slug}}/intake.yaml`. If the answer is not
written down, treat the customer as `private`.

## 1. Create The Local Artifact Folder

```bash
mkdir -p benchmarks/customers/{{customer_slug}}/{workspace,snapshots,reports}
cp benchmarks/customers/templates/customer-intake.yaml \
   benchmarks/customers/{{customer_slug}}/intake.yaml
cp benchmarks/customers/templates/workload.yaml \
   benchmarks/customers/{{customer_slug}}/workload.yaml
cp benchmarks/customers/templates/report-summary.md \
   benchmarks/customers/{{customer_slug}}/report-summary.md
```

Fill `intake.yaml` first. The `.gitignore` in `benchmarks/customers/` ignores
the customer folder by default; do not override it for private artifacts.

## 2. Snapshot The Trace DB

Use the buyer-approved snapshot path. If you are copying from a live gateway
host, take the snapshot before generating reports so day-7 numbers are stable.

```bash
cp /path/to/metis.db benchmarks/customers/{{customer_slug}}/snapshots/metis.db
```

If the source DB is still being written, use the gateway's normal backup path
instead of a raw copy.

## 3. Generate The Buyer Report

```bash
metis customer-report \
  --workspace /workspace \
  --customer-label "{{customer_name}}" \
  --customer-tier paid \
  --since "{{window_start_iso}}" \
  --until "{{window_end_iso}}" \
  --db-path benchmarks/customers/{{customer_slug}}/snapshots/metis.db \
  --out benchmarks/customers/{{customer_slug}}/reports/day7-buyer.html
```

Open the HTML locally and verify:

- Customer label and tier are correct.
- Window start/end match the agreement.
- Spend and call count are non-zero.
- `rows_missing_from_price_table` warning is absent, or named in the caveats.
- Quality verdict count matches the buyer's quality-signal story.

## 4. Generate The Anonymized Companion

This is the artifact you can safely use while drafting anonymous or public case
studies. It preserves metrics and timestamps but replaces customer label,
workspace path, DB path, gateway keys, users, and teams with deterministic
placeholders.

```bash
metis customer-report \
  --workspace /workspace \
  --customer-label "{{customer_name}}" \
  --customer-tier paid \
  --since "{{window_start_iso}}" \
  --until "{{window_end_iso}}" \
  --db-path benchmarks/customers/{{customer_slug}}/snapshots/metis.db \
  --format json \
  --anonymize \
  --out benchmarks/customers/{{customer_slug}}/reports/day7-anonymized.json
```

Smoke-check that the JSON does not include the customer name, real workspace
path, gateway key ids, user ids, or team ids.

## 5. Pick The Right Case-Study Template

Choose the template by buyer shape:

| Buyer shape | Template |
|---|---|
| Product/SaaS engineering, support automation, in-app AI | [`case-study-startup-saas-template.md`](../sales/case-study-startup-saas-template.md) |
| Developer tools, internal platform, CI/code-review workflows | [`case-study-dev-tools-company-template.md`](../sales/case-study-dev-tools-company-template.md) |
| Editorial, marketplace, creator, support content, moderation | [`case-study-content-platform-template.md`](../sales/case-study-content-platform-template.md) |

Draft into the ignored customer folder first:

```bash
cp docs/sales/case-study-startup-saas-template.md \
   benchmarks/customers/{{customer_slug}}/reports/case-study-draft.md
```

Replace placeholders from the anonymized JSON and operator notes. Keep every
claim traceable to either `day7-buyer.html`, `day7-anonymized.json`, or a named
buyer quote.

## 6. Buyer Review

Send the buyer:

- `day7-buyer.html`
- the case-study draft only if they chose `anonymous_ok` or `public_ok`

Ask them to approve four things separately:

- Numbers
- Quote
- Company naming or anonymous shape
- Caveats / "where it did not help"

No approval, no publication.

## 7. Publication Gate

Only after explicit approval:

1. Move the approved case study from the ignored customer folder into
   `docs/sales/case-study-{{customer_slug}}.md`.
2. Keep raw snapshots, reports, and notes ignored under `benchmarks/customers/`.
3. If a change-log entry is needed, draft it for the owner instead of editing
   broad project docs during the customer run.

## 8. Failure Modes

- **No quality verdicts:** publish attribution/cost only. Do not imply quality
  stayed flat.
- **Single shared key:** per-team rollup may be empty. Say that plainly and use
  per-key or total-spend framing.
- **Rows missing from price table:** either re-run with a supported baseline or
  include the warning in caveats.
- **Buyer wants private-only:** stop at the buyer report. The private run still
  helped the product; it does not need to become a public asset.

## 9. Final Operator Checklist

- [ ] `intake.yaml` filled and publication posture recorded.
- [ ] Trace snapshot stored under `benchmarks/customers/{{customer_slug}}/snapshots/`.
- [ ] Buyer report generated from the snapshot.
- [ ] Anonymized JSON generated with `--anonymize`.
- [ ] Case-study template chosen, if publication is allowed.
- [ ] Buyer approved numbers and quote before anything public moved into docs.
- [ ] `git status --short --ignored benchmarks/customers` confirms private
      customer artifacts are ignored.
