"""Test runner for the phone-number regex task.

Loads `test_cases.txt`, imports `PHONE_REGEX` from `solution.py`, and runs
`re.fullmatch(PHONE_REGEX, value)` against each case. Prints one line per
case (PASS/FAIL) and a final summary line:

    PASS 16/16   - every case classified correctly
    FAIL N/16    - N cases passed; (16 - N) misclassified

Exit code is 0 on full pass, 1 on any miss. The benchmark workload's
substring assertion looks for `PASS 16/16` in the agent's final message.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def load_cases(path: Path) -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for raw in path.read_text().splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        label, _, value = line.partition(" ")
        label = label.strip()
        value = value.strip()
        if label not in ("YES", "NO"):
            raise SystemExit(f"unknown label {label!r} in line: {raw!r}")
        cases.append((label, value))
    return cases


def main() -> int:
    here = Path(__file__).resolve().parent
    try:
        from solution import PHONE_REGEX  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - surfaced to the agent
        print(f"FAIL: could not import PHONE_REGEX from solution.py: {exc}")
        return 1

    if isinstance(PHONE_REGEX, str):
        pattern = re.compile(PHONE_REGEX)
    else:
        pattern = PHONE_REGEX  # assume already-compiled

    cases = load_cases(here / "test_cases.txt")
    total = len(cases)
    passed = 0
    for label, value in cases:
        matched = pattern.fullmatch(value) is not None
        expected = label == "YES"
        ok = matched == expected
        status = "PASS" if ok else "FAIL"
        marker = "match" if matched else "no-match"
        print(f"  {status}  [{label}] {value!r:<30}  -> {marker}")
        if ok:
            passed += 1

    if passed == total:
        print(f"PASS {passed}/{total}")
        return 0
    print(f"FAIL {passed}/{total}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
