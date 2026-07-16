"""Injection schedules — *when* a fault fires.

Four modes, matching the experiment plan:

* ``call_index:N`` — a single fault at a fixed position. This is what powers
  the position sweep.
* ``random`` — a single fault at a seeded random position.
* ``tool:NAME`` — a single fault on the first call to a named tool. Isolates
  the non-idempotent-write case.
* ``stochastic`` — each call independently at rate ``p``. Measures compounding.

Single-fault schedules are *armed*, not fixed: they fire on the first
**eligible** call at or after their target index. Without that, a
``partial_write`` scheduled at index 0 of a trajectory that opens with three
reads would silently degrade into a control run, and the control arm would be
contaminated with runs the analysis believes were faulted.
"""

from __future__ import annotations

from random import Random
from typing import Protocol

from chaosagent.faults.types import FaultSpec


class Schedule(Protocol):
    #: False for schedules that may fire more than once.
    single: bool

    def wants(self, call_index: int, tool: str) -> bool: ...

    def describe(self) -> str: ...


class Never:
    """The control arm."""

    single = True

    def wants(self, call_index: int, tool: str) -> bool:
        return False

    def describe(self) -> str:
        return "none"


class AtOrAfterIndex:
    """One fault, at the first eligible call at or after ``index``."""

    single = True

    def __init__(self, index: int) -> None:
        self.index = index

    def wants(self, call_index: int, tool: str) -> bool:
        return call_index >= self.index

    def describe(self) -> str:
        return f"call_index:{self.index}"


class AtTool:
    """One fault, on the first call to ``tool``."""

    single = True

    def __init__(self, tool: str) -> None:
        self.tool = tool

    def wants(self, call_index: int, tool: str) -> bool:
        return tool == self.tool

    def describe(self) -> str:
        return f"tool:{self.tool}"


class Stochastic:
    """Each call independently, at rate ``p``."""

    single = False

    def __init__(self, rate: float, rng: Random) -> None:
        self.rate = rate
        self.rng = rng

    def wants(self, call_index: int, tool: str) -> bool:
        return self.rng.random() < self.rate

    def describe(self) -> str:
        return f"stochastic:{self.rate}"


def build_schedule(spec: FaultSpec, rng: Random, trajectory_hint: int) -> Schedule:
    """Interpret ``FaultSpec.target``.

    ``trajectory_hint`` is the reference solver's optimal call count, used to
    place a ``random`` fault somewhere inside a plausible trajectory.
    """
    if spec.is_control:
        return Never()

    target = spec.target or "random"
    if target == "random":
        upper = max(1, trajectory_hint)
        return AtOrAfterIndex(rng.randrange(upper))
    if target == "stochastic":
        return Stochastic(spec.rate, rng)
    if target.startswith("call_index:"):
        return AtOrAfterIndex(int(target.split(":", 1)[1]))
    if target.startswith("tool:"):
        return AtTool(target.split(":", 1)[1])
    if target.startswith("position:"):
        # early | mid | late, normalised against the optimal trajectory length.
        bucket = target.split(":", 1)[1]
        return AtOrAfterIndex(position_index(bucket, trajectory_hint))
    raise ValueError(f"unrecognised fault target '{target}'")


#: Fraction of the optimal trajectory each bucket starts at.
POSITION_BUCKETS: dict[str, float] = {"early": 0.0, "mid": 0.5, "late": 0.85}


def position_index(bucket: str, trajectory_hint: int) -> int:
    try:
        fraction = POSITION_BUCKETS[bucket]
    except KeyError:
        raise ValueError(
            f"unknown position bucket '{bucket}'; use one of {sorted(POSITION_BUCKETS)}"
        ) from None
    upper = max(1, trajectory_hint)
    return min(upper - 1, int(fraction * upper))


def bucket_for(call_index: int, trajectory_len: int) -> str:
    """Inverse mapping, used by the analysis to bucket observed positions."""
    if trajectory_len <= 0:
        return "unknown"
    fraction = call_index / trajectory_len
    if fraction < 0.34:
        return "early"
    if fraction < 0.67:
        return "mid"
    return "late"


__all__ = [
    "POSITION_BUCKETS",
    "AtOrAfterIndex",
    "AtTool",
    "Never",
    "Schedule",
    "Stochastic",
    "bucket_for",
    "build_schedule",
    "position_index",
]
