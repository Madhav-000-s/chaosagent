"""Tool surface: preconditions, error codes, idempotency-key semantics, and the
ground-truth call log.
"""

from __future__ import annotations

from chaosagent.env import Environment, registry
from chaosagent.env.registry import NON_IDEMPOTENT_WRITES

# ---------------------------------------------------------------------------
# The surface itself
# ---------------------------------------------------------------------------


def test_exactly_fourteen_tools() -> None:
    names = registry.tool_names()
    assert len(names) == 14
    assert len(set(names)) == 14


def test_read_write_split_is_six_and_eight() -> None:
    specs = registry.specs()
    assert sum(1 for s in specs if s.kind == "read") == 6
    assert sum(1 for s in specs if s.kind == "write") == 8


def test_expected_tool_names() -> None:
    assert set(registry.tool_names()) == {
        "get_order",
        "list_orders",
        "get_inventory",
        "get_customer",
        "get_payment_status",
        "search_products",
        "create_order",
        "add_line_item",
        "reserve_stock",
        "release_stock",
        "charge_payment",
        "refund_payment",
        "cancel_order",
        "ship_order",
    }


def test_reads_are_idempotent_and_take_no_key() -> None:
    for spec in registry.specs():
        if spec.kind == "read":
            assert spec.idempotent
            assert not spec.accepts_idempotency_key


def test_non_idempotent_writes_all_accept_a_key() -> None:
    """A write the agent cannot safely retry must offer a way to retry safely."""
    for spec in registry.specs():
        if spec.kind == "write" and not spec.idempotent:
            assert spec.accepts_idempotency_key, spec.name


def test_declared_non_idempotent_writes_match_the_registry() -> None:
    actual = {s.name for s in registry.specs() if s.kind == "write" and not s.idempotent}
    assert set(NON_IDEMPOTENT_WRITES) <= actual


def test_every_tool_exposes_a_json_schema() -> None:
    for spec in registry.specs():
        assert spec.parameters.get("type") == "object"
        assert "properties" in spec.parameters
        assert spec.description


def test_idempotency_key_is_never_a_required_argument() -> None:
    """Its presence must leak nothing about the configuration under test."""
    for spec in registry.specs():
        assert "idempotency_key" not in spec.parameters.get("required", [])


# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------


def test_unknown_tool_is_not_found(env: Environment) -> None:
    result = env.call("delete_everything", {})
    assert not result.ok
    assert result.error is not None and result.error.code == "NOT_FOUND"


def test_unknown_entity_is_not_found(env: Environment) -> None:
    result = env.call("get_order", {"order_id": "nope"})
    assert result.error is not None and result.error.code == "NOT_FOUND"


def test_missing_required_argument_is_invalid_args(env: Environment) -> None:
    result = env.call("get_order", {})
    assert result.error is not None and result.error.code == "INVALID_ARGS"


def test_wrong_argument_type_is_invalid_args(env: Environment) -> None:
    env.call("create_order", {"customer_id": "cus_1"})
    result = env.call(
        "add_line_item", {"order_id": "ord_9001", "sku": "SKU-KEYB", "qty": "many"}
    )
    assert result.error is not None and result.error.code == "INVALID_ARGS"


def test_extra_arguments_are_ignored(env: Environment) -> None:
    result = env.call("get_inventory", {"sku": "SKU-KEYB", "hallucinated": True})
    assert result.ok


def test_charge_before_reserve_is_a_precondition_failure(env: Environment) -> None:
    env.call("create_order", {"customer_id": "cus_1"})
    env.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-KEYB", "qty": 1})
    result = env.call("charge_payment", {"order_id": "ord_9001", "amount_cents": 12_900})
    assert result.error is not None and result.error.code == "PRECONDITION"
    assert not result.error.retryable
    assert not result.error.state_may_have_changed


def test_insufficient_stock_has_its_own_code(tight_env: Environment) -> None:
    tight_env.call("create_order", {"customer_id": "cus_1"})
    tight_env.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-KEYB", "qty": 5})
    result = tight_env.call("reserve_stock", {"order_id": "ord_9001"})
    assert result.error is not None and result.error.code == "INSUFFICIENT_STOCK"


