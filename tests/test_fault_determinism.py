"""Every fault class produces an identical outcome under a fixed seed.

Gate 2, first half. A fault that varies run to run makes the configuration
comparison meaningless, because two arms would no longer be facing the same
world.
"""

from __future__ import annotations

import pytest

from chaosagent.env import Environment
from chaosagent.faults import FAULT_CLASSES, FaultInjector, FaultSpec, get_fault
from chaosagent.faults.schedule import (
    AtOrAfterIndex,
    AtTool,
    Never,
    Stochastic,
    bucket_for,
    build_schedule,
    position_index,
)
from chaosagent.tasks import default_task_set
from chaosagent.tasks.solver import TEMPLATES_BY_NAME, run_plan

TASK_SET = default_task_set()

#: A trajectory that touches reads, non-idempotent writes and a shipment.
SCRIPT: list[tuple[str, dict]] = [
    ("get_inventory", {"sku": "SKU-KEYB"}),
    ("create_order", {"customer_id": "cus_1"}),
    ("add_line_item", {"order_id": "ord_9001", "sku": "SKU-KEYB", "qty": 2}),
    ("reserve_stock", {"order_id": "ord_9001"}),
    ("get_order", {"order_id": "ord_9001"}),
    ("charge_payment", {"order_id": "ord_9001", "amount_cents": 25_800}),
    ("get_payment_status", {"order_id": "ord_9001"}),
    ("ship_order", {"order_id": "ord_9001"}),
]


def _run(fault_class: str, seed: int = 42, target: str = "random", **params):
    from chaosagent.env.fixtures import base_state

    env = Environment(seed=0, init_state=base_state())
    spec = FaultSpec(fault_class=fault_class, target=target, params=params)
    faulty = FaultInjector(spec, seed=seed, trajectory_hint=len(SCRIPT)).wrap(env)
    results = [faulty.call(tool, args) for tool, args in SCRIPT]
    return faulty, results


