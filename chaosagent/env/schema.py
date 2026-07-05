"""DDL for the order/fulfilment world.

Held as a Python constant rather than a data file so that packaging never has
to ship SQL as package data. ``docs/env_schema.sql`` is a generated copy kept
for readers; ``tests/test_env_determinism.py`` asserts the two never drift.
Regenerate it with ``chaosagent schema --out docs/env_schema.sql``.

Design notes worth stating, because they are load-bearing for the experiment:

* Money is integer cents everywhere. The `wrong_type` fault exists precisely to
  exploit agents that forget this.
* `payments` is append-only and carries both charges and refunds. Nothing is
  ever updated in place, so `no_double_charge` is decidable from history alone.
* `env_call_log` is the environment's own record of what executed. It is not
  derived from anything the agent saw.
"""

from __future__ import annotations

DDL = """
CREATE TABLE customers (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL
);

CREATE TABLE products (
    sku         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    category    TEXT NOT NULL DEFAULT 'general'
);

CREATE TABLE inventory (
    sku         TEXT PRIMARY KEY,
    onhand      INTEGER NOT NULL DEFAULT 0,
    reserved    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE orders (
    id          TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    status      TEXT NOT NULL,
    total_cents INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);

CREATE TABLE line_items (
    id               TEXT PRIMARY KEY,
    order_id         TEXT NOT NULL,
    sku              TEXT NOT NULL,
    qty              INTEGER NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    subtotal_cents   INTEGER NOT NULL
);

-- One row per (order, sku) currently holding stock.
CREATE TABLE reservations (
    order_id    TEXT NOT NULL,
    sku         TEXT NOT NULL,
    qty         INTEGER NOT NULL,
    at          INTEGER NOT NULL,
    PRIMARY KEY (order_id, sku)
);

-- Append-only ledger. kind is 'charge' or 'refund'.
CREATE TABLE payments (
    id           TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL,
    kind         TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    at           INTEGER NOT NULL,
    seq          INTEGER NOT NULL
);

CREATE TABLE shipments (
    id                 TEXT PRIMARY KEY,
    order_id           TEXT NOT NULL,
    -- Recorded at ship time: did reservations actually cover every line?
    -- A second ship_order on the same order finds the reservations already
    -- consumed and writes 0 here, which is how `shipped_implies_reserved`
    -- catches a double shipment after the fact.
    stock_was_reserved INTEGER NOT NULL,
    at                 INTEGER NOT NULL
);

-- Idempotency-key ledger. Keyed by (tool, key); stores the payload returned by
-- the original execution so a replay is byte-identical and free of side effects.
CREATE TABLE idempotency (
    tool         TEXT NOT NULL,
    key          TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    at           INTEGER NOT NULL,
    PRIMARY KEY (tool, key)
);

-- Ground truth. Written by Environment.call() for every attempt, successful or
-- not, including attempts the injector later hides from the agent.
CREATE TABLE env_call_log (
    seq              INTEGER PRIMARY KEY,
    tool             TEXT NOT NULL,
    args_json        TEXT NOT NULL,
    executed         INTEGER NOT NULL,
    ok               INTEGER NOT NULL,
    error_code       TEXT,
    idempotent_replay INTEGER NOT NULL DEFAULT 0,
    idempotency_key  TEXT,
    at               INTEGER NOT NULL
);

-- Bookkeeping for deterministic id generation. Single row.
CREATE TABLE counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

#: Tables dumped, in this order, by ``Environment.state()`` and by snapshots.
#: Fixed ordering is what makes state digests comparable.
STATE_TABLES: tuple[str, ...] = (
    "customers",
    "products",
    "inventory",
    "orders",
    "line_items",
    "payments",
    "shipments",
)

#: Every table, in dependency-free creation order, for snapshot/restore.
ALL_TABLES: tuple[str, ...] = (
    "customers",
    "products",
    "inventory",
    "orders",
    "line_items",
    "reservations",
    "payments",
    "shipments",
    "idempotency",
    "env_call_log",
    "counters",
)

#: Stable sort key per table so dumps are order-independent of insertion.
SORT_KEYS: dict[str, str] = {
    "customers": "id",
    "products": "sku",
    "inventory": "sku",
    "orders": "id",
    "line_items": "id",
    "reservations": "order_id, sku",
    "payments": "seq",
    "shipments": "id",
    "idempotency": "tool, key",
    "env_call_log": "seq",
    "counters": "name",
}

__all__ = ["ALL_TABLES", "DDL", "SORT_KEYS", "STATE_TABLES"]
