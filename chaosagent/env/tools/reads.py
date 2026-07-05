"""The six read tools.

Reads never mutate, so they are trivially idempotent. They matter to the study
anyway: the `stale` and `silent_empty` faults corrupt reads, and the
verification-read mechanism in the `guarded` config is built entirely out of
them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import Field

from chaosagent.env.registry import tool
from chaosagent.env.tools.common import (
    Args,
    order_view,
    payment_totals,
    require_customer,
    require_order,
)

if TYPE_CHECKING:  # pragma: no cover
    from chaosagent.env.world import Environment


class GetOrderArgs(Args):
    order_id: str = Field(description="Identifier of the order to read.")


@tool(
    name="get_order",
    kind="read",
    idempotent=True,
    args_model=GetOrderArgs,
    description=(
        "Read one order: status, total, line items, and how much has been "
        "charged and refunded against it."
    ),
)
def get_order(env: Environment, args: GetOrderArgs) -> dict[str, Any]:
    return order_view(env, args.order_id)


class ListOrdersArgs(Args):
    customer_id: str | None = Field(
        default=None, description="Restrict to one customer's orders."
    )
    status: str | None = Field(
        default=None,
        description="Restrict to one status: draft, reserved, paid, shipped, cancelled.",
    )


@tool(
    name="list_orders",
    kind="read",
    idempotent=True,
    args_model=ListOrdersArgs,
    description="List orders, optionally filtered by customer and/or status.",
)
def list_orders(env: Environment, args: ListOrdersArgs) -> list[dict[str, Any]]:
    sql = "SELECT id FROM orders WHERE 1 = 1"
    params: list[Any] = []
    if args.customer_id is not None:
        sql += " AND customer_id = ?"
        params.append(args.customer_id)
    if args.status is not None:
        sql += " AND status = ?"
        params.append(args.status)
    sql += " ORDER BY id"
    return [order_view(env, r["id"]) for r in env.q(sql, tuple(params))]


class GetInventoryArgs(Args):
    sku: str | None = Field(
        default=None, description="A single SKU. Omit to list the whole inventory."
    )


@tool(
    name="get_inventory",
    kind="read",
    idempotent=True,
    args_model=GetInventoryArgs,
    description=(
        "Read stock levels. `available` is on-hand minus reserved and is the "
        "number that governs whether a reservation will succeed."
    ),
)
def get_inventory(env: Environment, args: GetInventoryArgs) -> list[dict[str, Any]]:
    if args.sku is not None:
        rows = env.q("SELECT * FROM inventory WHERE sku = ?", (args.sku,))
    else:
        rows = env.q("SELECT * FROM inventory ORDER BY sku")
    return [
        {
            "sku": r["sku"],
            "onhand": int(r["onhand"]),
            "reserved": int(r["reserved"]),
            "available": int(r["onhand"]) - int(r["reserved"]),
        }
        for r in rows
    ]


class GetCustomerArgs(Args):
    customer_id: str = Field(description="Identifier of the customer to read.")


@tool(
    name="get_customer",
    kind="read",
    idempotent=True,
    args_model=GetCustomerArgs,
    description="Read a customer record and the ids of their orders.",
)
def get_customer(env: Environment, args: GetCustomerArgs) -> dict[str, Any]:
    row = require_customer(env, args.customer_id)
    orders = [
        r["id"]
        for r in env.q(
            "SELECT id FROM orders WHERE customer_id = ? ORDER BY id", (args.customer_id,)
        )
    ]
    return {
        "customer_id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "order_ids": orders,
    }


class GetPaymentStatusArgs(Args):
    order_id: str = Field(description="Identifier of the order to inspect.")


@tool(
    name="get_payment_status",
    kind="read",
    idempotent=True,
    args_model=GetPaymentStatusArgs,
    description=(
        "Read the full payment ledger for an order: every charge and refund in "
        "order, plus totals. This is the verification read to use after any "
        "failed or uncertain payment call."
    ),
)
def get_payment_status(env: Environment, args: GetPaymentStatusArgs) -> dict[str, Any]:
    require_order(env, args.order_id)
    charged, refunded, net = payment_totals(env, args.order_id)
    ledger = [
        {
            "payment_id": r["id"],
            "kind": r["kind"],
            "amount_cents": int(r["amount_cents"]),
            "at": int(r["at"]),
        }
        for r in env.q(
            "SELECT * FROM payments WHERE order_id = ? ORDER BY seq", (args.order_id,)
        )
    ]
    return {
        "order_id": args.order_id,
        "amount_charged_cents": charged,
        "amount_refunded_cents": refunded,
        "net_paid_cents": net,
        "charge_count": sum(1 for e in ledger if e["kind"] == "charge"),
        "refund_count": sum(1 for e in ledger if e["kind"] == "refund"),
        "ledger": ledger,
    }


class SearchProductsArgs(Args):
    query: str | None = Field(
        default=None, description="Case-insensitive substring match on SKU or name."
    )
    category: str | None = Field(default=None, description="Exact category match.")


@tool(
    name="search_products",
    kind="read",
    idempotent=True,
    args_model=SearchProductsArgs,
    description="Search the catalogue by name/SKU substring and/or category.",
)
def search_products(env: Environment, args: SearchProductsArgs) -> list[dict[str, Any]]:
    sql = "SELECT * FROM products WHERE 1 = 1"
    params: list[Any] = []
    if args.query:
        sql += " AND (LOWER(sku) LIKE ? OR LOWER(name) LIKE ?)"
        like = f"%{args.query.lower()}%"
        params += [like, like]
    if args.category:
        sql += " AND category = ?"
        params.append(args.category)
    sql += " ORDER BY sku"
    return [
        {
            "sku": r["sku"],
            "name": r["name"],
            "price_cents": int(r["price_cents"]),
            "category": r["category"],
        }
        for r in env.q(sql, tuple(params))
    ]


__all__ = [
    "get_customer",
    "get_inventory",
    "get_order",
    "get_payment_status",
    "list_orders",
    "search_products",
]
