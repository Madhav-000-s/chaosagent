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
