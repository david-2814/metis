# Benchmarks

Reproducible workload suite for the savings counterfactual. Specified in
[`docs/specs/benchmark.md`](../docs/specs/benchmark.md); driven by
[`scripts/benchmark.py`](../scripts/benchmark.py).

## Quick start

```bash
# Full suite, default actual=haiku / baseline=sonnet, fresh DB.
uv run python scripts/benchmark.py

# Single workload smoke (~$0.05-0.20).
uv run python scripts/benchmark.py --workload fix-a-bug-small

# View the dashboard against the same DB the run wrote.
uv run metis serve $(pwd) --db-path benchmarks/.runs/benchmark-<UTC-ts>.db
open http://127.0.0.1:8421/dashboard
```

The headline `savings_pct` printed by the script is the same number the
dashboard renders on `/analytics/savings` against that DB — the script and
the HTTP handler delegate to the same `AnalyticsStore.savings()` method.

## Layout

```
benchmarks/
├── README.md
├── .runs/                    # generated trace DBs + JSON reports (gitignored)
└── workloads/
    ├── fix-a-bug-small/
    │   ├── workload.yaml
    │   └── workspace/        # copied to a tempdir per run; never mutated in-tree
    ├── write-a-doc-from-notes/
    │   └── ...
    └── multi-turn-refactor/
        └── ...
```

## Cost expectations (per [`benchmark.md §5`](../docs/specs/benchmark.md#5-cost-budget))

| Run mode                          | Actual cost (real API spend) |
|-----------------------------------|-------------------------------|
| Single workload (smoke)           | ~$0.05–0.20                   |
| Full suite (3 workloads)          | ~$0.30–1.00                   |
| Full suite at `--model sonnet`    | ~$1.00–3.00                   |
| Full suite at `--model opus`      | ~$3.00–5.00                   |

The baseline never makes API calls — it's a re-pricing of the actual run's
recorded token counts under the baseline model's `PriceTable` rates.
