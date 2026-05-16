# Customer benchmarks

Private benchmark workspaces for paid-customer concierge runs.

This directory is intentionally **gitignored by default**. Keep real customer
inputs, trace snapshots, generated reports, evaluator rubrics, and case-study
drafts local unless the buyer explicitly approves publication. The only tracked
files here are the reusable templates under [`templates/`](templates/).

## Suggested layout for a customer run

```text
benchmarks/customers/
+-- {{customer_slug}}/
    +-- intake.yaml
    +-- workload.yaml
    +-- workspace/
    +-- snapshots/
    |   +-- metis.db
    +-- reports/
    |   +-- day5-internal.html
    |   +-- day7-buyer.html
    |   +-- day7-anonymized.json
    +-- notes.md
```

Use the templates as starting points:

```bash
mkdir -p benchmarks/customers/{{customer_slug}}/{workspace,snapshots,reports}
cp benchmarks/customers/templates/customer-intake.yaml \
   benchmarks/customers/{{customer_slug}}/intake.yaml
cp benchmarks/customers/templates/workload.yaml \
   benchmarks/customers/{{customer_slug}}/workload.yaml
cp benchmarks/customers/templates/report-summary.md \
   benchmarks/customers/{{customer_slug}}/report-summary.md
```

## Reporting contract

Buyer-facing reports should come from the trace DB snapshot, not notes:

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

If a report is going into a public case-study draft, generate an anonymized
companion:

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

Do not commit the customer directory. The `.gitignore` here exists to make that
the default even when the rest of the repo is being staged.
