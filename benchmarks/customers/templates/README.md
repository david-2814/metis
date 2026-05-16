# Customer benchmark templates

Copy these files into `benchmarks/customers/{{customer_slug}}/` for a paid
customer concierge run.

- [`customer-intake.yaml`](customer-intake.yaml) captures the buyer, trial,
  consent, and publication posture.
- [`workload.yaml`](workload.yaml) is the benchmark-harness-compatible workload
  shell for a customer-specific prompt set.
- [`report-summary.md`](report-summary.md) is a short markdown summary that can
  be filled from `metis customer-report` values before it becomes a case-study
  section.

Keep placeholders wrapped in `{{...}}`. The CLI helper used by
`metis customer-report` recognizes simple placeholder names like
`{{customer_label}}`, `{{savings_pct}}`, and `{{llm_calls}}` when building
derived report snippets.
