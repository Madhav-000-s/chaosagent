"""The task-validation gate.

50/50 solvable by the scripted solver under zero faults, with no invariant
violation attributable to the solution. Nothing downstream is trustworthy if
this is red: an unsolvable task would be scored as a model failure.
"""

from __future__ import annotations

import re

import pytest

from chaosagent.env import Environment
from chaosagent.tasks import ReferenceSolver, TaskGenerator, default_task_set, select
from chaosagent.tasks.solver import TEMPLATES_BY_NAME, PlanError, run_plan
from chaosagent.tasks.templates import ALL_TEMPLATES

TASK_SET = default_task_set()
RESULTS = ReferenceSolver().solve_all(list(TASK_SET.tasks))


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_suite_is_exactly_fifty_tasks() -> None:
    assert len(TASK_SET) == 50


def test_all_fifty_are_solved() -> None:
    bad = [(r.task_id, r.error or r.failures) for r in RESULTS if not r.solved]
    assert bad == []


def test_no_solution_introduces_an_invariant_violation() -> None:
    bad = [(r.task_id, r.invariant_violations) for r in RESULTS if r.invariant_violations]
    assert bad == []


def test_optimal_trajectories_are_four_to_ten_calls() -> None:
    for result in RESULTS:
        assert 4 <= result.calls <= 10, f"{result.task_id}: {result.calls}"


def test_min_calls_matches_the_measured_solution() -> None:
    by_id = {r.task_id: r for r in RESULTS}
    for task in TASK_SET.tasks:
        assert task.min_calls == by_id[task.id].calls


# ---------------------------------------------------------------------------
# Suite shape
# ---------------------------------------------------------------------------


def test_all_eight_templates_are_represented() -> None:
    assert {t.template for t in TASK_SET.tasks} == {t.name for t in ALL_TEMPLATES}
    assert len(ALL_TEMPLATES) == 8


def test_task_ids_are_unique() -> None:
    ids = [t.id for t in TASK_SET.tasks]
    assert len(set(ids)) == len(ids)


def test_every_task_exercises_a_non_idempotent_write() -> None:
    """The study is about non-idempotent operations; a task without one is a
    control, and there are none in this suite by design."""
    assert all(t.involves_non_idempotent for t in TASK_SET.tasks)


def test_generation_is_deterministic() -> None:
    a = TaskGenerator().generate()
    b = TaskGenerator().generate()
    assert a.content_hash() == b.content_hash()
    assert [t.model_dump() for t in a.tasks] == [t.model_dump() for t in b.tasks]


def test_content_hash_changes_when_a_prompt_changes() -> None:
    original = TASK_SET.tasks[0]
    mutated = original.model_copy(update={"prompt": original.prompt + " Also, hurry."})
    assert mutated.content_hash() != original.content_hash()


# ---------------------------------------------------------------------------
# Prompt hygiene — these guard the validity of the experiment
# ---------------------------------------------------------------------------


def test_prompts_never_state_an_amount() -> None:
    """The agent must read the total before charging it.

    Handing it the number in the prompt removes the read dependency that the
    `stale` and `wrong_type` faults exist to attack.
    """
    for task in TASK_SET.tasks:
        assert "cents" not in task.prompt.lower(), task.id
        assert "$" not in task.prompt, task.id


def test_prompts_never_give_safety_advice() -> None:
    """Guidance belongs to a configuration, not to a task. If a prompt told the
    agent to verify before retrying, every arm would get the intervention."""
    # Describing the *goal* is fine — the reconciliation task is allowed to say
    # the word "duplicate", since finding one is the job. What is banned is
    # advice about the agent's own tool-calling discipline, which is a
    # configuration's job to supply.
    banned = [
        "idempotency",
        "idempotent",
        "retry",
        "retries",
        "verify before",
        "check before retrying",
        "do not charge twice",
        "only charge once",
        "charge exactly once",
    ]
    for task in TASK_SET.tasks:
        lowered = task.prompt.lower()
        for phrase in banned:
            assert phrase not in lowered, f"{task.id} leaks guidance: '{phrase}'"


def test_prompts_are_substantive() -> None:
    for task in TASK_SET.tasks:
        assert len(task.prompt) > 60
        assert task.prompt.strip().endswith(".")


def test_every_prompt_names_the_entities_it_talks_about() -> None:
    """Either a customer id or an order id, so the task is actionable."""
    for task in TASK_SET.tasks:
        assert re.search(r"\b(cus_\d+|ord_\d+)\b", task.prompt), task.id


# ---------------------------------------------------------------------------
# The assertions actually discriminate
# ---------------------------------------------------------------------------


def _solved_env(task_id: str) -> Environment:
    task = TASK_SET.by_id(task_id)
    env = Environment(seed=0, init_state=task.init_state)
    run_plan(env, TEMPLATES_BY_NAME[task.template], task.params)
    return env


