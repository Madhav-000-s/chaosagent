"""State invariants.

Five pure predicates over world state. They exist because the terminal-state
assertion is necessary but not sufficient: an agent can land on the right final
order status having double-charged the customer along the way, and only an
invariant catches that.

Two of these — ``refund_le_charge`` and ``order_total_consistent`` — are also
enforced by the tools themselves. That is deliberate: they act as regression
detectors for the environment, so a future change that loosens a precondition
fails a test instead of silently corrupting the results. The three that fire in
practice under injected faults are ``no_double_charge``, ``reserved_le_onhand``
and ``shipped_implies_reserved``.

Note on baselines: some task fixtures *start* in a violating state on purpose
(the reconciliation task seeds a duplicate charge). The scorer therefore reports
violations newly introduced during a run, not absolute counts.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from chaosagent.types import InvariantViolation

if TYPE_CHECKING:  # pragma: no cover
    from chaosagent.env.world import Environment

#: Canonical names, in report order.
INVARIANT_NAMES: tuple[str, ...] = (
    "reserved_le_onhand",
    "no_double_charge",
    "order_total_consistent",
    "shipped_implies_reserved",
    "refund_le_charge",
)


def reserved_le_onhand(env: Environment) -> list[InvariantViolation]:
    """Reserved stock never exceeds stock on hand, and neither goes negative.

    A second ``reserve_stock`` on the same order double-counts the reservation;
    a second ``ship_order`` decrements on-hand twice. Both land here.
    """
    out: list[InvariantViolation] = []
    for row in env.q("SELECT sku, onhand, reserved FROM inventory ORDER BY sku"):
        sku, onhand, reserved = row["sku"], int(row["onhand"]), int(row["reserved"])
        if onhand < 0:
            out.append(
                InvariantViolation(
                    name="reserved_le_onhand",
                    detail=f"sku {sku}: on-hand is negative ({onhand}) — stock was consumed twice",
                )
            )
        if reserved < 0:
            out.append(
                InvariantViolation(
                    name="reserved_le_onhand",
                    detail=f"sku {sku}: reserved is negative ({reserved})",
                )
            )
        if reserved > onhand:
            out.append(
                InvariantViolation(
                    name="reserved_le_onhand",
                    detail=f"sku {sku}: reserved {reserved} exceeds on-hand {onhand}",
                )
            )
    return out


def no_double_charge(env: Environment) -> list[InvariantViolation]:
    """No (order, amount) is charged twice without an intervening refund.

    Walks the append-only payment ledger in sequence order and tracks how many
    charges of each amount are currently outstanding for each order. A refund of
    the same amount settles one. This is the invariant the `partial_write` and
    `timeout` faults are designed to break.
    """
    live: dict[tuple[str, int], int] = defaultdict(int)
    out: list[InvariantViolation] = []
    rows = env.q("SELECT order_id, kind, amount_cents FROM payments ORDER BY seq")
    for row in rows:
        key = (row["order_id"], int(row["amount_cents"]))
        if row["kind"] == "charge":
            live[key] += 1
            if live[key] > 1:
                out.append(
                    InvariantViolation(
                        name="no_double_charge",
                        detail=(
                            f"order {key[0]}: {live[key]} outstanding charges of "
                            f"{key[1]} cents with no intervening refund"
                        ),
                    )
                )
        elif row["kind"] == "refund" and live[key] > 0:
            live[key] -= 1
    return out


def order_total_consistent(env: Environment) -> list[InvariantViolation]:
    """``order.total_cents`` equals the sum of its line-item subtotals."""
    out: list[InvariantViolation] = []
    sums: dict[str, int] = defaultdict(int)
    for row in env.q("SELECT order_id, subtotal_cents FROM line_items"):
        sums[row["order_id"]] += int(row["subtotal_cents"])
    for row in env.q("SELECT id, total_cents FROM orders ORDER BY id"):
        expected = sums.get(row["id"], 0)
        if int(row["total_cents"]) != expected:
            out.append(
                InvariantViolation(
                    name="order_total_consistent",
                    detail=(
                        f"order {row['id']}: total {row['total_cents']} != "
                        f"sum of line items {expected}"
                    ),
                )
            )
    return out


def shipped_implies_reserved(env: Environment) -> list[InvariantViolation]:
    """Every shipment consumed stock that was actually reserved for it.

    ``ship_order`` records this judgement at ship time. A duplicate shipment
    finds the reservations already consumed and records ``0``.
    """
    out: list[InvariantViolation] = []
    for row in env.q(
        "SELECT id, order_id, stock_was_reserved FROM shipments ORDER BY id"
    ):
        if not int(row["stock_was_reserved"]):
            out.append(
                InvariantViolation(
                    name="shipped_implies_reserved",
                    detail=(
                        f"shipment {row['id']} for order {row['order_id']} shipped "
                        "without covering reservations"
                    ),
                )
            )
    # A shipped order that still holds reservations means stock was never drawn
    # down — the mirror-image corruption.
    for row in env.q(
        "SELECT o.id AS oid, COUNT(r.sku) AS n FROM orders o "
        "LEFT JOIN reservations r ON r.order_id = o.id "
        "WHERE o.status = 'shipped' GROUP BY o.id ORDER BY o.id"
    ):
        if int(row["n"]) > 0:
            out.append(
                InvariantViolation(
                    name="shipped_implies_reserved",
                    detail=f"order {row['oid']} is shipped but still holds {row['n']} reservation(s)",
                )
            )
    return out


def refund_le_charge(env: Environment) -> list[InvariantViolation]:
    """Total refunded never exceeds total charged, per order."""
    charged: dict[str, int] = defaultdict(int)
    refunded: dict[str, int] = defaultdict(int)
    for row in env.q("SELECT order_id, kind, amount_cents FROM payments"):
        if row["kind"] == "charge":
            charged[row["order_id"]] += int(row["amount_cents"])
        else:
            refunded[row["order_id"]] += int(row["amount_cents"])
    out: list[InvariantViolation] = []
    for order_id in sorted(set(charged) | set(refunded)):
        if refunded[order_id] > charged[order_id]:
            out.append(
                InvariantViolation(
                    name="refund_le_charge",
                    detail=(
                        f"order {order_id}: refunded {refunded[order_id]} exceeds "
                        f"charged {charged[order_id]}"
                    ),
                )
            )
    return out


#: name -> predicate, in report order.
INVARIANTS = {
    "reserved_le_onhand": reserved_le_onhand,
    "no_double_charge": no_double_charge,
    "order_total_consistent": order_total_consistent,
    "shipped_implies_reserved": shipped_implies_reserved,
    "refund_le_charge": refund_le_charge,
}


def check_all(env: Environment) -> list[InvariantViolation]:
    """Run every invariant. Returns all violations, in canonical order."""
    out: list[InvariantViolation] = []
    for name in INVARIANT_NAMES:
        out.extend(INVARIANTS[name](env))
    return out


def new_violations(
    before: list[InvariantViolation], after: list[InvariantViolation]
) -> list[InvariantViolation]:
    """Violations introduced during a run, ignoring those seeded at init.

    Compared by ``(name, detail)`` with multiplicity, so a second duplicate
    charge on an order that already had one still counts.
    """
    baseline: dict[tuple[str, str], int] = defaultdict(int)
    for v in before:
        baseline[(v.name, v.detail)] += 1
    out: list[InvariantViolation] = []
    for v in after:
        key = (v.name, v.detail)
        if baseline[key] > 0:
            baseline[key] -= 1
        else:
            out.append(v)
    return out


__all__ = [
    "INVARIANTS",
    "INVARIANT_NAMES",
    "check_all",
    "new_violations",
    "no_double_charge",
    "order_total_consistent",
    "refund_le_charge",
    "reserved_le_onhand",
    "shipped_implies_reserved",
]
