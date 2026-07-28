"""DuckDB trace store.

One row per run, one per tool call, one per LLM call. Every reported number is a
SQL query over these three tables — the queries ship in ``chaosagent/metrics/queries/``
so a reviewer who can read SQL can check the number without trusting the code
that produced it.

The pair that matters is ``calls.env_executed`` and ``calls.agent_saw_error``.
Their disagreement *is* silent corruption, expressed at the schema level.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from chaosagent.runtime.types import RunResult

DEFAULT_DB = Path(os.environ.get("CHAOSAGENT_TRACE_DB", "traces/chaosagent.duckdb"))

DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            VARCHAR PRIMARY KEY,
    experiment        VARCHAR,
    task_id           VARCHAR,
    template          VARCHAR,
    config            VARCHAR,
    model             VARCHAR,
    seed              INTEGER,
    fault_class       VARCHAR,        -- NULL for the control arm
    fault_target      VARCHAR,
    fault_landed      BOOLEAN,
    fault_position    INTEGER,        -- call index actually injected at
    trajectory_len    INTEGER,
    optimal_len       INTEGER,        -- from the reference solver
    claimed_success   BOOLEAN,
    claim_explicit    BOOLEAN,
    state_correct     BOOLEAN,        -- derived from assertion_failures
    assertion_failures VARCHAR,       -- JSON array
    invariants_broken INTEGER,
    invariant_names   VARCHAR,        -- JSON array
    stop_reason       VARCHAR,
    tokens_in         INTEGER,
    tokens_out        INTEGER,
    usd               DOUBLE,
    wall_ms           INTEGER,
    cache_hits        INTEGER,
    llm_calls         INTEGER,
    error             VARCHAR,
    created_at        TIMESTAMP
);

CREATE TABLE IF NOT EXISTS calls (
    run_id            VARCHAR,
    call_index        INTEGER,
    tool              VARCHAR,
    tool_kind         VARCHAR,
    args_json         VARCHAR,
    idempotency_key   VARCHAR,
    fault_applied     VARCHAR,        -- NULL if clean
    fault_outcome     VARCHAR,
    env_executed      BOOLEAN,        -- did the world actually mutate
    agent_saw_error   BOOLEAN,        -- what the agent observed
    -- True when an idempotency key short-circuited a repeat. Such a call
    -- SUCCEEDS and returns the original payload while executing nothing, so it
    -- must not be counted as an execution. This column exists because getting
    -- that wrong invented a double execution that never happened.
    idempotent_replay BOOLEAN,
    payload_json      VARCHAR,
    PRIMARY KEY (run_id, call_index)
);

CREATE TABLE IF NOT EXISTS llm_calls (
    run_id            VARCHAR,
    step              INTEGER,
    cache_hit         BOOLEAN,
    prompt_hash       VARCHAR,
    tokens_in         INTEGER,
    tokens_out        INTEGER,
    latency_ms        INTEGER,
    PRIMARY KEY (run_id, step)
);
"""


def connect(path: Path | str = DEFAULT_DB, *, read_only: bool = False) -> Any:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - install-time issue
        raise RuntimeError(
            "duckdb is required for the trace store; install with: pip install -e '.[run]'"
        ) from exc
    path = Path(path)
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        conn.execute(DDL)
    return conn


class TraceWriter:
    """Appends facts. Decides nothing."""

    def __init__(self, path: Path | str = DEFAULT_DB) -> None:
        self.path = Path(path)
        self.conn = connect(self.path)

    # -- resume support -----------------------------------------------------

    def completed_run_ids(self, experiment: str) -> set[str]:
        """Run ids already recorded — the basis of a resumable sweep.

        Killing a sweep at 80% must not cost the 80%.
        """
        rows = self.conn.execute(
            "SELECT run_id FROM runs WHERE experiment = ?", [experiment]
        ).fetchall()
        return {r[0] for r in rows}

    def spent_usd(self, experiment: str) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(usd), 0.0) FROM runs WHERE experiment = ?", [experiment]
        ).fetchone()
        return float(row[0]) if row else 0.0

    # -- writes -------------------------------------------------------------

    def write_run(
        self,
        result: RunResult,
        *,
        template: str,
        claim_explicit: bool,
        calls: list[dict[str, Any]],
        llm_calls: list[dict[str, Any]],
    ) -> None:
        self.conn.execute("DELETE FROM runs WHERE run_id = ?", [result.run_id])
        self.conn.execute("DELETE FROM calls WHERE run_id = ?", [result.run_id])
        self.conn.execute("DELETE FROM llm_calls WHERE run_id = ?", [result.run_id])

        spec = result.fault_spec
        self.conn.execute(
            """
            INSERT INTO runs VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, now()
            )
            """,
            [
                result.run_id,
                result.experiment,
                result.task_id,
                template,
                result.config,
                result.model,
                result.seed,
                None if spec is None or spec.is_control else spec.fault_class,
                # The full label, not just `target`: it carries the rate and any
                # params, without which a stochastic rate sweep is unanalysable
                # because every arm looks like plain "stochastic".
                None if spec is None else spec.label(),
                result.fault_landed,
                result.fault_position,
                result.trajectory_len,
                result.optimal_len,
                result.agent_claimed_success,
                claim_explicit,
                result.state_correct,
                json.dumps(result.assertion_failures),
                len(result.invariant_violations),
                json.dumps(result.invariant_violations),
                result.stop_reason,
                result.tokens_in,
                result.tokens_out,
                result.usd,
                result.wall_ms,
                result.cache_hits,
                result.llm_calls,
                result.error,
            ],
        )

        for call in calls:
            self.conn.execute(
                "INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    result.run_id,
                    call["call_index"],
                    call["tool"],
                    call["tool_kind"],
                    call["args_json"],
                    call["idempotency_key"],
                    call["fault_applied"],
                    call["fault_outcome"],
                    call["env_executed"],
                    call["agent_saw_error"],
                    call.get("idempotent_replay", False),
                    call["payload_json"],
                ],
            )

        for entry in llm_calls:
            self.conn.execute(
                "INSERT INTO llm_calls VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    result.run_id,
                    entry["step"],
                    entry["cache_hit"],
                    entry["prompt_hash"],
                    entry["tokens_in"],
                    entry["tokens_out"],
                    entry["latency_ms"],
                ],
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> TraceWriter:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


__all__ = ["DDL", "DEFAULT_DB", "TraceWriter", "connect"]
