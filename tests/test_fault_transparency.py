"""No fault metadata reaches anything the agent can see.

Gate 2, second half, and the single test that protects the validity of every
number the project reports. If the agent could tell an injected failure from a
real one — by a marker in the payload, a distinctive error string, or an
attribute on the environment object — then every recovery rate would be
measuring detection of the harness rather than recovery from the fault.

The `oracle` configuration is the deliberate exception, and it is exempted at
the *agent* layer by being handed the injection records explicitly. Nothing is
relaxed here.
"""

from __future__ import annotations

import json

import pytest

from chaosagent.env import Environment
from chaosagent.env.world import Environment as RealEnvironment
from chaosagent.faults import FAULT_CLASSES, FaultInjector, FaultSpec
from chaosagent.tasks import default_task_set
from chaosagent.tasks.solver import TEMPLATES_BY_NAME, run_plan

TASK_SET = default_task_set()

#: Words that only exist because a harness is present. None may appear in
#: anything the agent is shown.
#:
#: `timeout` and `rate_limit` are fault-class names but are deliberately *not*
#: banned: they are also the standard transport error-code vocabulary, and a
#: real API returns exactly those strings. Banning them would be testing
#: spelling rather than leakage. The property that actually matters for those
#: two — that the visible code vocabulary is shared between real and injected
#: failures — is asserted separately in
#: `test_visible_error_codes_are_a_shared_vocabulary`.
_NOT_ALSO_ERROR_CODES = tuple(
    f for f in FAULT_CLASSES if f not in ("timeout", "rate_limit", "malformed")
)

FORBIDDEN = (
    "inject",
    "fault",
    "chaos",
    "corrupt",
    "snapshot",
    "suppress",
    "harness",
    "committed",
    "pre-write",
    "pre_write",
    "truncated to",
    "dropped keys",
    "stringified",
    "emptied",
    "nulled",
    "replayed",
    *_NOT_ALSO_ERROR_CODES,
)


def _trajectory(task) -> list[tuple[str, dict]]:
    env = Environment(seed=0, init_state=task.init_state)
    run_plan(env, TEMPLATES_BY_NAME[task.template], task.params)
    return [(c.tool, c.args) for c in env.call_log()]


_SAMPLE_TASKS = [
    next(t for t in TASK_SET.tasks if t.template == name)
    for name in sorted({t.template for t in TASK_SET.tasks})
]


def _agent_visible(fault_class: str, task, seed: int) -> tuple[list, list]:
    """Returns (what the agent saw, what was actually injected)."""
    calls = _trajectory(task)
    env = Environment(seed=0, init_state=task.init_state)
    faulty = FaultInjector(
        FaultSpec(fault_class=fault_class), seed=seed, trajectory_hint=task.min_calls
    ).wrap(env)
    seen = [faulty.call(tool, args) for tool, args in calls]
    return seen, faulty.injected()


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fault_class", FAULT_CLASSES)
def test_no_forbidden_word_reaches_the_agent(fault_class: str) -> None:
    for task in _SAMPLE_TASKS:
        for seed in (1, 2, 3):
            seen, _ = _agent_visible(fault_class, task, seed)
            blob = json.dumps([r.model_dump() for r in seen], default=str).lower()
            for word in FORBIDDEN:
                assert word not in blob, f"{task.id}/{fault_class}/seed{seed}: '{word}'"


@pytest.mark.parametrize("fault_class", FAULT_CLASSES)
def test_injection_detail_never_leaks(fault_class: str) -> None:
    """The `detail` field is written for the trace and is full of giveaways."""
    for task in _SAMPLE_TASKS:
        seen, records = _agent_visible(fault_class, task, seed=5)
        if not records:
            continue
        blob = json.dumps([r.model_dump() for r in seen], default=str)
        for record in records:
            if record.detail:
                assert record.detail not in blob


@pytest.mark.parametrize("fault_class", FAULT_CLASSES)
def test_tool_results_keep_their_shape(fault_class: str) -> None:
    """A corrupted result is still a ToolResult with the same field types."""
    for task in _SAMPLE_TASKS:
        seen, _ = _agent_visible(fault_class, task, seed=4)
        for result in seen:
            assert isinstance(result.ok, bool)
            assert result.payload is None or isinstance(result.payload, dict | list | str)
            if result.ok:
                assert result.error is None
            else:
                assert result.error is not None


def test_visible_error_codes_are_a_shared_vocabulary() -> None:
    """No error code exists that only the harness could have produced.

    Every code an injected fault emits is one a real transport layer emits, and
    every code is drawn from the single `ErrorCode` literal that the environment
    itself uses. If the injector had its own private code, "was this injected?"
    would be a string comparison.
    """
    from typing import get_args

    from chaosagent.types import ErrorCode

    declared = set(get_args(ErrorCode))
    seen_codes: set[str] = set()
    for fault_class in FAULT_CLASSES:
        for task in _SAMPLE_TASKS:
            for seed in (1, 2, 3):
                seen, _ = _agent_visible(fault_class, task, seed)
                seen_codes |= {r.error.code for r in seen if r.error is not None}

    assert seen_codes <= declared, seen_codes - declared
    # And the transport codes are not exclusive to injection: the environment
    # raises UNKNOWN with the same shape on a storage error.
    assert "TIMEOUT" in declared and "RATE_LIMIT" in declared


