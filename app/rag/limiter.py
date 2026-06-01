"""In-process daily call counter for cost protection (spec §7.2).

Single-user local app, single process → an in-memory counter is sufficient
(resets on restart, which is acceptable). Keyed by date string so a new day
resets automatically.
"""
from __future__ import annotations

from typing import Dict


class DailyLimiter:
    def __init__(self, limit: int):
        self.limit = limit
        self._counts: Dict[str, int] = {}

    def allow(self, day: str) -> bool:
        """True if another call for `day` is within the limit. Does NOT consume
        a slot — call record() only after the call actually succeeds, so failed
        calls (e.g. missing API key) don't burn quota."""
        return self._counts.get(day, 0) < self.limit

    def record(self, day: str) -> None:
        """Consume one slot for `day` (call after a successful billable call)."""
        self._counts[day] = self._counts.get(day, 0) + 1

    def remaining(self, day: str) -> int:
        return max(0, self.limit - self._counts.get(day, 0))
