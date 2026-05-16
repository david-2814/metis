# recursive-data-structure-traversal

`org.json` is an org-chart tree. Each node has the shape:

```json
{"name": "...", "tombstoned": <bool>, "reports": [<child nodes>]}
```

Write `solver.py` exporting a single function:

```python
def find_chain(target_name: str) -> list[str]:
    ...
```

`find_chain` reads `org.json` from the current working directory and
returns the **shortest** chain of names from the root to a node named
`target_name`. The returned list always starts with `"ROOT"` and ends
with `target_name` (inclusive of both endpoints).

Rules:

1. **Tombstoned subtrees are invisible.** If a node has
   `tombstoned: true`, neither that node nor any of its descendants
   may appear in any returned chain — treat the whole subtree as if
   it were not present. A tombstoned subtree must not be walked even
   to look for a deeper target.
2. **Shortest wins.** If `target_name` appears at more than one
   depth in the non-tombstoned subtree, return the chain to the
   shallowest occurrence. If two occurrences are at the same depth,
   return whichever appears first in left-to-right order.
3. **Not found returns the empty list `[]`.** Including the case
   where the only occurrences live in tombstoned subtrees.
4. **The root counts.** `find_chain("ROOT")` returns `["ROOT"]`.
5. Matching is case-sensitive.

`runner.py` exercises eight cases. It prints `PASS 8/8` only when
every case is correct, and otherwise prints the failing cases.
