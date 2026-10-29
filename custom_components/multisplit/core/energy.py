"""Whether the house has power to spare, settled against passing clouds.

Pure, like the planner: the coordinator reads the sensor and the clock and
hands them in, so the settling is tested rather than trusted.
"""

from __future__ import annotations

from datetime import datetime, timedelta


class SurplusLatch:
    """Follows a surplus signal only once it has held its new state.

    Settling both ways matters more than it looks. Banking heat *uses* the
    surplus, so export can stop because the plan started banking; a latch
    that dropped out at once would stop banking the moment it began, and the
    two would oscillate at the sensor's own pace. Holding each state for
    ``settle`` before following it bounds that loop to one change per settle.

    An unknown reading counts as no surplus: the safe answer is the one the
    system gave before this existed. A fresh latch starts there too, so a
    restart never begins by banking on a sensor it has not yet seen settle.
    """

    def __init__(self, settle: timedelta) -> None:
        self.settle = settle
        self.active = False
        self._pending: bool | None = None
        self._since: datetime | None = None

    def update(self, reading: bool | None, now: datetime) -> bool:
        wanted = bool(reading)
        if wanted == self.active:
            self._pending, self._since = None, None
        elif self._pending != wanted:
            self._pending, self._since = wanted, now
        if self._since is not None and now - self._since >= self.settle:
            self.active = wanted
            self._pending, self._since = None, None
        return self.active

    def describe(self, now: datetime) -> str:
        """For the explain service: where the latch is, and what it waits on."""
        state = "surplus" if self.active else "no surplus"
        if self._pending is None or self._since is None:
            return state
        left = max(timedelta(0), self.settle - (now - self._since))
        towards = "surplus" if self._pending else "no surplus"
        return f"{state}, changing to {towards} in {int(left.total_seconds() // 60)} min if it holds"
