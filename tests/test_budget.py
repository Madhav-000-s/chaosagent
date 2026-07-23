"""Budget exhaustion ends a run cleanly. It is never an exception.

A run that hits its ceiling is a datum — "this configuration could not finish
inside the budget" is a result, and one worth reporting separately. A run that
raises is lost work and, worse, a sweep that dies at 80%.
"""

from __future__ import annotations

import asyncio

from chaosagent.agents.configs import get_config
from chaosagent.env.fixtures import base_state
from chaosagent.env.world import Environment
from chaosagent.faults import FaultInjector, FaultSpec
from chaosagent.runtime.cache import ResponseCache
from chaosagent.runtime.orchestrator import Experiment, run_experiment
from chaosagent.runtime.runner import make_run_id
from chaosagent.runtime.trace import TraceWriter
from chaosagent.runtime.types import Budget, RunResult
from chaosagent.tasks import default_task_set

from .conftest import ScriptedLLM

TASK = default_task_set().by_id("place_and_charge_01")

#: An agent that never stops calling tools.
LOOP_FOREVER = [{"tool": "get_inventory", "args": {}} for _ in range(500)]


def _run(steps, budget: Budget, config: str = "naive"):
    from chaosagent.agents.loop import run_agent

    env = Environment(seed=1, init_state=base_state())
    faulty = FaultInjector(FaultSpec(fault_class="none"), seed=1).wrap(env)
    llm = ScriptedLLM(steps)
    outcome = asyncio.run(run_agent(TASK, faulty, llm, get_config(config), budget))
    return outcome, faulty, llm


# ---------------------------------------------------------------------------
# Every ceiling terminates cleanly
# ---------------------------------------------------------------------------


def test_max_steps_stops_cleanly() -> None:
    outcome, _, llm = _run(LOOP_FOREVER, Budget(max_steps=5))
    assert outcome.stop_reason == "budget"
    assert llm.calls == 5
    assert outcome.error is None


def test_max_calls_stops_cleanly() -> None:
    outcome, faulty, _ = _run(LOOP_FOREVER, Budget(max_calls=4, max_steps=50))
    assert outcome.stop_reason in ("max_calls", "budget")
    assert len(faulty.history) <= 4


def test_max_tokens_stops_cleanly() -> None:
    outcome, _, _ = _run(LOOP_FOREVER, Budget(max_tokens=350, max_steps=50))
    assert outcome.stop_reason == "budget"
    assert outcome.tokens_in + outcome.tokens_out >= 350


def test_max_usd_stops_cleanly() -> None:
    from chaosagent.agents.loop import run_agent

    env = Environment(seed=1, init_state=base_state())
    faulty = FaultInjector(FaultSpec(fault_class="none"), seed=1).wrap(env)
    # 1M input tokens per turn on Haiku is $1 a turn.
    llm = ScriptedLLM(LOOP_FOREVER, tokens_in=1_000_000, tokens_out=0)
    outcome = asyncio.run(
        run_agent(
            TASK, faulty, llm, get_config("naive"), Budget(max_usd=2.5, max_tokens=10**9, max_steps=50)
        )
    )
    assert outcome.stop_reason == "budget"
    assert llm.calls <= 4


def test_budget_exhaustion_is_never_an_exception() -> None:
    for budget in (
        Budget(max_steps=1),
        Budget(max_calls=1),
        Budget(max_tokens=1),
        Budget(max_usd=0.0),
    ):
        outcome, _, _ = _run(LOOP_FOREVER, budget)
        assert outcome.stop_reason in ("budget", "max_calls", "done")
        assert outcome.error is None


def test_a_zero_budget_run_still_produces_a_scoreable_record() -> None:
    outcome, _, _ = _run(LOOP_FOREVER, Budget(max_usd=0.0))
    assert outcome.claimed_success is False
    assert outcome.claim_explicit is False


# ---------------------------------------------------------------------------
# An unparseable claim is recorded, not guessed
# ---------------------------------------------------------------------------


