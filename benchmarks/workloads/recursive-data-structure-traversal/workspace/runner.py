"""Test runner for the recursive-data-structure-traversal workload.

Imports solver.find_chain from solver.py, runs 8 fixed cases against
the org.json tree, prints PASS 8/8 or a per-case failure list.

Do not modify this file.
"""

from __future__ import annotations

import sys
import traceback

from solver import find_chain

CASES: list[tuple[str, str, list[str]]] = [
    # (description, target_name, expected_chain)
    (
        "shallow leaf (Hank, depth 2)",
        "Hank",
        ["ROOT", "Gina", "Hank"],
    ),
    (
        "deep leaf (Target, depth 6 via Bob/Dan/Eve)",
        "Target",
        ["ROOT", "Alice", "Bob", "Dan", "Eve", "Target"],
    ),
    (
        "root itself",
        "ROOT",
        ["ROOT"],
    ),
    (
        "very deep leaf (Deep, depth 7)",
        "Deep",
        ["ROOT", "Gina", "Iris", "Kate", "Leo", "Mary", "Deep"],
    ),
    (
        "duplicate at different depths — shortest must win",
        # DUP exists at Nora/DUP (depth 2) and at Nora/Quinn/Rita/DUP
        # (depth 4). Expected: the shallower one.
        "DUP",
        ["ROOT", "Nora", "DUP"],
    ),
    (
        "name lives ONLY in a tombstoned subtree — must return []",
        # Hidden / DeeperHidden / AlsoHidden are all under Skip
        # (tombstoned). They must not be reachable.
        "Hidden",
        [],
    ),
    (
        "name appears in both a tombstoned and a live subtree — live shorter wins",
        # Target appears at Alice/Bob/Dan/Eve/Target (depth 5, live)
        # and at Alice/Frank/Target (Frank is tombstoned). Expected:
        # the live path (the tombstoned shorter path is invisible).
        # NOTE: this is a re-check of the Target case above; included
        # explicitly to catch implementations that prune tombstoned
        # AFTER finding the shortest path overall.
        "Target",
        ["ROOT", "Alice", "Bob", "Dan", "Eve", "Target"],
    ),
    (
        "name does not exist anywhere",
        "NoSuchPerson",
        [],
    ),
]


def main() -> int:
    failures: list[str] = []
    for desc, target, expected in CASES:
        try:
            got = find_chain(target)
        except Exception as exc:  # noqa: BLE001 — runner reports any crash as a failure.
            failures.append(f"  - {desc}: target={target!r} raised {type(exc).__name__}: {exc}")
            traceback.print_exc()
            continue
        if got != expected:
            failures.append(
                f"  - {desc}: target={target!r}\n"
                f"      expected: {expected}\n"
                f"      got:      {got}"
            )

    if failures:
        print(f"FAIL {len(CASES) - len(failures)}/{len(CASES)}")
        for line in failures:
            print(line)
        return 1
    print(f"PASS {len(CASES)}/{len(CASES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
