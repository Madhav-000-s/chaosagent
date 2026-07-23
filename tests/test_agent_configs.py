"""The configurations, exercised end to end against a scripted model.

The centrepiece is `test_the_headline_result`: the identical agent, the
identical fault, the identical retry — and a double charge under `naive` that
does not happen under `guarded`. No API key involved; the mechanism is
structural, which is the claim.
"""

from __future__ import annotations

import asyncio

import pytest

from chaosagent.agents.configs import CONFIGS, DECOMPOSITION, HEADLINE, get_config
from chaosagent.agents.loop import parse_claim, run_agent, tool_schemas
from chaosagent.agents.strategies import (
    IdempotencyKeyDecorator,
    contract_guidance,
    verification_call,
)
from chaosagent.env import registry
from chaosagent.env.fixtures import base_state
from chaosagent.env.invariants import no_double_charge
from chaosagent.env.world import Environment
from chaosagent.faults import FaultInjector, FaultSpec
from chaosagent.runtime.types import Budget
from chaosagent.types import ErrorEnvelope

from .conftest import ScriptedLLM

#: Reach a reserved order, then charge it. The charge is where faults bite.
TO_CHARGE = [
    {"tool": "create_order", "args": {"customer_id": "cus_1"}},
    {"tool": "add_line_item", "args": {"order_id": "ord_9001", "sku": "SKU-CABL", "qty": 1}},
    {"tool": "reserve_stock", "args": {"order_id": "ord_9001"}},
    {"tool": "charge_payment", "args": {"order_id": "ord_9001", "amount_cents": 1_200}},
    {"text": "Order placed and charged.\nRESULT: SUCCESS"},
]


def _run(config_name: str, fault: str, *, retry_on_error: bool = True, seed: int = 1):
    env = Environment(seed=seed, init_state=base_state())
    spec = FaultSpec(fault_class=fault, target="tool:charge_payment")
    faulty = FaultInjector(spec, seed=seed, trajectory_hint=5).wrap(env)
    llm = ScriptedLLM(TO_CHARGE, retry_on_error=retry_on_error)
    task = _stub_task()
    outcome = asyncio.run(run_agent(task, faulty, llm, get_config(config_name), Budget()))
    return env, faulty, outcome


def _stub_task():
    from chaosagent.tasks import default_task_set

    return default_task_set().by_id("place_and_charge_01")


# ---------------------------------------------------------------------------
# The headline
# ---------------------------------------------------------------------------


def test_the_headline_result() -> None:
    """Same fault, same retry: `naive` double-charges, `guarded` does not."""
    naive_env, _, naive_out = _run("naive", "partial_write")
    guarded_env, _, guarded_out = _run("guarded", "partial_write")

    naive_charges = [p for p in naive_env.state().payments if p["kind"] == "charge"]
    guarded_charges = [p for p in guarded_env.state().payments if p["kind"] == "charge"]

    assert len(naive_charges) == 2, "the trap did not spring"
    assert len(guarded_charges) == 1, "the idempotency key did not hold"

    assert no_double_charge(naive_env) != []
    assert no_double_charge(guarded_env) == []

    # And the naive agent reports success while the customer has been charged twice.
    assert naive_out.claimed_success is True


def test_the_world_really_did_execute_the_suppressed_write() -> None:
    """partial_write means the charge landed even though the agent saw an error."""
    env, faulty, _ = _run("naive", "partial_write", retry_on_error=False)
    record = faulty.injected()[0]
    assert record.env_executed is True
    assert faulty.history[record.call_index].agent_saw_error is True
    assert len(env.state().payments) == 1


def test_blind_retry_config_also_double_charges() -> None:
    """`retry` is the framework default, and the framework default is unsafe."""
    env, _, _ = _run("retry", "partial_write", retry_on_error=False)
    charges = [p for p in env.state().payments if p["kind"] == "charge"]
    assert len(charges) > 1


