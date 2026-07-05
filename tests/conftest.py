"""Shared fixtures."""

from __future__ import annotations

import pytest

from chaosagent.env import Environment
from chaosagent.env.fixtures import base_state
from chaosagent.types import InitState, LineSeed, OrderSeed


@pytest.fixture
def env() -> Environment:
    """An empty world with the full catalogue and 25 of everything."""
    return Environment(seed=1, init_state=base_state())


@pytest.fixture
def tight_env() -> Environment:
    """A world where one SKU has exactly enough stock for a single order."""
    return Environment(seed=1, init_state=base_state({"SKU-KEYB": 2}))


@pytest.fixture
def paid_order_env() -> Environment:
    """A world containing one paid, reserved order ready to ship."""
    state: InitState = base_state()
    state.orders = [
        OrderSeed(
            id="ord_1",
            customer_id="cus_1",
            status="paid",
            lines=[LineSeed(sku="SKU-KEYB", qty=1), LineSeed(sku="SKU-CABL", qty=2)],
        )
    ]
    return Environment(seed=1, init_state=state)


def drive(env: Environment, calls: list[tuple[str, dict]]) -> list:
    """Run a scripted action sequence, returning every result."""
    return [env.call(tool, args) for tool, args in calls]
