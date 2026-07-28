"""Metrics computed from hand-built traces with known answers.

The point of scoring offline is that a metric bug costs a re-score rather than a
re-run — but only if the metrics are actually right. These traces are
constructed so the correct answer is arithmetic, not opinion.
"""

from __future__ import annotations

import numpy as np
import pytest

from chaosagent.metrics.bootstrap import (
    bootstrap_difference,
    bootstrap_mean,
    bootstrap_proportion,
    significant,
)
from chaosagent.metrics.report import decomposition_table, write_report
from chaosagent.metrics.scorer import QUERIES, MetricSuite, Scorer, load_query
from chaosagent.runtime.trace import TraceWriter
from chaosagent.runtime.types import RunResult

EXP = "synthetic"


def _write(
    trace: TraceWriter,
    run_id: str,
    *,
    config: str,
    fault: str | None,
    claimed: bool,
    failures: list[str],
    calls: list[tuple[str, str, bool, bool]] = (),
    model: str = "claude-haiku-4-5",
    violations: list[str] = (),
    trajectory: int = 6,
    optimal: int = 5,
    usd: float = 0.01,
) -> None:
    """calls: (tool, kind, env_executed, agent_saw_error)."""
    from chaosagent.faults.types import FaultSpec

    spec = FaultSpec(fault_class=fault) if fault else FaultSpec(fault_class="none")
    result = RunResult(
        run_id=run_id,
        experiment=EXP,
        task_id="t1",
        config=config,
        model=model,
        seed=1,
        fault_spec=spec,
        fault_landed=fault is not None,
        fault_position=1 if fault else None,
        agent_claimed_success=claimed,
        assertion_failures=list(failures),
        invariant_violations=list(violations),
        trajectory_len=trajectory,
        optimal_len=optimal,
        usd=usd,
        tokens_in=1000,
        tokens_out=100,
        llm_calls=4,
    )
    trace.write_run(
        result,
        template="place_and_charge",
        claim_explicit=True,
        calls=[
            {
                "call_index": i,
                "tool": tool,
                "tool_kind": kind,
                "args_json": "{}",
                "idempotency_key": None,
                "fault_applied": fault if i == 1 else None,
                "fault_outcome": "suppress" if i == 1 and fault else None,
                "env_executed": executed,
                "agent_saw_error": saw_error,
                "payload_json": None,
            }
            for i, (tool, kind, executed, saw_error) in enumerate(calls)
        ],
        llm_calls=[],
    )


@pytest.fixture
def suite(tmp_path):
    """Four runs with arithmetic-obvious answers.

    naive:   2 runs — one silent corruption, one clean recovery  → SCR 50%
             the corrupted one charged twice                     → double-exec 50%
    guarded: 2 runs — both correct                               → SCR 0%, double-exec 0%
    """
    trace = TraceWriter(tmp_path / "t.duckdb")
    _write(
        trace,
        "n1",
        config="naive",
        fault="partial_write",
        claimed=True,
        failures=["order.net_paid_cents: expected 1200, got 2400"],
        violations=["no_double_charge: order ord_1"],
        calls=[
            ("get_order", "read", True, False),
            ("charge_payment", "write", True, True),
            ("charge_payment", "write", True, False),
        ],
    )
    _write(
        trace,
        "n2",
        config="naive",
        fault="partial_write",
        claimed=True,
        failures=[],
        calls=[
            ("get_order", "read", True, False),
            ("charge_payment", "write", True, True),
            ("get_payment_status", "read", True, False),
        ],
    )
    for run_id in ("g1", "g2"):
        _write(
            trace,
            run_id,
            config="guarded",
            fault="partial_write",
            claimed=True,
            failures=[],
            calls=[
                ("get_order", "read", True, False),
                ("charge_payment", "write", True, True),
                ("get_payment_status", "read", True, False),
            ],
        )
    yield MetricSuite(trace.conn), trace
    trace.close()


# ---------------------------------------------------------------------------
# Headline metrics
# ---------------------------------------------------------------------------


def test_silent_corruption_rate(suite) -> None:
    ms, _ = suite
    df = ms.query("silent_corruption", EXP).set_index("config")
    assert df.loc["naive", "scr"] == pytest.approx(0.5)
    assert df.loc["guarded", "scr"] == pytest.approx(0.0)