def test_missing_result_line_is_flagged_not_assumed() -> None:
    outcome, _, _ = _run([{"text": "I think that's everything."}], Budget())
    assert outcome.stop_reason == "done"
    assert outcome.claim_explicit is False
    assert outcome.claimed_success is False


def test_explicit_claim_is_recorded_as_explicit() -> None:
    outcome, _, _ = _run([{"text": "Done.\nRESULT: SUCCESS"}], Budget())
    assert outcome.claim_explicit is True
    assert outcome.claimed_success is True


# ---------------------------------------------------------------------------
# An LLM failure costs one run, not the sweep
# ---------------------------------------------------------------------------


def test_llm_error_is_captured_on_the_run() -> None:
    from chaosagent.agents.loop import run_agent

    class Exploding(ScriptedLLM):
        async def complete(self, system, messages, tools):
            raise RuntimeError("upstream on fire")

    env = Environment(seed=1, init_state=base_state())
    faulty = FaultInjector(FaultSpec(fault_class="none"), seed=1).wrap(env)
    outcome = asyncio.run(
        run_agent(TASK, faulty, Exploding([]), get_config("naive"), Budget())
    )
    assert outcome.stop_reason == "error"
    assert "upstream on fire" in (outcome.error or "")


# ---------------------------------------------------------------------------
# The trace store and the resumable, cost-bounded sweep
# ---------------------------------------------------------------------------


def _result(run_id: str, experiment: str = "t", usd: float = 0.1) -> RunResult:
    return RunResult(
        run_id=run_id,
        experiment=experiment,
        task_id="place_and_charge_01",
        config="naive",
        model="claude-haiku-4-5",
        seed=1,
        usd=usd,
    )


def test_trace_round_trip(tmp_path) -> None:
    with TraceWriter(tmp_path / "t.duckdb") as trace:
        trace.write_run(
            _result("r1"),
            template="place_and_charge",
            claim_explicit=True,
            calls=[
                {
                    "call_index": 0,
                    "tool": "charge_payment",
                    "tool_kind": "write",
                    "args_json": "{}",
                    "idempotency_key": None,
                    "fault_applied": "partial_write",
                    "fault_outcome": "suppress",
                    "env_executed": True,
                    "agent_saw_error": True,
                    "payload_json": None,
                }
            ],
            llm_calls=[
                {
                    "step": 1,
                    "cache_hit": False,
                    "prompt_hash": "abc",
                    "tokens_in": 10,
                    "tokens_out": 2,
                    "latency_ms": 5,
                }
            ],
        )
        assert trace.completed_run_ids("t") == {"r1"}
        assert trace.spent_usd("t") == 0.1
        row = trace.conn.execute(
            "SELECT env_executed, agent_saw_error FROM calls WHERE run_id='r1'"
        ).fetchone()
        assert row == (True, True)  # the trap, at the schema level


def test_rewriting_a_run_replaces_rather_than_duplicates(tmp_path) -> None:
    with TraceWriter(tmp_path / "t.duckdb") as trace:
        for _ in range(2):
            trace.write_run(_result("r1"), template="x", claim_explicit=True, calls=[], llm_calls=[])
        count = trace.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        assert count == 1


def test_run_ids_are_deterministic() -> None:
    args = ("e1", "task_01", "guarded", "claude-haiku-4-5", 1, None)
    assert make_run_id(*args) == make_run_id(*args)


def test_run_ids_separate_every_axis() -> None:
    base = ("e1", "task_01", "guarded", "claude-haiku-4-5", 1, None)
    variants = [
        ("e2", *base[1:]),
        (base[0], "task_02", *base[2:]),
        (*base[:2], "naive", *base[3:]),
        (*base[:3], "claude-opus-5", *base[4:]),
        (*base[:4], 2, base[5]),
        (*base[:5], FaultSpec(fault_class="timeout")),
    ]
    ids = {make_run_id(*base)} | {make_run_id(*v) for v in variants}
    assert len(ids) == len(variants) + 1


