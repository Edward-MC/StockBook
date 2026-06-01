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
        """Record one call for `day`; return False if it exceeds the limit."""
        used = self._counts.get(day, 0)
        if used >= self.limit:
            return False
        self._counts[day] = used + 1
        return True

    def remaining(self, day: str) -> int:
        return max(0, self.limit - self._counts.get(day, 0))
