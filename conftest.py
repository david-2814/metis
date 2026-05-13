"""Workspace-root pytest configuration.

Puts the repo root on `sys.path` so workspace-wide test helpers (under
`tests_shared/`) are importable from any workspace member's test tree
regardless of where pytest is invoked from.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.resolve()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
