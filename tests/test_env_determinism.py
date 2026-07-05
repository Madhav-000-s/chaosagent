"""The environment is a pure function of (seed, action sequence).

If this file ever goes red, every number the project reports is unreliable,
because runs stop being comparable across configurations.
"""

from __future__ import annotations

from chaosagent.env import Environment
from chaosagent.env.fixtures import base_state
from chaosagent.env.schema import ALL_TABLES, SORT_KEYS, STATE_TABLES

SCRIPT: list[tuple[str, dict]] = [
    ("create_order", {"customer_id": "cus_1"}),
    ("add_line_item", {"order_id": "ord_9001", "sku": "SKU-KEYB", "qty": 2}),
    ("add_line_item", {"order_id": "ord_9001", "sku": "SKU-CABL", "qty": 3}),
    ("reserve_stock", {"order_id": "ord_9001"}),
    ("get_order", {"order_id": "ord_9001"}),
    ("charge_payment", {"order_id": "ord_9001", "amount_cents": 29_400}),
    ("ship_order", {"order_id": "ord_9001"}),
    ("get_inventory", {}),
]


def _run() -> Environment:
    env = Environment(seed=7, init_state=base_state())
    for tool, args in SCRIPT:
        env.call(tool, args)
    return env


def test_same_seed_same_digest_100x() -> None:
    reference = _run().digest()
    for _ in range(100):
        assert _run().digest() == reference


def test_call_log_is_identical_across_runs() -> None:
    a = [c.model_dump() for c in _run().call_log()]
    b = [c.model_dump() for c in _run().call_log()]
    assert a == b


def test_no_wall_clock_dependence() -> None:
    """Timestamps come from the virtual clock, so they are reproducible."""
    a = _run()
    b = _run()
    assert a.clock.now() == b.clock.now()
    assert [p["at"] for p in a.state().payments] == [p["at"] for p in b.state().payments]
    # One tick per call, starting from the fixed epoch.
    from chaosagent.env.clock import EPOCH

    assert a.clock.now() == EPOCH + len(SCRIPT)


def test_snapshot_restore_round_trips() -> None:
    env = Environment(seed=7, init_state=base_state())
    for tool, args in SCRIPT[:4]:
        env.call(tool, args)

    snap = env.snapshot()
    mid_digest = env.digest()
    mid_clock = env.clock.now()

    for tool, args in SCRIPT[4:]:
        env.call(tool, args)
    assert env.digest() != mid_digest

    env.restore(snap)
    assert env.digest() == mid_digest
    assert env.clock.now() == mid_clock
    # The call log is part of the snapshot: ground truth must roll back too.
    assert len(env.call_log()) == 4


def test_restore_then_replay_reaches_the_same_state() -> None:
    env = Environment(seed=7, init_state=base_state())
    snap = env.snapshot()
    for tool, args in SCRIPT:
        env.call(tool, args)
    first = env.digest()

    env.restore(snap)
    for tool, args in SCRIPT:
        env.call(tool, args)
    assert env.digest() == first


def test_state_dump_covers_every_state_table() -> None:
    state = _run().state()
    assert set(state.model_dump()) == set(STATE_TABLES)


def test_every_table_has_a_declared_sort_key() -> None:
    """Ordering is what makes digests and snapshots comparable."""
    assert set(SORT_KEYS) == set(ALL_TABLES)


def test_snapshot_bytes_are_stable() -> None:
    assert _run().snapshot() == _run().snapshot()


def test_documented_ddl_has_not_drifted() -> None:
    """`docs/env_schema.sql` is generated. Regenerate with `chaosagent schema`."""
    from pathlib import Path

    from chaosagent.env.schema import DDL

    doc = Path(__file__).resolve().parents[1] / "docs" / "env_schema.sql"
    assert doc.exists(), "run: chaosagent schema --out docs/env_schema.sql"
    assert doc.read_text(encoding="utf-8").strip() == DDL.strip()
