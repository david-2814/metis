"""Workspace-root pytest configuration.

Puts the repo root on `sys.path` so workspace-wide test helpers (under
`tests_shared/`) are importable from any workspace member's test tree
regardless of where pytest is invoked from. Also puts `scripts/` on
`sys.path` so tests can import the top-level utility scripts (e.g.
`scripts/benchmark.py` is exercised by `apps/cli/tests/test_benchmark.py`).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.resolve()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
