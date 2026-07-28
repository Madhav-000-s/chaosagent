"""The scorer. Pure, offline, and operates only on the trace store.

Takes no LLM and no environment. Every metric is one of the shipped SQL files in
``queries/`` — a reviewer who can read the query can check the number without
trusting this module. That separation is also what makes a metric bug cheap: it
costs a re-score, not a re-run.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from chaosagent.metrics.bootstrap import Interval, bootstrap_difference, bootstrap_proportion

QUERY_DIR = Path(__file__).parent / "queries"

#: Metric name -> number of times the experiment name is bound in the query.
QUERIES: dict[str, int] = {
    "silent_corruption": 1,
    "double_execution": 3,
    "recovery": 2,
    "efficiency": 1,
    "clean_run": 1,
    "blast_radius": 2,
    "the_trap": 1,
    "position_sweep": 1,
    "model_sweep": 2,
    "fault_landing": 1,
}


@cache
def load_query(name: str) -> str:
    path = QUERY_DIR / f"{name}.sql"
    if not path.exists():
        raise KeyError(f"unknown metric '{name}'; known: {sorted(QUERIES)}")
    return path.read_text(encoding="utf-8")


@dataclass
class RunScore:
    """The per-run judgements, derived from recorded facts."""

    run_id: str
    config: str
    fault_class: str | None
    claimed_success: bool
    state_correct: bool
    invariants_broken: int
    double_executed: bool
    trajectory_len: int
    optimal_len: int
    usd: float

    @property
    def silent_corruption(self) -> bool:
        return self.claimed_success and not self.state_correct

    @property
    def call_overhead(self) -> float:
        return self.trajectory_len / self.optimal_len if self.optimal_len else float("nan")


class Scorer:
    """Scores individual runs. The suite below handles aggregates."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def score_run(self, run_id: str) -> RunScore:
        row = self.conn.execute(
            """
            SELECT r.run_id, r.config, r.fault_class, r.claimed_success, r.state_correct,
                   r.invariants_broken, r.trajectory_len, r.optimal_len, r.usd,
                   COALESCE((
                       SELECT MAX(cnt) FROM (
                           SELECT COUNT(*) AS cnt FROM calls c
                           WHERE c.run_id = r.run_id AND c.env_executed
                             AND c.tool IN ('charge_payment','reserve_stock',
                                            'ship_order','refund_payment')
                           GROUP BY c.tool
                       )
                   ), 0) AS max_repeats
            FROM runs r WHERE r.run_id = ?
            """,
            [run_id],
        ).fetchone()
        if row is None:
            raise KeyError(f"no run '{run_id}' in the trace store")
        return RunScore(
            run_id=row[0],
            config=row[1],
            fault_class=row[2],
            claimed_success=bool(row[3]),
            state_correct=bool(row[4]),
            invariants_broken=int(row[5]),
            double_executed=int(row[9]) > 1,
            trajectory_len=int(row[6]),
            optimal_len=int(row[7]),
            usd=float(row[8]),
        )


class MetricSuite:
    """Runs every shipped query against one experiment."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def query(self, name: str, experiment: str) -> Any:
        sql = load_query(name)
        params = [experiment] * QUERIES[name]
        return self.conn.execute(sql, params).df()

    def compute(self, experiment: str) -> dict[str, Any]:
        return {name: self.query(name, experiment) for name in QUERIES}

    def experiments(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT experiment FROM runs ORDER BY 1"
        ).fetchall()
        return [r[0] for r in rows]

    def run_count(self, experiment: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM runs WHERE experiment = ?", [experiment]
        ).fetchone()
        return int(row[0]) if row else 0

    # -- confidence intervals ----------------------------------------------

    def _outcomes(self, experiment: str, config: str, expression: str) -> list[float]:
        """Raw per-run 0/1 outcomes, so the bootstrap resamples runs not rates."""
        rows = self.conn.execute(
            f"SELECT CASE WHEN {expression} THEN 1.0 ELSE 0.0 END FROM runs "
            "WHERE experiment = ? AND config = ? AND fault_landed",
            [experiment, config],
        ).fetchall()
        return [float(r[0]) for r in rows]

    def scr_interval(self, experiment: str, config: str) -> Interval:
        values = self._outcomes(
            experiment, config, "claimed_success AND NOT state_correct"
        )
        return bootstrap_proportion(int(sum(values)), len(values))

    def recovery_interval(self, experiment: str, config: str) -> Interval:
        values = self._outcomes(experiment, config, "state_correct")
        return bootstrap_proportion(int(sum(values)), len(values))

    def scr_difference(self, experiment: str, a: str, b: str) -> Interval:
        """CI on ``scr(a) - scr(b)``. Straddling zero means "cannot tell yet"."""
        return bootstrap_difference(
            self._outcomes(experiment, a, "claimed_success AND NOT state_correct"),
            self._outcomes(experiment, b, "claimed_success AND NOT state_correct"),
        )


def open_suite(db: Path | str) -> tuple[MetricSuite, Any]:
    from chaosagent.runtime.trace import connect

    conn = connect(db, read_only=True)
    return MetricSuite(conn), conn


__all__ = [
    "QUERIES",
    "QUERY_DIR",
    "MetricSuite",
    "RunScore",
    "Scorer",
    "load_query",
    "open_suite",
]
