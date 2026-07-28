"""Bootstrap confidence intervals.

Every rate in this project is a proportion over a modest number of runs, so a
point estimate on its own invites over-reading. The percentile bootstrap makes
no distributional assumption and degrades gracefully at small n — which matters,
because the reduced grids are small on purpose.

Seeded, so the intervals in the README are reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

DEFAULT_RESAMPLES = 10_000
DEFAULT_SEED = 20260731


@dataclass(frozen=True)
class Interval:
    point: float
    low: float
    high: float
    n: int

    def __str__(self) -> str:
        return f"{self.point:.0%} [{self.low:.0%}–{self.high:.0%}]"

    def pct(self, digits: int = 0) -> str:
        return f"{self.point * 100:.{digits}f}"

    @property
    def width(self) -> float:
        return self.high - self.low


def bootstrap_mean(
    values: list[float] | np.ndarray,
    *,
    resamples: int = DEFAULT_RESAMPLES,
    alpha: float = 0.05,
    seed: int = DEFAULT_SEED,
) -> Interval:
    """Percentile bootstrap CI for a mean (or a proportion)."""
    data = np.asarray(list(values), dtype=float)
    n = len(data)
    if n == 0:
        return Interval(float("nan"), float("nan"), float("nan"), 0)
    point = float(data.mean())
    if n == 1:
        return Interval(point, point, point, 1)

    rng = np.random.default_rng(seed)
    draws = rng.choice(data, size=(resamples, n), replace=True).mean(axis=1)
    low, high = np.percentile(draws, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return Interval(point, float(low), float(high), n)


def bootstrap_proportion(
    successes: int,
    n: int,
    *,
    resamples: int = DEFAULT_RESAMPLES,
    alpha: float = 0.05,
    seed: int = DEFAULT_SEED,
) -> Interval:
    """CI for a proportion given counts rather than raw observations."""
    if n == 0:
        return Interval(float("nan"), float("nan"), float("nan"), 0)
    data = np.concatenate([np.ones(successes), np.zeros(n - successes)])
    return bootstrap_mean(data, resamples=resamples, alpha=alpha, seed=seed)


def bootstrap_difference(
    a: list[float] | np.ndarray,
    b: list[float] | np.ndarray,
    *,
    resamples: int = DEFAULT_RESAMPLES,
    alpha: float = 0.05,
    seed: int = DEFAULT_SEED,
) -> Interval:
    """CI for ``mean(a) - mean(b)``.

    This is the one that answers "does this arm actually beat that one" — an
    interval straddling zero means the grid is too small to say, which at these
    sample sizes is a result worth reporting rather than hiding.
    """
    xa = np.asarray(list(a), dtype=float)
    xb = np.asarray(list(b), dtype=float)
    if len(xa) == 0 or len(xb) == 0:
        return Interval(float("nan"), float("nan"), float("nan"), 0)

    rng = np.random.default_rng(seed)
    da = rng.choice(xa, size=(resamples, len(xa)), replace=True).mean(axis=1)
    db = rng.choice(xb, size=(resamples, len(xb)), replace=True).mean(axis=1)
    diff = da - db
    low, high = np.percentile(diff, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return Interval(
        float(xa.mean() - xb.mean()), float(low), float(high), len(xa) + len(xb)
    )


def significant(interval: Interval) -> bool:
    """True when a difference interval excludes zero."""
    if interval.n == 0:
        return False
    return interval.low > 0 or interval.high < 0


__all__ = [
    "DEFAULT_RESAMPLES",
    "DEFAULT_SEED",
    "Interval",
    "bootstrap_difference",
    "bootstrap_mean",
    "bootstrap_proportion",
    "significant",
]