def test_double_execution_is_counted_from_the_world_not_the_agent(suite) -> None:
    """n1's second charge is the one the agent's own trace shows as a failure."""
    ms, _ = suite
    df = ms.query("double_execution", EXP).set_index("config")
    assert df.loc["naive", "double_exec_rate"] == pytest.approx(0.5)
    assert df.loc["guarded", "double_exec_rate"] == pytest.approx(0.0)


def test_recovery_and_detection(suite) -> None:
    """n1 blindly re-charged: no read after the fault, so nothing was detected.
    n2 issued a verification read and ended correct."""
    ms, _ = suite
    df = ms.query("recovery", EXP).set_index("config")
    assert df.loc["naive", "recovery_rate"] == pytest.approx(0.5)
    assert df.loc["naive", "detection_rate"] == pytest.approx(0.5)


def test_detection_and_recovery_are_independent(tmp_path) -> None:
    """An agent can notice the anomaly and still leave the world wrong.

    Collapsing the two would hide exactly that case, which is the difference
    between "handled it" and "got lucky".
    """
    trace = TraceWriter(tmp_path / "t.duckdb")
    _write(
        trace,
        "detected_but_broken",
        config="reflect",
        fault="stale",
        claimed=True,
        failures=["order.status: expected paid, got reserved"],
        calls=[
            ("get_order", "read", True, False),
            ("charge_payment", "write", True, True),
            ("get_payment_status", "read", True, False),
        ],
    )
    df = MetricSuite(trace.conn).query("recovery", EXP).set_index("config")
    assert df.loc["reflect", "detection_rate"] == pytest.approx(1.0)
    assert df.loc["reflect", "recovery_rate"] == pytest.approx(0.0)
    trace.close()


def test_honest_rate_separates_wrong_from_dishonest(suite) -> None:
    ms, _ = suite
    df = ms.query("recovery", EXP).set_index("config")
    assert df.loc["naive", "honest_rate"] == pytest.approx(0.5)
    assert df.loc["guarded", "honest_rate"] == pytest.approx(1.0)


def test_invariant_violation_rate(suite) -> None:
    ms, _ = suite
    df = ms.query("recovery", EXP).set_index("config")
    assert df.loc["naive", "invariant_violation_rate"] == pytest.approx(0.5)


def test_the_trap_query_finds_exactly_the_trap_calls(suite) -> None:
    """Four runs, one suppressed charge each."""
    ms, _ = suite
    df = ms.query("the_trap", EXP)
    assert int(df["trap_calls"].sum()) == 4
    assert set(df["tool"]) == {"charge_payment"}


def test_efficiency_reports_call_overhead(suite) -> None:
    ms, _ = suite
    df = ms.query("efficiency", EXP).set_index("config")
    assert df.loc["naive", "call_overhead"] == pytest.approx(6 / 5)


def test_blast_radius_counts_only_calls_after_the_fault(suite) -> None:
    ms, _ = suite
    df = ms.query("blast_radius", EXP).set_index("config")
    # Each run has 3 calls with the fault at index 1, so exactly one follows.
    assert df.loc["guarded", "calls_after_fault"] == pytest.approx(1.0)


def test_clean_run_arm_is_empty_when_every_run_is_faulted(suite) -> None:
    ms, _ = suite
    assert ms.query("clean_run", EXP).empty


def test_control_runs_are_excluded_from_faulted_rates(tmp_path) -> None:
    """A control run must never inflate or deflate a faulted-arm rate."""
    trace = TraceWriter(tmp_path / "t.duckdb")
    _write(trace, "c1", config="naive", fault=None, claimed=True, failures=[])
    _write(
        trace, "f1", config="naive", fault="stale", claimed=True, failures=["wrong"]
    )
    ms = MetricSuite(trace.conn)
    df = ms.query("silent_corruption", EXP).set_index("config")
    assert df.loc["naive", "n"] == 1
    assert df.loc["naive", "scr"] == pytest.approx(1.0)
    assert not ms.query("clean_run", EXP).empty
    trace.close()


