"""Each invariant fires on a hand-built violating state, and stays quiet on a
clean one.

The negative cases matter as much as the positive ones: an invariant that fires
on healthy states would inflate the failure rate of every configuration equally
and quietly ruin the comparison.
"""

from __future__ import annotations

from chaosagent.env import Environment
from chaosagent.env.fixtures import base_state
from chaosagent.env.invariants import (
    INVARIANT_NAMES,
    check_all,
    new_violations,
    no_double_charge,
    order_total_consistent,
    refund_le_charge,
    reserved_le_onhand,
    shipped_implies_reserved,
)
from chaosagent.types import InvariantViolation, LineSeed, OrderSeed


def _names(violations: list[InvariantViolation]) -> set[str]:
    return {v.name for v in violations}


def test_clean_world_has_no_violations(env: Environment) -> None:
    assert check_all(env) == []


def test_completed_happy_path_has_no_violations(env: Environment) -> None:
    env.call("create_order", {"customer_id": "cus_1"})
    env.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-KEYB", "qty": 1})
    env.call("reserve_stock", {"order_id": "ord_9001"})
    env.call("charge_payment", {"order_id": "ord_9001", "amount_cents": 12_900})
    env.call("ship_order", {"order_id": "ord_9001"})
    assert check_all(env) == []


# ---------------------------------------------------------------------------
# reserved_le_onhand
# ---------------------------------------------------------------------------


def test_reserved_le_onhand_fires_when_reserved_exceeds_stock(env: Environment) -> None:
    env.x("UPDATE inventory SET onhand = 3, reserved = 5 WHERE sku = 'SKU-KEYB'")
    assert _names(reserved_le_onhand(env)) == {"reserved_le_onhand"}


def test_reserved_le_onhand_fires_on_negative_onhand(env: Environment) -> None:
    env.x("UPDATE inventory SET onhand = -1 WHERE sku = 'SKU-MOUS'")
    assert any("negative" in v.detail for v in reserved_le_onhand(env))


def test_double_reserve_under_tight_stock_breaks_the_invariant(
    tight_env: Environment,
) -> None:
    """Two reservations for a 2-unit order against 2 units on hand."""
    tight_env.call("create_order", {"customer_id": "cus_1"})
    tight_env.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-KEYB", "qty": 2})
    assert tight_env.call("reserve_stock", {"order_id": "ord_9001"}).ok
    # The second call is rejected for lack of availability, so the world stays
    # consistent — the environment's own precondition saves it here.
    second = tight_env.call("reserve_stock", {"order_id": "ord_9001"})
    assert not second.ok
    assert second.error is not None and second.error.code == "INSUFFICIENT_STOCK"
    assert check_all(tight_env) == []


# ---------------------------------------------------------------------------
# no_double_charge — the headline invariant
# ---------------------------------------------------------------------------


def test_no_double_charge_fires_on_repeat_charge(env: Environment) -> None:
    env.call("create_order", {"customer_id": "cus_1"})
    env.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-KEYB", "qty": 1})
    env.call("reserve_stock", {"order_id": "ord_9001"})
    assert env.call("charge_payment", {"order_id": "ord_9001", "amount_cents": 12_900}).ok
    # The gateway does not deduplicate. This is the behaviour under test.
    assert env.call("charge_payment", {"order_id": "ord_9001", "amount_cents": 12_900}).ok
    assert _names(no_double_charge(env)) == {"no_double_charge"}


def test_idempotency_key_prevents_the_double_charge(env: Environment) -> None:
    env.call("create_order", {"customer_id": "cus_1"})
    env.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-KEYB", "qty": 1})
    env.call("reserve_stock", {"order_id": "ord_9001"})
    args = {"order_id": "ord_9001", "amount_cents": 12_900, "idempotency_key": "k-1"}
    assert env.call("charge_payment", args).ok
    assert env.call("charge_payment", args).ok
    assert no_double_charge(env) == []
    assert len(env.state().payments) == 1


def test_refund_settles_the_charge_so_recharging_is_clean(env: Environment) -> None:
    env.call("create_order", {"customer_id": "cus_1"})
    env.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-KEYB", "qty": 1})
    env.call("reserve_stock", {"order_id": "ord_9001"})
    env.call("charge_payment", {"order_id": "ord_9001", "amount_cents": 12_900})
    env.call("refund_payment", {"order_id": "ord_9001", "amount_cents": 12_900})
    env.call("charge_payment", {"order_id": "ord_9001", "amount_cents": 12_900})
    assert no_double_charge(env) == []


