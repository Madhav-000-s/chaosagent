"""Virtual clock.

No part of the environment reads the wall clock. Time advances only when the
harness says so, which buys three things:

1. `timeout` faults are *injected*, not waited for — a full sweep runs in
   seconds rather than hours.
2. Two runs with the same seed and action sequence produce byte-identical
   timestamps, so state digests compare cleanly.
3. Snapshots are complete: restoring state also restores time.
"""

from __future__ import annotations

#: Fixed virtual epoch. Arbitrary, but stable across releases so that committed
#: trace fixtures keep comparing equal.
EPOCH = 1_750_000_000


class VirtualClock:
    """A monotonic tick counter standing in for wall time."""

    __slots__ = ("_t",)

    def __init__(self, start: int = EPOCH) -> None:
        self._t = start

    def now(self) -> int:
        return self._t

    def advance(self, ticks: int = 1) -> int:
        if ticks < 0:
            raise ValueError("virtual time does not run backwards")
        self._t += ticks
        return self._t

    # -- snapshot support ---------------------------------------------------

    def state(self) -> int:
        return self._t

    def restore(self, t: int) -> None:
        self._t = t

    def __repr__(self) -> str:  # pragma: no cover - display only
        return f"VirtualClock(t={self._t})"


__all__ = ["EPOCH", "VirtualClock"]