def test_idempotency_key_makes_blind_retry_safe() -> None:
    """Identical code path, key attached: the retries collapse to one execution."""
    env = Environment(seed=1, init_state=base_state())
    faulty = FaultInjector(
        FaultSpec(fault_class="partial_write", target="tool:charge_payment"),
        seed=1,
        trajectory_hint=5,
    ).wrap(env)
    llm = ScriptedLLM(TO_CHARGE, retry_on_error=False)
    config = get_config("contract+idem").__class__(
        name="retry+idem",
        error_formatter=get_config("retry").error_formatter,
        recovery_policy=get_config("retry").recovery_policy,
        call_decorator=IdempotencyKeyDecorator(),
        system_prompt=get_config("retry").system_prompt,
    )
    asyncio.run(run_agent(_stub_task(), faulty, llm, config, Budget()))
    assert len([p for p in env.state().payments if p["kind"] == "charge"]) == 1


def test_control_arm_completes_cleanly() -> None:
    env, faulty, outcome = _run("naive", "none", retry_on_error=False)
    assert faulty.injected() == []
    assert outcome.claimed_success is True
    assert outcome.stop_reason == "done"
    assert len(env.state().payments) == 1


# ---------------------------------------------------------------------------
# Verification reads
# ---------------------------------------------------------------------------


def test_verify_read_costs_an_extra_call_and_reveals_the_truth() -> None:
    _, faulty, _ = _run("contract+verify", "partial_write", retry_on_error=False)
    reads = [c for c in faulty.history if c.tool == "get_payment_status"]
    assert reads, "no verification read was issued"


def test_no_verify_read_without_the_flag() -> None:
    _, faulty, _ = _run("contract", "partial_write", retry_on_error=False)
    assert not [c for c in faulty.history if c.tool == "get_payment_status"]


def test_verification_read_mapping_covers_every_write() -> None:
    for spec in registry.specs():
        if spec.kind != "write":
            continue
        args = {"order_id": "ord_1", "customer_id": "cus_1"}
        assert verification_call(spec.name, args) is not None, spec.name


def test_flaky_does_not_trigger_a_verification_read() -> None:
    """A 503 before processing changed nothing, so verifying would be pure cost."""
    _, faulty, _ = _run("guarded", "flaky", retry_on_error=False)
    assert not [c for c in faulty.history if c.tool == "get_payment_status"]


# ---------------------------------------------------------------------------
# Strategy objects
# ---------------------------------------------------------------------------


def test_idempotency_key_is_stable_for_the_same_logical_call() -> None:
    decorator = IdempotencyKeyDecorator()
    spec = registry.spec_for("charge_payment")
    args = {"order_id": "ord_1", "amount_cents": 500}
    assert decorator.decorate("charge_payment", args, spec)["idempotency_key"] == (
        decorator.decorate("charge_payment", dict(args), spec)["idempotency_key"]
    )


def test_idempotency_key_differs_for_a_different_call() -> None:
    decorator = IdempotencyKeyDecorator()
    spec = registry.spec_for("charge_payment")
    a = decorator.decorate("charge_payment", {"order_id": "ord_1", "amount_cents": 500}, spec)
    b = decorator.decorate("charge_payment", {"order_id": "ord_1", "amount_cents": 900}, spec)
    assert a["idempotency_key"] != b["idempotency_key"]


def test_decorator_leaves_reads_alone() -> None:
    decorator = IdempotencyKeyDecorator()
    spec = registry.spec_for("get_order")
    assert decorator.decorate("get_order", {"order_id": "x"}, spec) == {"order_id": "x"}


def test_raw_formatter_drops_the_machine_readable_fields() -> None:
    envelope = ErrorEnvelope(
        code="TIMEOUT",
        message="timed out",
        retryable=True,
        state_may_have_changed=True,
        idempotency_supported=True,
        hint="verify first",
    )
    raw = get_config("naive").error_formatter.format(envelope)
    assert "timed out" in raw
    assert "state_may_have_changed" not in raw


def test_envelope_formatter_keeps_them() -> None:
    envelope = ErrorEnvelope(
        code="TIMEOUT",
        message="timed out",
        retryable=True,
        state_may_have_changed=True,
        idempotency_supported=True,
        hint="verify first",
    )
    text = get_config("contract").error_formatter.format(envelope)
    assert "state_may_have_changed" in text
    assert "idempotency_supported" in text