def test_different_amounts_are_not_a_double_charge(env: Environment) -> None:
    env.call("create_order", {"customer_id": "cus_1"})
    env.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-KEYB", "qty": 1})
    env.call("reserve_stock", {"order_id": "ord_9001"})
    env.call("charge_payment", {"order_id": "ord_9001", "amount_cents": 12_900})
    env.call("charge_payment", {"order_id": "ord_9001", "amount_cents": 500})
    assert no_double_charge(env) == []


# ---------------------------------------------------------------------------
# order_total_consistent
# ---------------------------------------------------------------------------


def test_order_total_consistent_fires_on_tampered_total(env: Environment) -> None:
    env.call("create_order", {"customer_id": "cus_1"})
    env.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-KEYB", "qty": 1})
    env.x("UPDATE orders SET total_cents = 1 WHERE id = 'ord_9001'")
    assert _names(order_total_consistent(env)) == {"order_total_consistent"}


# ---------------------------------------------------------------------------
# shipped_implies_reserved
# ---------------------------------------------------------------------------


def test_double_ship_breaks_shipped_implies_reserved(paid_order_env: Environment) -> None:
    assert paid_order_env.call("ship_order", {"order_id": "ord_1"}).ok
    assert paid_order_env.call("ship_order", {"order_id": "ord_1"}).ok
    violations = check_all(paid_order_env)
    assert "shipped_implies_reserved" in _names(violations)


def test_double_ship_also_drives_stock_negative(paid_order_env: Environment) -> None:
    for _ in range(3):
        paid_order_env.call("ship_order", {"order_id": "ord_1"})
    inv = {r["sku"]: r["onhand"] for r in paid_order_env.state().inventory}
    assert inv["SKU-KEYB"] == 25 - 3


def test_shipped_order_holding_reservations_is_a_violation(
    paid_order_env: Environment,
) -> None:
    paid_order_env.x("UPDATE orders SET status = 'shipped' WHERE id = 'ord_1'")
    assert "shipped_implies_reserved" in _names(shipped_implies_reserved(paid_order_env))


# ---------------------------------------------------------------------------
# refund_le_charge
# ---------------------------------------------------------------------------


def test_refund_le_charge_fires_on_tampered_ledger(env: Environment) -> None:
    env.x(
        "INSERT INTO payments (id, order_id, kind, amount_cents, at, seq) "
        "VALUES ('p1', 'ord_x', 'refund', 500, 0, 1)"
    )
    assert _names(refund_le_charge(env)) == {"refund_le_charge"}


def test_over_refund_is_rejected_by_the_tool(env: Environment) -> None:
    env.call("create_order", {"customer_id": "cus_1"})
    env.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-CABL", "qty": 1})
    env.call("reserve_stock", {"order_id": "ord_9001"})
    env.call("charge_payment", {"order_id": "ord_9001", "amount_cents": 1_200})
    result = env.call("refund_payment", {"order_id": "ord_9001", "amount_cents": 9_999})
    assert not result.ok
    assert result.error is not None and result.error.code == "PRECONDITION"
    assert refund_le_charge(env) == []


# ---------------------------------------------------------------------------
# Baseline handling
# ---------------------------------------------------------------------------


def test_seeded_double_charge_is_a_baseline_not_a_run_failure() -> None:
    """The reconciliation task starts dirty on purpose."""
    state = base_state()
    state.orders = [
        OrderSeed(
            id="ord_1",
            customer_id="cus_1",
            status="paid",
            lines=[LineSeed(sku="SKU-CABL", qty=1)],
            extra_charges=[1_200],
        )
    ]
    env = Environment(seed=1, init_state=state)
    before = check_all(env)
    assert "no_double_charge" in _names(before)
    # Nothing the agent did, so nothing is attributed to the run.
    assert new_violations(before, check_all(env)) == []


def test_new_violations_counts_with_multiplicity() -> None:
    a = InvariantViolation(name="no_double_charge", detail="x")
    assert new_violations([a], [a, a]) == [a]
    assert new_violations([a], [a]) == []
    assert new_violations([], [a]) == [a]


def test_every_declared_invariant_has_an_implementation() -> None:
    from chaosagent.env.invariants import INVARIANTS

    assert set(INVARIANTS) == set(INVARIANT_NAMES)
    assert len(INVARIANT_NAMES) == 5