def _signature(faulty, results) -> str:
    """Everything that must be reproducible, flattened to one comparable blob."""
    import json

    return json.dumps(
        {
            "results": [r.model_dump() for r in results],
            "injected": [i.model_dump() for i in faulty.injected()],
            "history": [
                {
                    "i": c.call_index,
                    "tool": c.tool,
                    "executed": c.env_executed,
                    "saw_error": c.agent_saw_error,
                    "fault": c.fault_applied,
                }
                for c in faulty.history
            ],
            "state": faulty.state().digest(),
        },
        sort_keys=True,
        default=str,
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fault_class", FAULT_CLASSES)
def test_identical_outcome_under_fixed_seed_100x(fault_class: str) -> None:
    reference = _signature(*_run(fault_class))
    for _ in range(100):
        assert _signature(*_run(fault_class)) == reference


@pytest.mark.parametrize("fault_class", FAULT_CLASSES)
def test_every_class_actually_lands_somewhere(fault_class: str) -> None:
    """A fault that never fires would quietly turn a faulted arm into a control."""
    landed = False
    for seed in range(12):
        faulty, _ = _run(fault_class, seed=seed)
        if faulty.injected():
            landed = True
            break
    assert landed, f"{fault_class} never injected across 12 seeds"


@pytest.mark.parametrize("fault_class", FAULT_CLASSES)
def test_different_seeds_can_differ(fault_class: str) -> None:
    """Determinism must not have been achieved by ignoring the seed."""
    signatures = {_signature(*_run(fault_class, seed=s)) for s in range(12)}
    assert len(signatures) > 1, f"{fault_class} ignores its seed"


def test_control_arm_injects_nothing() -> None:
    faulty, results = _run("none")
    assert faulty.injected() == []
    assert all(c.fault_applied is None for c in faulty.history)
    assert all(r.ok for r in results)


# ---------------------------------------------------------------------------
# Outcome semantics — the four-way split
# ---------------------------------------------------------------------------


def test_partial_write_executes_but_reports_failure() -> None:
    """The trap: the world moved, the agent was told it did not."""
    faulty, _ = _run("partial_write", target="tool:charge_payment")
    record = faulty.injected()[0]
    assert record.outcome == "suppress"
    assert record.env_executed is True
    call = faulty.history[record.call_index]
    assert call.env_executed and call.agent_saw_error
    # The charge really is in the ledger.
    assert len(faulty.state().payments) == 1


def test_timeout_pre_commit_does_not_execute() -> None:
    faulty, _ = _run("timeout", target="tool:charge_payment", committed=False)
    record = faulty.injected()[0]
    assert record.outcome == "block"
    assert record.env_executed is False
    assert faulty.state().payments == []


def test_timeout_post_commit_does_execute() -> None:
    faulty, _ = _run("timeout", target="tool:charge_payment", committed=True)
    record = faulty.injected()[0]
    assert record.outcome == "suppress"
    assert record.env_executed is True
    assert len(faulty.state().payments) == 1


def test_both_timeout_variants_produce_identical_envelopes() -> None:
    """If they differed, the agent could read ground truth off the error and
    the guarded config's advantage would be an artefact."""
    _, committed = _run("timeout", target="tool:charge_payment", committed=True, timeout_ms=5000)
    _, blocked = _run("timeout", target="tool:charge_payment", committed=False, timeout_ms=5000)
    a = next(r.error for r in committed if r.error and r.error.code == "TIMEOUT")
    b = next(r.error for r in blocked if r.error and r.error.code == "TIMEOUT")
    assert a == b


def test_corrupting_faults_execute_and_report_success() -> None:
    for fault_class in ("malformed", "wrong_type", "silent_empty", "stale"):
        faulty, _ = _run(fault_class, seed=3)
        records = faulty.injected()
        if not records:
            continue
        assert records[0].outcome == "corrupt", fault_class
        assert records[0].env_executed is True, fault_class
        assert faulty.history[records[0].call_index].agent_saw_error is False, fault_class


def test_delay_faults_fail_then_succeed() -> None:
    for fault_class in ("flaky", "rate_limit"):
        faulty, _ = _run(fault_class, target="tool:get_order", times=2)
        errors = [c for c in faulty.history if c.tool == "get_order" and c.agent_saw_error]
        assert len(errors) == 1, fault_class
        assert not errors[0].env_executed, fault_class


def test_flaky_marks_state_unchanged_but_partial_write_does_not() -> None:
    """The contrast that makes blind retry correct in one case and wrong in the
    other. If both said the same thing there would be nothing to attribute."""
    _, flaky = _run("flaky", target="tool:charge_payment")
    _, partial = _run("partial_write", target="tool:charge_payment")
    flaky_err = next(r.error for r in flaky if r.error)
    partial_err = next(r.error for r in partial if r.error)
    assert flaky_err.state_may_have_changed is False
    assert partial_err.state_may_have_changed is True


# ---------------------------------------------------------------------------
# Injection policy
# ---------------------------------------------------------------------------


def test_single_schedules_inject_at_most_once() -> None:
    for fault_class in FAULT_CLASSES:
        for seed in range(6):
            faulty, _ = _run(fault_class, seed=seed)
            assert len(faulty.injected()) <= 1, fault_class


def test_stochastic_can_inject_more_than_once() -> None:
    from chaosagent.env.fixtures import base_state

    counts = []
    for seed in range(10):
        env = Environment(seed=0, init_state=base_state())
        spec = FaultSpec(fault_class="flaky", target="stochastic", rate=0.9)
        faulty = FaultInjector(spec, seed=seed, trajectory_hint=len(SCRIPT)).wrap(env)
        for tool, args in SCRIPT:
            faulty.call(tool, args)
        counts.append(len(faulty.injected()))
    assert max(counts) > 1


def test_faults_never_fire_on_a_call_the_world_would_reject() -> None:
    """Corrupting an already-failing call would measure the agent's reaction to
    the environment rather than to the fault."""
    from chaosagent.env.fixtures import base_state

    env = Environment(seed=0, init_state=base_state())
    spec = FaultSpec(fault_class="partial_write", target="call_index:0")
    faulty = FaultInjector(spec, seed=1, trajectory_hint=4).wrap(env)

    # This call is refused on its own merits: no such order.
    rejected = faulty.call("charge_payment", {"order_id": "nope", "amount_cents": 100})
    assert not rejected.ok
    assert rejected.error is not None and rejected.error.code == "NOT_FOUND"
    assert faulty.injected() == []

    # The fault is still armed and lands on the next eligible call.
    faulty.call("create_order", {"customer_id": "cus_1"})
    assert len(faulty.injected()) == 1


def test_ineligible_tools_keep_the_fault_armed() -> None:
    """partial_write applies only to writes, so a read-heavy prefix must not
    consume the injection."""
    from chaosagent.env.fixtures import base_state

    env = Environment(seed=0, init_state=base_state())
    spec = FaultSpec(fault_class="partial_write", target="call_index:0")
    faulty = FaultInjector(spec, seed=1, trajectory_hint=4).wrap(env)
    faulty.call("get_inventory", {})
    faulty.call("search_products", {})
    assert faulty.injected() == []
    faulty.call("create_order", {"customer_id": "cus_1"})
    assert len(faulty.injected()) == 1
    assert faulty.injected()[0].call_index == 2


def test_applies_to_matches_the_declared_surface() -> None:
    from chaosagent.env import registry

    for spec in registry.specs():
        assert get_fault("partial_write").applies_to(spec.name, spec) == (spec.kind == "write")
        assert get_fault("stale").applies_to(spec.name, spec) == (spec.kind == "read")
        assert get_fault("silent_empty").applies_to(spec.name, spec) == (spec.kind == "read")
        assert get_fault("timeout").applies_to(spec.name, spec)


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


def test_schedule_parsing() -> None:
    from random import Random

    assert isinstance(build_schedule(FaultSpec(fault_class="none"), Random(0), 5), Never)
    assert isinstance(
        build_schedule(FaultSpec(fault_class="flaky", target="call_index:2"), Random(0), 5),
        AtOrAfterIndex,
    )
    assert isinstance(
        build_schedule(FaultSpec(fault_class="flaky", target="tool:get_order"), Random(0), 5),
        AtTool,
    )
    assert isinstance(
        build_schedule(
            FaultSpec(fault_class="flaky", target="stochastic", rate=0.1), Random(0), 5
        ),
        Stochastic,
    )


def test_unknown_target_is_rejected() -> None:
    from random import Random

    with pytest.raises(ValueError, match="unrecognised fault target"):
        build_schedule(FaultSpec(fault_class="flaky", target="whenever"), Random(0), 5)


def test_position_buckets_are_ordered_and_in_range() -> None:
    for length in range(1, 11):
        early = position_index("early", length)
        mid = position_index("mid", length)
        late = position_index("late", length)
        assert 0 <= early <= mid <= late < max(1, length)


def test_bucket_for_is_the_inverse_mapping() -> None:
    assert bucket_for(0, 10) == "early"
    assert bucket_for(5, 10) == "mid"
    assert bucket_for(9, 10) == "late"


def test_unknown_fault_class_is_rejected() -> None:
    with pytest.raises(KeyError, match="unknown fault class"):
        get_fault("gremlins")


# ---------------------------------------------------------------------------
# Against the real task suite
# ---------------------------------------------------------------------------


def _clean_trajectory(task) -> list[tuple[str, dict]]:
    """The exact call sequence the reference solver makes with no faults."""
    env = Environment(seed=0, init_state=task.init_state)
    run_plan(env, TEMPLATES_BY_NAME[task.template], task.params)
    return [(c.tool, c.args) for c in env.call_log()]


#: One task per template, so every trajectory shape is covered.
_SAMPLE_TASKS = [
    next(t for t in TASK_SET.tasks if t.template == name)
    for name in sorted({t.template for t in TASK_SET.tasks})
]


@pytest.mark.parametrize("fault_class", FAULT_CLASSES)
def test_injection_over_real_task_trajectories(fault_class: str) -> None:
    """Injecting into a real trajectory must never raise out of the injector.

    The scripted solver is deliberately fragile — it indexes straight into
    payloads — so it is *not* replayed here. What is replayed is the exact call
    sequence it produced on a clean run, which is a real trajectory shape
    without the brittleness. Anything the injector throws is a harness bug.
    """
    for task in _SAMPLE_TASKS:
        calls = _clean_trajectory(task)
        env = Environment(seed=0, init_state=task.init_state)
        spec = FaultSpec(fault_class=fault_class)
        faulty = FaultInjector(spec, seed=7, trajectory_hint=task.min_calls).wrap(env)
        for tool, args in calls:
            result = faulty.call(tool, args)
            assert result.ok or result.error is not None, f"{task.id}/{fault_class}"
        assert len(faulty.history) == len(calls)


@pytest.mark.parametrize("fault_class", FAULT_CLASSES)
def test_real_trajectories_are_deterministic(fault_class: str) -> None:
    def once(task) -> str:
        import json

        calls = _clean_trajectory(task)
        env = Environment(seed=0, init_state=task.init_state)
        faulty = FaultInjector(
            FaultSpec(fault_class=fault_class), seed=11, trajectory_hint=task.min_calls
        ).wrap(env)
        results = [faulty.call(tool, args) for tool, args in calls]
        return json.dumps(
            [r.model_dump() for r in results] + [i.model_dump() for i in faulty.injected()],
            sort_keys=True,
            default=str,
        )

    for task in _SAMPLE_TASKS:
        assert once(task) == once(task), f"{task.id}/{fault_class}"