def test_contract_guidance_tracks_the_envelope_not_the_fault() -> None:
    uncertain = ErrorEnvelope(
        code="TIMEOUT", message="m", retryable=True, state_may_have_changed=True,
        idempotency_supported=True,
    )
    safe = ErrorEnvelope(
        code="UNAVAILABLE", message="m", retryable=True, state_may_have_changed=False,
        idempotency_supported=True,
    )
    assert "read the affected entity" in contract_guidance(uncertain).lower()
    assert "safe" in contract_guidance(safe).lower()


# ---------------------------------------------------------------------------
# Fault transparency at the agent layer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("config", [c for c in HEADLINE if c != "oracle"])
def test_no_config_but_oracle_sees_fault_metadata(config: str) -> None:
    """The gate from phase 3, re-asserted where it actually matters."""
    _, _, _ = _run(config, "partial_write", retry_on_error=False)
    env = Environment(seed=1, init_state=base_state())
    faulty = FaultInjector(
        FaultSpec(fault_class="partial_write", target="tool:charge_payment"),
        seed=1,
        trajectory_hint=5,
    ).wrap(env)
    llm = ScriptedLLM(TO_CHARGE, retry_on_error=False)
    asyncio.run(run_agent(_stub_task(), faulty, llm, get_config(config), Budget()))

    blob = str(llm.seen_messages).lower()
    for word in ("fault disclosure", "partial_write", "inject", "harness"):
        assert word not in blob, f"{config} leaked '{word}'"


def test_oracle_is_told_exactly_what_happened() -> None:
    env = Environment(seed=1, init_state=base_state())
    faulty = FaultInjector(
        FaultSpec(fault_class="partial_write", target="tool:charge_payment"),
        seed=1,
        trajectory_hint=5,
    ).wrap(env)
    llm = ScriptedLLM(TO_CHARGE, retry_on_error=False)
    asyncio.run(run_agent(_stub_task(), faulty, llm, get_config("oracle"), Budget()))
    blob = str(llm.seen_messages)
    assert "FAULT DISCLOSURE" in blob
    assert "DID execute" in blob


# ---------------------------------------------------------------------------
# Config table
# ---------------------------------------------------------------------------


def test_eight_configurations() -> None:
    assert len(CONFIGS) == 8
    assert set(HEADLINE) <= set(CONFIGS)
    assert set(DECOMPOSITION) <= set(CONFIGS)


def test_the_decomposition_is_a_clean_two_by_two() -> None:
    """Each arm toggles exactly one mechanism relative to its neighbours."""
    def flags(name: str) -> tuple[bool, bool]:
        config = get_config(name)
        return (
            config.call_decorator.name != "identity",
            config.recovery_policy.verify_read,
        )

    assert flags("contract") == (False, False)
    assert flags("contract+idem") == (True, False)
    assert flags("contract+verify") == (False, True)
    assert flags("guarded") == (True, True)


def test_every_config_shares_the_base_prompt() -> None:
    """A config that was better at the *task* would confound everything."""
    from chaosagent.agents.prompts import BASE

    for config in CONFIGS.values():
        assert config.system_prompt.startswith(BASE), config.name


def test_only_oracle_sees_faults() -> None:
    assert [c.name for c in CONFIGS.values() if c.sees_oracle] == ["oracle"]


def test_tool_schemas_cover_the_whole_surface() -> None:
    schemas = tool_schemas()
    assert len(schemas) == 14
    for schema in schemas:
        assert schema["description"]
        assert schema["input_schema"]["type"] == "object"


# ---------------------------------------------------------------------------
# Claim parsing — silent corruption depends on it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("All done.\nRESULT: SUCCESS", True),
        ("Could not reserve stock.\nRESULT: FAILURE", False),
        ("done\nresult: success", True),
        ("**RESULT: SUCCESS**", True),
        ("RESULT: SUCCESS\ntrailing note", True),
        ("no claim here", None),
        ("", None),
    ],
)
def test_parse_claim(text: str, expected: bool | None) -> None:
    assert parse_claim(text) == expected


def test_last_claim_wins() -> None:
    assert parse_claim("RESULT: FAILURE\nactually no\nRESULT: SUCCESS") is True