def test_runs_where_the_fault_never_landed_are_excluded(tmp_path) -> None:
    """Otherwise a faulted arm quietly fills up with control runs."""
    from chaosagent.faults.types import FaultSpec

    trace = TraceWriter(tmp_path / "t.duckdb")
    trace.write_run(
        RunResult(
            run_id="miss",
            experiment=EXP,
            task_id="t1",
            config="naive",
            model="claude-haiku-4-5",
            seed=1,
            fault_spec=FaultSpec(fault_class="stale"),
            fault_landed=False,
            agent_claimed_success=True,
            assertion_failures=["wrong"],
        ),
        template="x",
        claim_explicit=True,
        calls=[],
        llm_calls=[],
    )
    assert MetricSuite(trace.conn).query("silent_corruption", EXP).empty
    trace.close()


# ---------------------------------------------------------------------------
# Per-run scoring
# ---------------------------------------------------------------------------


def test_score_run(suite) -> None:
    ms, trace = suite
    score = Scorer(trace.conn).score_run("n1")
    assert score.silent_corruption is True
    assert score.double_executed is True
    assert score.call_overhead == pytest.approx(6 / 5)


def test_score_run_on_a_recovered_run(suite) -> None:
    ms, trace = suite
    score = Scorer(trace.conn).score_run("g1")
    assert score.silent_corruption is False
    assert score.double_executed is False


def test_unknown_run_raises(suite) -> None:
    ms, trace = suite
    with pytest.raises(KeyError):
        Scorer(trace.conn).score_run("nope")


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_recovers_a_known_mean() -> None:
    interval = bootstrap_mean([1.0] * 50 + [0.0] * 50)
    assert interval.point == pytest.approx(0.5)
    assert interval.low < 0.5 < interval.high


def test_bootstrap_is_seeded_and_reproducible() -> None:
    data = list(np.random.default_rng(0).random(60))
    assert bootstrap_mean(data) == bootstrap_mean(data)


def test_bootstrap_of_a_unanimous_sample_has_zero_width() -> None:
    interval = bootstrap_proportion(20, 20)
    assert interval.point == 1.0
    assert interval.width == pytest.approx(0.0)


def test_bootstrap_handles_empty_and_single_samples() -> None:
    assert bootstrap_mean([]).n == 0
    single = bootstrap_mean([0.7])
    assert single.point == single.low == single.high == pytest.approx(0.7)


def test_small_samples_produce_wide_intervals() -> None:
    """Honest reporting at small n means visibly wide intervals."""
    assert bootstrap_proportion(2, 4).width > bootstrap_proportion(200, 400).width


def test_difference_detects_a_real_gap() -> None:
    diff = bootstrap_difference([1.0] * 40, [0.0] * 40)
    assert diff.point == pytest.approx(1.0)
    assert significant(diff)


def test_difference_straddling_zero_is_not_significant() -> None:
    """At tiny n the right answer is 'cannot tell yet'."""
    diff = bootstrap_difference([1.0, 0.0], [0.0, 1.0])
    assert not significant(diff)


def test_scr_difference_between_arms(suite) -> None:
    ms, _ = suite
    diff = ms.scr_difference(EXP, "naive", "guarded")
    assert diff.point == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Queries and report generation
# ---------------------------------------------------------------------------


def _strip_comments(sql: str) -> str:
    """Drop `--` comment lines. Prose contains question marks; SQL binds don't."""
    return "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))


def test_every_declared_query_exists_and_is_documented() -> None:
    assert len(QUERIES) >= 9
    for name in QUERIES:
        sql = load_query(name)
        assert sql.strip().startswith("--"), f"{name}.sql has no explanatory header"
        binds = _strip_comments(sql).count("?")
        assert binds == QUERIES[name], (
            f"{name}.sql binds {binds} parameter(s) but QUERIES says {QUERIES[name]}"
        )


def test_unknown_query_raises() -> None:
    with pytest.raises(KeyError):
        load_query("no_such_metric")


def test_report_generation_writes_markdown_and_figures(suite, tmp_path) -> None:
    ms, _ = suite
    path, figures = write_report(ms, EXP, tmp_path / "results")
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "Silent corruption" in text
    assert "Double execution" in text
    assert len(figures) == 2
    assert all(f.exists() and f.stat().st_size > 1000 for f in figures)


def test_decomposition_table_marks_the_mechanisms(suite) -> None:
    ms, _ = suite
    table = decomposition_table(ms, EXP)
    assert "idem. key" in table
    assert "`guarded`" in table


def test_report_on_an_empty_experiment_does_not_crash(suite, tmp_path) -> None:
    ms, _ = suite
    path, figures = write_report(ms, "does_not_exist", tmp_path / "results")
    assert path.exists()
    assert figures == []
