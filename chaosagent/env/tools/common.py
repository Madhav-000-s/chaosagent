"""Shared helpers and argument bases for the tool implementations."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from chaosagent.env.errors import not_found

if TYPE_CHECKING:  # pragma: no cover
    from chaosagent.env.world import Environment


class Args(BaseModel):
    """Base for every tool's argument model.

    Extra keys are ignored rather than rejected: models routinely hallucinate a
    plausible-looking extra field, and failing the call for that would measure
    schema-following rather than fault recovery.
    """

    model_config = ConfigDict(extra="ignore")


class IdempotentArgs(Args):
    """Base for writes that accept an idempotency key.

    The key is optional at the schema level for every config, so that the
    presence of the *field* leaks nothing: what differs between configs is
    whether the agent is told to populate it.
    """

    idempotency_key: str | None = Field(
        default=None,
        description=(
            "Optional client-generated key. Replaying a call with a key that was "
            "already used returns the original result without executing again."
        ),
    )


# ---------------------------------------------------------------------------
# Row lookups
# ---------------------------------------------------------------------------


def require_order(env: Environment, order_id: str) -> sqlite3.Row:
    row = env.q1("SELECT * FROM orders WHERE id = ?", (order_id,))
    if row is None:
        raise not_found("Order", order_id)
    return row


def require_customer(env: Environment, customer_id: str) -> sqlite3.Row:
    row = env.q1("SELECT * FROM customers WHERE id = ?", (customer_id,))
    if row is None:
        raise not_found("Customer", customer_id)
    return row


def require_product(env: Environment, sku: str) -> sqlite3.Row:
    row = env.q1("SELECT * FROM products WHERE sku = ?", (sku,))
    if row is None:
        raise not_found("Product", sku)
    return row


# ---------------------------------------------------------------------------
# Derived views
# ---------------------------------------------------------------------------


def payment_totals(env: Environment, order_id: str) -> tuple[int, int, int]:
    """``(charged, refunded, net)`` in cents for an order."""
    charged = 0
    refunded = 0
    for row in env.q(
        "SELECT kind, amount_cents FROM payments WHERE order_id = ?", (order_id,)
    ):
        if row["kind"] == "charge":
            charged += int(row["amount_cents"])
        else:
            refunded += int(row["amount_cents"])
    return charged, refunded, charged - refunded


def lines_of(env: Environment, order_id: str) -> list[dict[str, Any]]:
    return [
        {
            "line_id": r["id"],
            "sku": r["sku"],
            "qty": int(r["qty"]),
            "unit_price_cents": int(r["unit_price_cents"]),
            "subtotal_cents": int(r["subtotal_cents"]),
        }
        for r in env.q(
            "SELECT * FROM line_items WHERE order_id = ? ORDER BY id", (order_id,)
        )
    ]


def order_view(env: Environment, order_id: str) -> dict[str, Any]:
    """The canonical read-model of an order. Every tool that returns an order
    returns exactly this shape, so a corrupted payload is indistinguishable in
    structure from a real one."""
    row = require_order(env, order_id)
    charged, refunded, net = payment_totals(env, order_id)
    return {
        "order_id": row["id"],
        "customer_id": row["customer_id"],
        "status": row["status"],
        "total_cents": int(row["total_cents"]),
        "line_items": lines_of(env, order_id),
        "amount_charged_cents": charged,
        "amount_refunded_cents": refunded,
        "net_paid_cents": net,
        "created_at": int(row["created_at"]),
    }


def available(env: Environment, sku: str) -> int:
    row = env.q1("SELECT onhand, reserved FROM inventory WHERE sku = ?", (sku,))
    if row is None:
        return 0
    return int(row["onhand"]) - int(row["reserved"])


def recompute_total(env: Environment, order_id: str) -> int:
    total = 0
    for r in env.q("SELECT subtotal_cents FROM line_items WHERE order_id = ?", (order_id,)):
        total += int(r["subtotal_cents"])
    env.x("UPDATE orders SET total_cents = ? WHERE id = ?", (total, order_id))
    return total


__all__ = [
    "Args",
    "IdempotentArgs",
    "available",
    "lines_of",
    "order_view",
    "payment_totals",
    "recompute_total",
    "require_customer",
    "require_order",
    "require_product",
]