def test_sweep_skips_runs_already_recorded(tmp_path) -> None:
    """Killing a sweep at 80% must not cost the 80%."""
    exp = Experiment(
        name="resume_test",
        configs=["naive"],
        faults=["none"],
        tasks="sample:2",
        seeds=[1],
        models=["claude-haiku-4-5"],
    )
    cells = exp.cells()
    assert len(cells) == 2

    with TraceWriter(tmp_path / "t.duckdb") as trace:
        trace.write_run(
            _result(cells[0].run_id("resume_test"), experiment="resume_test"),
            template="x",
            claim_explicit=True,
            calls=[],
            llm_calls=[],
        )
        report = asyncio.run(
            run_experiment(exp, trace=trace, cache=ResponseCache(enabled=False), dry_run=True)
        )
    assert report.planned == 2
    assert report.skipped == 1


def test_dry_run_spends_nothing(tmp_path) -> None:
    exp = Experiment(
        name="dry",
        configs=["naive", "guarded"],
        faults=["timeout", "none"],
        tasks="sample:1",
        seeds=[1],
        models=["claude-haiku-4-5"],
    )
    with TraceWriter(tmp_path / "t.duckdb") as trace:
        report = asyncio.run(
            run_experiment(exp, trace=trace, cache=ResponseCache(enabled=False), dry_run=True)
        )
    assert report.planned == 4
    assert report.executed == 0
    assert report.usd == 0.0


def test_prior_spend_counts_against_the_ceiling(tmp_path) -> None:
    """A resumed sweep must not restart the budget from zero."""
    exp = Experiment(
        name="ceiling",
        configs=["naive"],
        faults=["none"],
        tasks="sample:1",
        seeds=[1],
        models=["claude-haiku-4-5"],
        max_usd_total=1.0,
    )
    with TraceWriter(tmp_path / "t.duckdb") as trace:
        trace.write_run(
            _result("other", experiment="ceiling", usd=0.9),
            template="x",
            claim_explicit=True,
            calls=[],
            llm_calls=[],
        )
        report = asyncio.run(
            run_experiment(exp, trace=trace, cache=ResponseCache(enabled=False), dry_run=True)
        )
    assert report.usd == 0.9


# ---------------------------------------------------------------------------
# Experiment definitions
# ---------------------------------------------------------------------------


def test_every_shipped_experiment_parses_and_has_a_ceiling() -> None:
    from pathlib import Path

    files = sorted(Path("experiments").glob("*.yaml"))
    assert len(files) >= 5
    for path in files:
        exp = Experiment.from_yaml(path)
        assert exp.cells(), path.name
        assert exp.max_usd_total > 0, path.name


def test_reduced_grids_fit_the_declared_budget() -> None:
    """The five default experiments must total under $10 of ceiling."""
    from pathlib import Path

    total = sum(
        Experiment.from_yaml(p).max_usd_total
        for p in sorted(Path("experiments").glob("e[1-5]_*.yaml"))
        if "full" not in p.name
    )
    assert total <= 10.0, f"declared ceilings total ${total:.2f}"


def test_position_schedule_expands_to_three_buckets() -> None:
    exp = Experiment.from_yaml("experiments/e2_position.yaml")
    targets = {c.fault.target for c in exp.cells() if c.fault and not c.fault.is_control}
    assert targets == {"position:early", "position:mid", "position:late"}


def test_rate_schedule_expands_to_every_rate() -> None:
    exp = Experiment.from_yaml("experiments/e5_rate.yaml")
    rates = {c.fault.rate for c in exp.cells() if c.fault and not c.fault.is_control}
    assert rates == {0.05, 0.15, 0.3}


def test_model_sweep_covers_three_tiers() -> None:
    exp = Experiment.from_yaml("experiments/e4_models.yaml")
    assert len({c.model for c in exp.cells()}) == 3
