"""Declarative terminal-state assertions.

The correct terminal state of every task is known by construction, so scoring is
a state comparison rather than a judgement. But comparing whole world states is
too strict: an agent that creates an order after an extra read gets a different
generated id, and that difference is not a failure. So assertions are a list of
*semantic* checks — "customer cus_1 has exactly one order, it is paid, its net
paid equals its total, and it was charged once".

Each check returns a list of human-readable failure strings. Empty means pass.
Those strings end up in the trace, which is what makes a failed run diagnosable
without re-running it.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from chaosagent.types import WorldState


class StateView:
    """Read-model over a :class:`WorldState`, mirroring the tools' vocabulary."""

    def __init__(self, state: WorldState) -> None:
        self.state = state
        self.orders: dict[str, dict[str, Any]] = {o["id"]: o for o in state.orders}
        self.lines_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for li in state.line_items:
            self.lines_by_order[li["order_id"]].append(li)
        self.inventory: dict[str, dict[str, Any]] = {i["sku"]: i for i in state.inventory}

        self._charged: dict[str, int] = defaultdict(int)
        self._refunded: dict[str, int] = defaultdict(int)
        self._charge_n: dict[str, int] = defaultdict(int)
        self._refund_n: dict[str, int] = defaultdict(int)
        for p in state.payments:
            if p["kind"] == "charge":
                self._charged[p["order_id"]] += int(p["amount_cents"])
                self._charge_n[p["order_id"]] += 1
            else:
                self._refunded[p["order_id"]] += int(p["amount_cents"])
                self._refund_n[p["order_id"]] += 1

    def charged(self, order_id: str) -> int:
        return self._charged[order_id]

    def refunded(self, order_id: str) -> int:
        return self._refunded[order_id]

    def net_paid(self, order_id: str) -> int:
        return self._charged[order_id] - self._refunded[order_id]

    def charge_count(self, order_id: str) -> int:
        return self._charge_n[order_id]

    def refund_count(self, order_id: str) -> int:
        return self._refund_n[order_id]

    def shipment_count(self, order_id: str) -> int:
        return sum(1 for s in self.state.shipments if s["order_id"] == order_id)

    def qty_of(self, order_id: str, sku: str) -> int:
        return sum(
            int(li["qty"]) for li in self.lines_by_order[order_id] if li["sku"] == sku
        )

    def find_orders(
        self,
        order_id: str | None = None,
        customer_id: str | None = None,
        contains_sku: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        out = list(self.orders.values())
        if order_id is not None:
            out = [o for o in out if o["id"] == order_id]
        if customer_id is not None:
            out = [o for o in out if o["customer_id"] == customer_id]
        if status is not None:
            out = [o for o in out if o["status"] == status]
        if contains_sku is not None:
            out = [o for o in out if self.qty_of(o["id"], contains_sku) > 0]
        return sorted(out, key=lambda o: o["id"])


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


class _Check(BaseModel):
    model_config = ConfigDict(frozen=True)


class OrderCheck(_Check):
    """Assert the state of exactly one order, selected by id or by owner."""

    kind: Literal["order"] = "order"
    order_id: str | None = None
    customer_id: str | None = None
    contains_sku: str | None = None
    label: str = "order"

    status: str | None = None
    total_cents: int | None = None
    net_paid_cents: int | None = None
    charged_cents: int | None = None
    refunded_cents: int | None = None
    charge_count: int | None = None
    refund_count: int | None = None
    shipment_count: int | None = None
    #: sku -> expected quantity on the order.
    line_qtys: dict[str, int] | None = None

    def evaluate(self, view: StateView) -> list[str]:
        matches = view.find_orders(
            order_id=self.order_id,
            customer_id=self.customer_id,
            contains_sku=self.contains_sku,
        )
        if len(matches) != 1:
            return [
                f"{self.label}: expected exactly 1 matching order, found {len(matches)}"
            ]
        order = matches[0]
        oid = order["id"]
        fails: list[str] = []

        def cmp(name: str, actual: Any, expected: Any) -> None:
            if expected is not None and actual != expected:
                fails.append(f"{self.label}[{oid}].{name}: expected {expected}, got {actual}")

        cmp("status", order["status"], self.status)
        cmp("total_cents", int(order["total_cents"]), self.total_cents)
        cmp("net_paid_cents", view.net_paid(oid), self.net_paid_cents)
        cmp("charged_cents", view.charged(oid), self.charged_cents)
        cmp("refunded_cents", view.refunded(oid), self.refunded_cents)
        cmp("charge_count", view.charge_count(oid), self.charge_count)
        cmp("refund_count", view.refund_count(oid), self.refund_count)
        cmp("shipment_count", view.shipment_count(oid), self.shipment_count)
        if self.line_qtys is not None:
            for sku, qty in sorted(self.line_qtys.items()):
                cmp(f"qty[{sku}]", view.qty_of(oid, sku), qty)
        return fails


class OrderCountCheck(_Check):
    """Assert how many orders match a filter. Guards against stray creations."""

    kind: Literal["order_count"] = "order_count"
    customer_id: str | None = None
    status: str | None = None
    count: int = 0

    def evaluate(self, view: StateView) -> list[str]:
        n = len(view.find_orders(customer_id=self.customer_id, status=self.status))
        if n != self.count:
            what = f"customer={self.customer_id or '*'} status={self.status or '*'}"
            return [f"order_count({what}): expected {self.count}, got {n}"]
        return []


class InventoryCheck(_Check):
    """Assert stock levels for one SKU."""

    kind: Literal["inventory"] = "inventory"
    sku: str
    onhand: int | None = None
    reserved: int | None = None
    available: int | None = None

    def evaluate(self, view: StateView) -> list[str]:
        row = view.inventory.get(self.sku)
        if row is None:
            return [f"inventory[{self.sku}]: sku missing from inventory"]
        onhand, reserved = int(row["onhand"]), int(row["reserved"])
        fails = []
        if self.onhand is not None and onhand != self.onhand:
            fails.append(f"inventory[{self.sku}].onhand: expected {self.onhand}, got {onhand}")
        if self.reserved is not None and reserved != self.reserved:
            fails.append(
                f"inventory[{self.sku}].reserved: expected {self.reserved}, got {reserved}"
            )
        if self.available is not None and (onhand - reserved) != self.available:
            fails.append(
                f"inventory[{self.sku}].available: expected {self.available}, "
                f"got {onhand - reserved}"
            )
        return fails


Check = Annotated[
    OrderCheck | OrderCountCheck | InventoryCheck, Field(discriminator="kind")
]


class StateAssertion(BaseModel):
    """The complete correctness condition for a task."""

    model_config = ConfigDict(frozen=True)

    checks: list[Check] = Field(default_factory=list)

    def failures(self, state: WorldState) -> list[str]:
        view = StateView(state)
        out: list[str] = []
        for check in self.checks:
            out.extend(check.evaluate(view))
        return out

    def holds(self, state: WorldState) -> bool:
        return not self.failures(state)


__all__ = [
    "Check",
    "InventoryCheck",
    "OrderCheck",
    "OrderCountCheck",
    "StateAssertion",
    "StateView",
]