def test_error_envelopes_have_no_extra_fields() -> None:
    """An injected envelope must have exactly the fields a real one has."""
    real = Environment(seed=0).call("get_order", {"order_id": "nope"})
    assert real.error is not None
    real_fields = set(real.error.model_dump())
    for fault_class in FAULT_CLASSES:
        for task in _SAMPLE_TASKS:
            seen, _ = _agent_visible(fault_class, task, seed=6)
            for result in seen:
                if result.error is not None:
                    assert set(result.error.model_dump()) == real_fields


# ---------------------------------------------------------------------------
# The environment object itself must not give the game away
# ---------------------------------------------------------------------------


def test_faulty_environment_exposes_the_same_call_surface() -> None:
    """An agent that probes its environment must find nothing unusual."""
    env = Environment(seed=0)
    faulty = FaultInjector(FaultSpec(fault_class="timeout"), seed=1).wrap(env)
    for name in ("call", "state", "call_log", "check_invariants", "snapshot", "clock"):
        assert hasattr(faulty, name), name
        assert hasattr(RealEnvironment, name), name


def test_injection_records_are_not_reachable_from_a_result() -> None:
    seen, records = _agent_visible("partial_write", _SAMPLE_TASKS[0], seed=1)
    for result in seen:
        assert not hasattr(result, "injection")
        assert not hasattr(result, "fault_applied")
        assert "injection" not in result.model_dump()


# ---------------------------------------------------------------------------
# The specific indistinguishability the study depends on
# ---------------------------------------------------------------------------


def test_a_suppressed_write_looks_exactly_like_a_blocked_one() -> None:
    """The entire argument for verification reads.

    partial_write (the world moved) and a pre-commit timeout (it did not) must
    be indistinguishable from the error alone. Only reading the world tells
    them apart.
    """
    from chaosagent.env.fixtures import base_state

    def charge_error(fault_class: str, **params):
        env = Environment(seed=0, init_state=base_state())
        faulty = FaultInjector(
            FaultSpec(
                fault_class=fault_class, target="tool:charge_payment", params=params
            ),
            seed=1,
            trajectory_hint=5,
        ).wrap(env)
        faulty.call("create_order", {"customer_id": "cus_1"})
        faulty.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-CABL", "qty": 1})
        faulty.call("reserve_stock", {"order_id": "ord_9001"})
        result = faulty.call("charge_payment", {"order_id": "ord_9001", "amount_cents": 1_200})
        return result, faulty

    committed, env_a = charge_error("timeout", committed=True)
    blocked, env_b = charge_error("timeout", committed=False)

    # Ground truth differs...
    assert len(env_a.state().payments) == 1
    assert env_b.state().payments == []
    # ...but nothing the agent received does.
    assert committed.error is not None and blocked.error is not None
    assert committed.error.model_dump() == blocked.error.model_dump()


def test_a_stale_read_is_a_structurally_valid_read() -> None:
    """It must be well-formed, or it degenerates into `malformed`."""
    from chaosagent.env.fixtures import base_state

    env = Environment(seed=0, init_state=base_state())
    faulty = FaultInjector(
        FaultSpec(fault_class="stale", target="tool:get_order"), seed=1, trajectory_hint=5
    ).wrap(env)
    faulty.call("create_order", {"customer_id": "cus_1"})
    faulty.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-CABL", "qty": 1})
    faulty.call("reserve_stock", {"order_id": "ord_9001"})
    stale = faulty.call("get_order", {"order_id": "ord_9001"})

    assert stale.ok
    assert isinstance(stale.payload, dict)
    # Same keys as a genuine read — only the values are out of date.
    fresh = env.call("get_order", {"order_id": "ord_9001"})
    assert set(stale.payload) == set(fresh.payload)
    assert stale.payload["status"] != fresh.payload["status"]


def test_control_runs_are_byte_identical_to_an_unwrapped_environment() -> None:
    """The control arm must measure task competence, not harness overhead."""
    for task in _SAMPLE_TASKS:
        calls = _trajectory(task)

        plain_env = Environment(seed=0, init_state=task.init_state)
        plain = [plain_env.call(t, a) for t, a in calls]

        wrapped_env = Environment(seed=0, init_state=task.init_state)
        wrapped = FaultInjector(FaultSpec(fault_class="none"), seed=1).wrap(wrapped_env)
        through = [wrapped.call(t, a) for t, a in calls]

        assert [r.model_dump() for r in plain] == [r.model_dump() for r in through]
        assert plain_env.digest() == wrapped_env.digest()