def test_doing_nothing_fails_every_task() -> None:
    for task in TASK_SET.tasks:
        env = Environment(seed=0, init_state=task.init_state)
        assert task.expected_terminal.failures(env.state()), task.id


def test_a_double_charge_fails_the_assertion() -> None:
    """The headline corruption must be caught by the terminal-state check."""
    task = TASK_SET.by_id("place_and_charge_01")
    env = _solved_env("place_and_charge_01")
    assert task.expected_terminal.holds(env.state())

    order_id = env.state().orders[0]["id"]
    total = int(env.state().orders[0]["total_cents"])
    env.call("charge_payment", {"order_id": order_id, "amount_cents": total})
    failures = task.expected_terminal.failures(env.state())
    assert any("charge_count" in f or "net_paid" in f for f in failures)


def test_a_stray_extra_order_fails_the_assertion() -> None:
    task = TASK_SET.by_id("place_and_charge_01")
    env = _solved_env("place_and_charge_01")
    env.call("create_order", {"customer_id": task.params["customer"]})
    assert task.expected_terminal.failures(env.state())


def test_charging_the_wrong_amount_fails_the_assertion() -> None:
    """Guards the `wrong_type` fault: 1200 cents is not 12 dollars."""
    task = TASK_SET.by_id("out_of_stock_01")
    env = Environment(seed=0, init_state=task.init_state)
    env.call("create_order", {"customer_id": task.params["customer"]})
    env.call("add_line_item", {"order_id": "ord_9001", "sku": task.params["sku_ok"], "qty": 1})
    env.call("reserve_stock", {"order_id": "ord_9001"})
    env.call("charge_payment", {"order_id": "ord_9001", "amount_cents": 1})
    assert task.expected_terminal.failures(env.state())


def test_a_double_shipment_fails_the_assertion() -> None:
    task = TASK_SET.by_id("tight_lifecycle_01")
    env = _solved_env("tight_lifecycle_01")
    assert task.expected_terminal.holds(env.state())
    order_id = [o["id"] for o in env.state().orders if o["id"] != "ord_1"][0]
    env.call("ship_order", {"order_id": order_id})
    assert any("shipment_count" in f for f in task.expected_terminal.failures(env.state()))


def test_touching_the_untouched_order_fails_the_assertion() -> None:
    """ship_paid tasks must leave the unpaid order alone."""
    task = TASK_SET.by_id("ship_paid_01")
    env = _solved_env("ship_paid_01")
    assert task.expected_terminal.holds(env.state())
    env.call("reserve_stock", {"order_id": "ord_4"})
    assert task.expected_terminal.failures(env.state())


# ---------------------------------------------------------------------------
# Plan machinery
# ---------------------------------------------------------------------------


def test_plan_error_when_a_declared_failure_does_not_happen() -> None:
    """A plan that lies about a step's outcome is a malformed template.

    The out_of_stock plan declares its reservation will be refused. Run it
    against a world where the stock is present and that declaration is false.
    """
    from chaosagent.env.fixtures import base_state

    task = TASK_SET.by_id("out_of_stock_01")
    env = Environment(seed=0, init_state=base_state())  # everything in stock
    with pytest.raises(PlanError, match="expected ok=False"):
        run_plan(env, TEMPLATES_BY_NAME["out_of_stock"], task.params)


def test_expected_failures_are_declared_not_accidental() -> None:
    """out_of_stock reserves stock it knows is unavailable, on purpose."""
    task = TASK_SET.by_id("out_of_stock_01")
    env = Environment(seed=0, init_state=task.init_state)
    run_plan(env, TEMPLATES_BY_NAME["out_of_stock"], task.params)
    refusals = [c for c in env.call_log() if not c.ok]
    assert [c.error_code for c in refusals] == ["INSUFFICIENT_STOCK"]


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_select_all() -> None:
    assert len(select(TASK_SET, "all")) == 50


def test_select_by_template() -> None:
    picked = select(TASK_SET, "template:reconcile")
    assert len(picked) == 6
    assert all(t.template == "reconcile" for t in picked)


def test_select_by_id() -> None:
    picked = select(TASK_SET, "place_and_charge_01,ship_paid_02")
    assert [t.id for t in picked] == ["place_and_charge_01", "ship_paid_02"]


def test_sample_is_stratified_across_templates() -> None:
    """A reduced grid must still touch every template."""
    picked = select(TASK_SET, "sample:8")
    assert len(picked) == 8
    assert len({t.template for t in picked}) == 8


def test_sample_is_deterministic() -> None:
    a = [t.id for t in select(TASK_SET, "sample:16")]
    b = [t.id for t in select(TASK_SET, "sample:16")]
    assert a == b


def test_sample_larger_than_suite_is_clamped() -> None:
    assert len(select(TASK_SET, "sample:999")) == 50


def test_unknown_task_id_raises() -> None:
    with pytest.raises(KeyError):
        TASK_SET.by_id("nope_99")
