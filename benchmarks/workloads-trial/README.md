# Trial workloads

These workloads are **not part of the project benchmark suite** under
[`benchmarks/workloads/`](../workloads/). They exist for one purpose:
let a buyer who just brought up the gateway run a single, pre-baked
workload through it and see a real cost number — without committing to
the full suite or writing their own.

## Contract

- **Runtime:** &lt; 2 minutes per workload.
- **Cost:** &lt; $0.10 per run on `anthropic:claude-haiku-4-5` (the
  default `--model haiku`). At sonnet rates, &lt; $0.50.
- **Self-contained:** the `workspace/` is a small but realistic fixture.
- **Discriminating:** uses the hybrid evaluator with `grounding_tokens`
  so the quality column reflects whether the agent grounded in the
  fixture, not whether it parroted a substring (per `RESULTS.md §A3-rev`).

## Why a separate directory

`benchmarks/workloads/` is the project-internal benchmark suite — the
six workloads we run to evidence the headline savings number in
[`docs/savings-demo.md`](../../docs/savings-demo.md). Their cost ceilings,
quality rubrics, and assertion sets are tuned to discriminate between
haiku / sonnet / no-active-model under the §A3-rev evaluator. They are
not stable consumer-facing artifacts — we change them as we learn.

This directory is the consumer-facing surface. The CLI subcommand
[`metis trial`](../../docs/operations/quickstart.md) runs whichever
workload is named here; the operations doc references this path
directly. Don't change a workload here without updating the doc.

## Adding a workload

Same schema as `benchmarks/workloads/<name>/workload.yaml` — see
[`docs/specs/benchmark.md §3.1`](../../docs/specs/benchmark.md). Keep
the runtime + cost contract tight; this is what a buyer's first
impression of "cost-per-quality" reads from.
