"""The eight write tools.

Read the module docstring in ``env/world.py`` before changing anything here.
The short version: these tools do **not** protect themselves against being
called twice. ``charge_payment`` on an already-paid order charges it again,
``ship_order`` on a shipped order ships it again. That is not an oversight — it
is the behaviour of every real payment gateway and fulfilment API that has ever
shipped, and it is the only reason a double-execution is observable. The
mechanism that prevents repetition is the idempotency key, and whether the agent
uses one is the independent variable in the guard-decomposition ablation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import Field

from chaosagent.env.errors import insufficient_stock, invalid_args, precondition
from chaosagent.env.registry import tool
from chaosagent.env.tools.common import (
    IdempotentArgs,
    order_view,
    payment_totals,
    recompute_total,
    require_customer,
    require_order,
    require_product,
)

if TYPE_CHECKING:  # pragma: no cover
    from chaosagent.env.world import Environment


# ---------------------------------------------------------------------------
# Order construction
# ---------------------------------------------------------------------------


class CreateOrderArgs(IdempotentArgs):
    customer_id: str = Field(description="Customer the order belongs to.")


@tool(
    name="create_order",
    kind="write",
    idempotent=False,
    accepts_idempotency_key=True,
    preconditions=["customer exists"],
    invariants_touched=["order_total_consistent"],
    args_model=CreateOrderArgs,
    description="Create a new empty order in status 'draft' for a customer.",
)
def create_order(env: Environment, args: CreateOrderArgs) -> dict[str, Any]:
    require_customer(env, args.customer_id)
    order_id = env.next_id("order", "ord_", start=9000)
    env.x(
        "INSERT INTO orders (id, customer_id, status, total_cents, created_at) "
        "VALUES (?, ?, 'draft', 0, ?)",
        (order_id, args.customer_id, env.clock.now()),
    )
    return order_view(env, order_id)


class AddLineItemArgs(IdempotentArgs):
    order_id: str = Field(description="Order to add the line to. Must be in 'draft'.")
    sku: str = Field(description="Product SKU.")
    qty: int = Field(description="Quantity, must be positive.")


@tool(
    name="add_line_item",
    kind="write",
    idempotent=False,
    accepts_idempotency_key=True,
    preconditions=["order.status == 'draft'", "product exists", "qty > 0"],
    invariants_touched=["order_total_consistent"],
    args_model=AddLineItemArgs,
    description=(
        "Add a line to a draft order at the product's catalogue price. "
        "Recomputes the order total."
    ),
)
def add_line_item(env: Environment, args: AddLineItemArgs) -> dict[str, Any]:
    order = require_order(env, args.order_id)
    if order["status"] != "draft":
        raise precondition(
            f"Order {args.order_id} is '{order['status']}', not 'draft'; lines can only "
            "be added to a draft order.",
            hint="Release the stock reservation first to return the order to 'draft'.",
        )
    if args.qty <= 0:
        raise invalid_args(f"qty must be positive, got {args.qty}.")
    product = require_product(env, args.sku)

    unit = int(product["price_cents"])
    line_id = env.next_id("line_item", "li_", start=5000)
    env.x(
        "INSERT INTO line_items (id, order_id, sku, qty, unit_price_cents, subtotal_cents) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (line_id, args.order_id, args.sku, args.qty, unit, unit * args.qty),
    )
    recompute_total(env, args.order_id)
    return order_view(env, args.order_id)


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------


class ReserveStockArgs(IdempotentArgs):
    order_id: str = Field(description="Order whose lines should be reserved.")


@tool(
    name="reserve_stock",
    kind="write",
    idempotent=False,
    accepts_idempotency_key=True,
    preconditions=[
        "order.status in ('draft', 'reserved')",
        "order has at least one line",
        "available(sku) >= qty for every line",
    ],
    invariants_touched=["reserved_le_onhand"],
    args_model=ReserveStockArgs,
    description=(
        "Reserve stock for every line on the order and move it to 'reserved'. "
        "Fails if any line exceeds available stock."
    ),
)
def reserve_stock(env: Environment, args: ReserveStockArgs) -> dict[str, Any]:
    order = require_order(env, args.order_id)
    if order["status"] not in ("draft", "reserved"):
        raise precondition(
            f"Order {args.order_id} is '{order['status']}'; stock can only be reserved "
            "for a draft or already-reserved order."
        )
    lines = env.q(
        "SELECT sku, qty FROM line_items WHERE order_id = ? ORDER BY id", (args.order_id,)
    )
    if not lines:
        raise precondition(f"Order {args.order_id} has no line items to reserve.")

    # Validate every line before mutating anything, so a rejection leaves the
    # world untouched.
    for line in lines:
        inv = env.q1("SELECT onhand, reserved FROM inventory WHERE sku = ?", (line["sku"],))
        avail = (int(inv["onhand"]) - int(inv["reserved"])) if inv else 0
        if avail < int(line["qty"]):
            raise insufficient_stock(line["sku"], int(line["qty"]), avail)

    now = env.clock.now()
    for line in lines:
        sku, qty = line["sku"], int(line["qty"])
        existing = env.q1(
            "SELECT qty FROM reservations WHERE order_id = ? AND sku = ?",
            (args.order_id, sku),
        )
        if existing is None:
            env.x(
                "INSERT INTO reservations (order_id, sku, qty, at) VALUES (?, ?, ?, ?)",
                (args.order_id, sku, qty, now),
            )
        else:
            # Repeat call: the reservation accumulates. This is the double
            # execution, and under tight stock it breaks reserved_le_onhand.
            env.x(
                "UPDATE reservations SET qty = qty + ?, at = ? WHERE order_id = ? AND sku = ?",
                (qty, now, args.order_id, sku),
            )
        env.x("UPDATE inventory SET reserved = reserved + ? WHERE sku = ?", (qty, sku))

    env.x("UPDATE orders SET status = 'reserved' WHERE id = ?", (args.order_id,))
    return order_view(env, args.order_id)


class ReleaseStockArgs(IdempotentArgs):
    order_id: str = Field(description="Order whose reservations should be released.")


@tool(
    name="release_stock",
    kind="write",
    idempotent=False,
    accepts_idempotency_key=True,
    preconditions=["order.status == 'reserved'"],
    invariants_touched=["reserved_le_onhand"],
    args_model=ReleaseStockArgs,
    description=(
        "Release all stock reservations for an order and return it to 'draft'."
    ),
)
def release_stock(env: Environment, args: ReleaseStockArgs) -> dict[str, Any]:
    order = require_order(env, args.order_id)
    if order["status"] != "reserved":
        raise precondition(
            f"Order {args.order_id} is '{order['status']}'; only a reserved order holds "
            "stock to release."
        )
    _release_reservations(env, args.order_id)
    env.x("UPDATE orders SET status = 'draft' WHERE id = ?", (args.order_id,))
    return order_view(env, args.order_id)


def _release_reservations(env: Environment, order_id: str) -> None:
    for r in env.q(
        "SELECT sku, qty FROM reservations WHERE order_id = ? ORDER BY sku", (order_id,)
    ):
        env.x(
            "UPDATE inventory SET reserved = reserved - ? WHERE sku = ?",
            (int(r["qty"]), r["sku"]),
        )
    env.x("DELETE FROM reservations WHERE order_id = ?", (order_id,))


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------


class ChargePaymentArgs(IdempotentArgs):
    order_id: str = Field(description="Order to charge.")
    amount_cents: int = Field(
        description=(
            "Amount in INTEGER CENTS. 12.00 dollars is 1200. Must be positive."
        )
    )


@tool(
    name="charge_payment",
    kind="write",
    idempotent=False,
    accepts_idempotency_key=True,
    preconditions=["order.status in ('reserved', 'paid')", "amount_cents > 0"],
    invariants_touched=["no_double_charge", "refund_le_charge"],
    args_model=ChargePaymentArgs,
    description=(
        "Charge the customer for an order and move it to 'paid'. This gateway "
        "does NOT deduplicate on its own: calling it twice charges twice unless "
        "the same idempotency_key is supplied."
    ),
)
def charge_payment(env: Environment, args: ChargePaymentArgs) -> dict[str, Any]:
    order = require_order(env, args.order_id)
    if order["status"] not in ("reserved", "paid"):
        raise precondition(
            f"Order {args.order_id} is '{order['status']}'; charge requires a reserved "
            "or already-paid order.",
            hint="Reserve stock before charging.",
        )
    if args.amount_cents <= 0:
        raise invalid_args(f"amount_cents must be positive, got {args.amount_cents}.")

    payment_id = env.record_payment(args.order_id, "charge", args.amount_cents)
    env.x("UPDATE orders SET status = 'paid' WHERE id = ?", (args.order_id,))
    charged, refunded, net = payment_totals(env, args.order_id)
    return {
        "payment_id": payment_id,
        "order_id": args.order_id,
        "amount_cents": args.amount_cents,
        "status": "captured",
        "order_status": "paid",
        "amount_charged_cents": charged,
        "net_paid_cents": net,
    }


class RefundPaymentArgs(IdempotentArgs):
    order_id: str = Field(description="Order to refund.")
    amount_cents: int = Field(
        description="Amount in INTEGER CENTS. Cannot exceed the net amount paid."
    )


@tool(
    name="refund_payment",
    kind="write",
    idempotent=False,
    accepts_idempotency_key=True,
    preconditions=["net_paid_cents >= amount_cents", "amount_cents > 0"],
    invariants_touched=["refund_le_charge", "no_double_charge"],
    args_model=RefundPaymentArgs,
    description=(
        "Refund part or all of what an order has been charged. Fully refunding a "
        "paid order returns it to 'reserved' so it can then be cancelled."
    ),
)
def refund_payment(env: Environment, args: RefundPaymentArgs) -> dict[str, Any]:
    order = require_order(env, args.order_id)
    if args.amount_cents <= 0:
        raise invalid_args(f"amount_cents must be positive, got {args.amount_cents}.")
    _, _, net = payment_totals(env, args.order_id)
    if net < args.amount_cents:
        raise precondition(
            f"Order {args.order_id} has only {net} cents outstanding; cannot refund "
            f"{args.amount_cents}.",
            hint="Read get_payment_status to see the current net paid amount.",
        )

    payment_id = env.record_payment(args.order_id, "refund", args.amount_cents)
    charged, refunded, new_net = payment_totals(env, args.order_id)
    if new_net == 0 and order["status"] == "paid":
        env.x("UPDATE orders SET status = 'reserved' WHERE id = ?", (args.order_id,))
    return {
        "payment_id": payment_id,
        "order_id": args.order_id,
        "amount_cents": args.amount_cents,
        "status": "refunded",
        "amount_refunded_cents": refunded,
        "net_paid_cents": new_net,
    }


# ---------------------------------------------------------------------------
# Terminal transitions
# ---------------------------------------------------------------------------


class CancelOrderArgs(IdempotentArgs):
    order_id: str = Field(description="Order to cancel.")


@tool(
    name="cancel_order",
    kind="write",
    idempotent=True,
    accepts_idempotency_key=True,
    preconditions=["order.status != 'shipped'", "net_paid_cents == 0"],
    invariants_touched=["reserved_le_onhand"],
    args_model=CancelOrderArgs,
    description=(
        "Cancel an order, releasing any stock it holds. Refuses while money is "
        "still outstanding: refund first, then cancel. Cancelling an already "
        "cancelled order is a no-op."
    ),
)
def cancel_order(env: Environment, args: CancelOrderArgs) -> dict[str, Any]:
    order = require_order(env, args.order_id)
    if order["status"] == "cancelled":
        return order_view(env, args.order_id)
    if order["status"] == "shipped":
        raise precondition(
            f"Order {args.order_id} has already shipped and cannot be cancelled."
        )
    _, _, net = payment_totals(env, args.order_id)
    if net != 0:
        raise precondition(
            f"Order {args.order_id} still has {net} cents charged against it.",
            hint="Refund the outstanding amount with refund_payment before cancelling.",
        )
    _release_reservations(env, args.order_id)
    env.x("UPDATE orders SET status = 'cancelled' WHERE id = ?", (args.order_id,))
    return order_view(env, args.order_id)


class ShipOrderArgs(IdempotentArgs):
    order_id: str = Field(description="Order to ship.")


@tool(
    name="ship_order",
    kind="write",
    idempotent=False,
    accepts_idempotency_key=True,
    preconditions=["order.status in ('paid', 'shipped')"],
    invariants_touched=["shipped_implies_reserved", "reserved_le_onhand"],
    args_model=ShipOrderArgs,
    description=(
        "Ship a paid order: consumes the reserved stock, draws down on-hand "
        "inventory, and records a shipment. Not deduplicated — shipping twice "
        "creates two shipments and draws stock down twice."
    ),
)
def ship_order(env: Environment, args: ShipOrderArgs) -> dict[str, Any]:
    order = require_order(env, args.order_id)
    if order["status"] not in ("paid", "shipped"):
        raise precondition(
            f"Order {args.order_id} is '{order['status']}'; only a paid order can ship.",
            hint="Reserve stock and charge the order before shipping.",
        )
    lines = env.q(
        "SELECT sku, qty FROM line_items WHERE order_id = ? ORDER BY id", (args.order_id,)
    )
    if not lines:
        raise precondition(f"Order {args.order_id} has no line items to ship.")

    # Judge coverage *before* consuming, and record the judgement. A duplicate
    # shipment finds the reservations already gone and records False, which is
    # how shipped_implies_reserved catches it after the fact.
    covered = True
    for line in lines:
        res = env.q1(
            "SELECT qty FROM reservations WHERE order_id = ? AND sku = ?",
            (args.order_id, line["sku"]),
        )
        if res is None or int(res["qty"]) < int(line["qty"]):
            covered = False
            break

    for line in lines:
        sku, qty = line["sku"], int(line["qty"])
        res = env.q1(
            "SELECT qty FROM reservations WHERE order_id = ? AND sku = ?",
            (args.order_id, sku),
        )
        held = int(res["qty"]) if res else 0
        take = min(held, qty)
        if take:
            env.x("UPDATE inventory SET reserved = reserved - ? WHERE sku = ?", (take, sku))
        # On-hand is drawn down regardless: the goods physically left. A second
        # shipment therefore drives on-hand negative.
        env.x("UPDATE inventory SET onhand = onhand - ? WHERE sku = ?", (qty, sku))
    env.x("DELETE FROM reservations WHERE order_id = ?", (args.order_id,))

    shipment_id = env.next_id("shipment", "shp_", start=700)
    env.x(
        "INSERT INTO shipments (id, order_id, stock_was_reserved, at) VALUES (?, ?, ?, ?)",
        (shipment_id, args.order_id, int(covered), env.clock.now()),
    )
    env.x("UPDATE orders SET status = 'shipped' WHERE id = ?", (args.order_id,))
    return {
        "shipment_id": shipment_id,
        "order_id": args.order_id,
        "order_status": "shipped",
        "lines_shipped": [{"sku": r["sku"], "qty": int(r["qty"])} for r in lines],
    }


__all__ = [
    "add_line_item",
    "cancel_order",
    "charge_payment",
    "create_order",
    "refund_payment",
    "release_stock",
    "reserve_stock",
    "ship_order",
]
