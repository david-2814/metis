# intentionally-failing-task

Control workload for the evaluator. The workload's prompt deliberately asks
the agent to refuse, and the `evaluate.expect_substring_in_final_response`
sentinel cannot match a real response — so the heuristic judge's content
penalty + substring-missing penalty both fire and the workload scores below
0.8. This is the negative case that proves the rubric isn't pinned at 1.00.

Included in the default suite — discovery is filesystem-based, so future
benchmark runs always pick up this control case. Cost is ≤ $0.005 per run
(one short turn, no tools).

Run in isolation:

```bash
uv run python scripts/benchmark.py --workload intentionally-failing-task
```

Expected verdict: `score < 0.8` and `flags_negative` containing
`expected_substring_missing` plus either `workload_assistant_refusal_detected`
or `workload_empty_assistant_response`. See
[`packages/metis-core/tests/eval/test_judge.py::test_workload_heuristic_combined_failure_scores_below_0_8`](../../../packages/metis-core/tests/eval/test_judge.py)
for a unit-level verification that doesn't require a live API call.