def test_genuine_refusals_never_claim_state_changed(env: Environment) -> None:
    """Only transport faults set this flag; the world is honest about itself."""
    for result in (
        env.call("get_order", {"order_id": "nope"}),
        env.call("charge_payment", {"order_id": "nope", "amount_cents": 1}),
        env.call("get_order", {}),
    ):
        assert result.error is not None
        assert not result.error.state_may_have_changed


def test_rejected_call_leaves_the_world_untouched(tight_env: Environment) -> None:
    tight_env.call("create_order", {"customer_id": "cus_1"})
    tight_env.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-KEYB", "qty": 1})
    tight_env.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-MOUS", "qty": 99})
    before = tight_env.digest()
    assert not tight_env.call("reserve_stock", {"order_id": "ord_9001"}).ok
    assert tight_env.digest() == before


# ---------------------------------------------------------------------------
# Idempotency-key semantics
# ---------------------------------------------------------------------------


def test_replay_returns_the_original_payload(env: Environment) -> None:
    a = env.call("create_order", {"customer_id": "cus_1", "idempotency_key": "k"})
    b = env.call("create_order", {"customer_id": "cus_1", "idempotency_key": "k"})
    assert a.payload == b.payload
    assert len(env.state().orders) == 1


def test_replay_is_recorded_as_not_executed(env: Environment) -> None:
    env.call("create_order", {"customer_id": "cus_1", "idempotency_key": "k"})
    env.call("create_order", {"customer_id": "cus_1", "idempotency_key": "k"})
    log = env.call_log()
    assert log[0].executed and not log[0].idempotent_replay
    assert not log[1].executed and log[1].idempotent_replay


def test_different_keys_execute_separately(env: Environment) -> None:
    env.call("create_order", {"customer_id": "cus_1", "idempotency_key": "k1"})
    env.call("create_order", {"customer_id": "cus_1", "idempotency_key": "k2"})
    assert len(env.state().orders) == 2


def test_keys_are_scoped_per_tool(env: Environment) -> None:
    env.call("create_order", {"customer_id": "cus_1", "idempotency_key": "shared"})
    env.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-CABL", "qty": 1,
                               "idempotency_key": "shared"})
    assert len(env.state().line_items) == 1


def test_no_key_means_no_protection(env: Environment) -> None:
    env.call("create_order", {"customer_id": "cus_1"})
    env.call("create_order", {"customer_id": "cus_1"})
    assert len(env.state().orders) == 2


# ---------------------------------------------------------------------------
# The ground-truth call log
# ---------------------------------------------------------------------------


def test_call_log_records_failed_attempts(env: Environment) -> None:
    env.call("get_order", {"order_id": "nope"})
    log = env.call_log()
    assert len(log) == 1
    assert not log[0].executed and not log[0].ok
    assert log[0].error_code == "NOT_FOUND"


def test_call_log_counts_double_execution(env: Environment) -> None:
    env.call("create_order", {"customer_id": "cus_1"})
    env.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-KEYB", "qty": 1})
    env.call("reserve_stock", {"order_id": "ord_9001"})
    env.call("charge_payment", {"order_id": "ord_9001", "amount_cents": 12_900})
    env.call("charge_payment", {"order_id": "ord_9001", "amount_cents": 12_900})
    charges = [c for c in env.call_log() if c.tool == "charge_payment" and c.executed]
    assert len(charges) == 2


def test_clock_advances_once_per_call_including_failures(env: Environment) -> None:
    start = env.clock.now()
    env.call("get_order", {"order_id": "nope"})
    env.call("get_inventory", {})
    assert env.clock.now() == start + 2


# ---------------------------------------------------------------------------
# Lifecycle behaviour used by the task templates
# ---------------------------------------------------------------------------


def test_full_happy_path(env: Environment) -> None:
    env.call("create_order", {"customer_id": "cus_2"})
    env.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-MONI", "qty": 1})
    order = env.call("get_order", {"order_id": "ord_9001"}).payload
    assert order["total_cents"] == 31_900
    assert env.call("reserve_stock", {"order_id": "ord_9001"}).ok
    assert env.call("charge_payment", {"order_id": "ord_9001", "amount_cents": 31_900}).ok
    assert env.call("ship_order", {"order_id": "ord_9001"}).ok
    final = env.call("get_order", {"order_id": "ord_9001"}).payload
    assert final["status"] == "shipped"
    assert final["net_paid_cents"] == 31_900


