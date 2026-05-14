"""Per-workspace retention caps for the pattern store.

See `pattern-store.md §6`. v1 caps target single-user laptop scale. Phase 3+
may raise these or add a virtual column index to keep K-NN cheap at scale.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PatternCaps:
    """Per-workspace caps. Pinned at construction time so tests can build
    narrower stores."""

    soft_cap_rows: int = 5_000
    hard_cap_rows: int = 10_000
    max_age_days: int = 180

    def __post_init__(self) -> None:
        if self.soft_cap_rows <= 0 or self.hard_cap_rows <= 0:
            raise ValueError("caps must be positive")
        if self.soft_cap_rows > self.hard_cap_rows:
            raise ValueError("soft_cap_rows must be <= hard_cap_rows")
        if self.max_age_days <= 0:
            raise ValueError("max_age_days must be positive")