def test_cancel_requires_refund_first(paid_order_env: Environment) -> None:
    blocked = paid_order_env.call("cancel_order", {"order_id": "ord_1"})
    assert blocked.error is not None and blocked.error.code == "PRECONDITION"
    assert "refund" in (blocked.error.hint or "").lower()

    total = paid_order_env.call("get_order", {"order_id": "ord_1"}).payload["total_cents"]
    assert paid_order_env.call(
        "refund_payment", {"order_id": "ord_1", "amount_cents": total}
    ).ok
    assert paid_order_env.call("cancel_order", {"order_id": "ord_1"}).ok
    assert paid_order_env.call("get_order", {"order_id": "ord_1"}).payload["status"] == "cancelled"


def test_cancel_releases_stock(paid_order_env: Environment) -> None:
    before = {r["sku"]: r["reserved"] for r in paid_order_env.state().inventory}
    assert before["SKU-KEYB"] == 1
    total = paid_order_env.call("get_order", {"order_id": "ord_1"}).payload["total_cents"]
    paid_order_env.call("refund_payment", {"order_id": "ord_1", "amount_cents": total})
    paid_order_env.call("cancel_order", {"order_id": "ord_1"})
    after = {r["sku"]: r["reserved"] for r in paid_order_env.state().inventory}
    assert after["SKU-KEYB"] == 0


def test_cancel_is_idempotent(paid_order_env: Environment) -> None:
    total = paid_order_env.call("get_order", {"order_id": "ord_1"}).payload["total_cents"]
    paid_order_env.call("refund_payment", {"order_id": "ord_1", "amount_cents": total})
    paid_order_env.call("cancel_order", {"order_id": "ord_1"})
    digest = paid_order_env.digest()
    assert paid_order_env.call("cancel_order", {"order_id": "ord_1"}).ok
    assert paid_order_env.digest() == digest


def test_shipped_order_cannot_be_cancelled(paid_order_env: Environment) -> None:
    paid_order_env.call("ship_order", {"order_id": "ord_1"})
    result = paid_order_env.call("cancel_order", {"order_id": "ord_1"})
    assert result.error is not None and result.error.code == "PRECONDITION"


def test_full_refund_returns_a_paid_order_to_reserved(paid_order_env: Environment) -> None:
    total = paid_order_env.call("get_order", {"order_id": "ord_1"}).payload["total_cents"]
    paid_order_env.call("refund_payment", {"order_id": "ord_1", "amount_cents": total})
    assert paid_order_env.call("get_order", {"order_id": "ord_1"}).payload["status"] == "reserved"


def test_release_then_modify_then_reserve(env: Environment) -> None:
    env.call("create_order", {"customer_id": "cus_1"})
    env.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-CABL", "qty": 1})
    env.call("reserve_stock", {"order_id": "ord_9001"})
    blocked = env.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-STND", "qty": 1})
    assert blocked.error is not None and blocked.error.code == "PRECONDITION"
    assert env.call("release_stock", {"order_id": "ord_9001"}).ok
    assert env.call("add_line_item", {"order_id": "ord_9001", "sku": "SKU-STND", "qty": 1}).ok
    assert env.call("reserve_stock", {"order_id": "ord_9001"}).ok
    assert env.call("get_order", {"order_id": "ord_9001"}).payload["total_cents"] == 6_300


def test_payment_status_ledger_is_ordered(paid_order_env: Environment) -> None:
    paid_order_env.call("refund_payment", {"order_id": "ord_1", "amount_cents": 100})
    status = paid_order_env.call("get_payment_status", {"order_id": "ord_1"}).payload
    assert [e["kind"] for e in status["ledger"]] == ["charge", "refund"]
    assert status["charge_count"] == 1 and status["refund_count"] == 1
    assert status["net_paid_cents"] == status["amount_charged_cents"] - 100


def test_search_products_filters(env: Environment) -> None:
    assert len(env.call("search_products", {"category": "peripherals"}).payload) == 3
    hits = env.call("search_products", {"query": "cable"}).payload
    assert [h["sku"] for h in hits] == ["SKU-CABL"]


def test_list_orders_filters_by_status(paid_order_env: Environment) -> None:
    assert len(paid_order_env.call("list_orders", {"status": "paid"}).payload) == 1
    assert paid_order_env.call("list_orders", {"status": "draft"}).payload == []
