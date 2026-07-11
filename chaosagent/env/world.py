"""The deterministic world.

An order/fulfilment system backed by an in-memory SQLite database. It knows
nothing about agents, faults or LLMs — it executes tool calls, records what it
actually did, and can be snapshotted and restored.

Two properties the rest of the harness depends on:

* **Pure function of (seed, action sequence).** No wall clock, no ``random``
  without a seed, no dict-ordering dependence. Same inputs → same state digest.
* **Independent ground truth.** ``env.call_log()`` records what executed. The
  agent's trace records what the agent saw. Comparing the two is the entire
  measurement.

A design decision that is easy to misread as a bug: the write tools do **not**
defend themselves against repetition. ``charge_payment`` will happily charge a
paid order a second time, exactly as a real payment gateway does when you retry
without an idempotency key. If preconditions rejected the second charge there
would be no double-charge to measure and the study would be vacuous. Safety
comes from idempotency keys — which is the mechanism under test — not from the
preconditions.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from chaosagent.env import registry
from chaosagent.env.clock import EPOCH, VirtualClock
from chaosagent.env.errors import ToolError
from chaosagent.env.invariants import check_all
from chaosagent.env.schema import ALL_TABLES, DDL, SORT_KEYS, STATE_TABLES
from chaosagent.types import (
    EnvCall,
    ErrorEnvelope,
    InitState,
    InvariantViolation,
    ToolResult,
    WorldState,
)

#: Ticks of virtual time consumed by one tool call.
TICKS_PER_CALL = 1


class Environment:
    """The world. One instance per run."""

    def __init__(self, seed: int = 0, init_state: InitState | None = None) -> None:
        self.seed = seed
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(DDL)
        self._clock = VirtualClock(EPOCH)
        self._seq = 0
        self._payment_seq = 0
        registry._ensure_loaded()
        if init_state is not None:
            self._seed_world(init_state)

    # -- low-level SQL helpers used by tool implementations -----------------

    def q(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, params))

    def q1(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        cur = self._conn.execute(sql, params)
        return cur.fetchone()

    def x(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._conn.execute(sql, params)

    def next_id(self, kind: str, prefix: str, start: int = 0) -> str:
        """Deterministic identifier generation, backed by the counters table.

        ``start`` is the counter's value *before* the first allocation, so the
        first id handed out is ``start + 1``.
        """
        row = self.q1("SELECT value FROM counters WHERE name = ?", (kind,))
        nxt = int(row["value"]) + 1 if row else start + 1
        if row:
            self.x("UPDATE counters SET value = ? WHERE name = ?", (nxt, kind))
        else:
            self.x("INSERT INTO counters (name, value) VALUES (?, ?)", (kind, nxt))
        return f"{prefix}{nxt}"

    @property
    def clock(self) -> VirtualClock:
        return self._clock

    # -- seeding ------------------------------------------------------------

    def _seed_world(self, init: InitState) -> None:
        for c in init.customers:
            self.x(
                "INSERT INTO customers (id, name, email) VALUES (?, ?, ?)",
                (c.id, c.name, c.email),
            )
        for p in init.products:
            self.x(
                "INSERT INTO products (sku, name, price_cents, category) VALUES (?, ?, ?, ?)",
                (p.sku, p.name, p.price_cents, p.category),
            )
        for inv in init.inventory:
            self.x(
                "INSERT INTO inventory (sku, onhand, reserved) VALUES (?, ?, ?)",
                (inv.sku, inv.onhand, inv.reserved),
            )

        now = self._clock.now()
        for o in init.orders:
            total = 0
            self.x(
                "INSERT INTO orders (id, customer_id, status, total_cents, created_at) "
                "VALUES (?, ?, ?, 0, ?)",
                (o.id, o.customer_id, o.status, now),
            )
            for idx, line in enumerate(o.lines, start=1):
                prod = self.q1(
                    "SELECT price_cents FROM products WHERE sku = ?", (line.sku,)
                )
                if prod is None:
                    raise ValueError(
                        f"init_state order {o.id} references unknown sku '{line.sku}'"
                    )
                unit = int(prod["price_cents"])
                subtotal = unit * line.qty
                total += subtotal
                self.x(
                    "INSERT INTO line_items "
                    "(id, order_id, sku, qty, unit_price_cents, subtotal_cents) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (f"{o.id}_li{idx}", o.id, line.sku, line.qty, unit, subtotal),
                )
            self.x(
                "UPDATE orders SET total_cents = ? WHERE id = ?", (total, o.id)
            )

            # Materialise the side effects implied by the seeded status.
            if o.status in ("reserved", "paid"):
                for line in o.lines:
                    self.x(
                        "INSERT INTO reservations (order_id, sku, qty, at) VALUES (?, ?, ?, ?)",
                        (o.id, line.sku, line.qty, now),
                    )
                    self.x(
                        "UPDATE inventory SET reserved = reserved + ? WHERE sku = ?",
                        (line.qty, line.sku),
                    )
            if o.status in ("paid", "shipped"):
                self._record_payment(o.id, "charge", total, now)
            if o.status == "shipped":
                for line in o.lines:
                    self.x(
                        "UPDATE inventory SET onhand = onhand - ? WHERE sku = ?",
                        (line.qty, line.sku),
                    )
                self.x(
                    "INSERT INTO shipments (id, order_id, stock_was_reserved, at) "
                    "VALUES (?, ?, 1, ?)",
                    (f"{o.id}_shp1", o.id, now),
                )
            # Deliberate pre-existing duplicate charges (reconciliation task).
            for amount in o.extra_charges:
                self._record_payment(o.id, "charge", amount, now)

        self._conn.commit()

    def _record_payment(self, order_id: str, kind: str, amount: int, at: int) -> str:
        self._payment_seq += 1
        pid = f"pay_{self._payment_seq}"
        self.x(
            "INSERT INTO payments (id, order_id, kind, amount_cents, at, seq) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pid, order_id, kind, amount, at, self._payment_seq),
        )
        return pid

    def record_payment(self, order_id: str, kind: str, amount: int) -> str:
        """Public hook for the payment tools."""
        return self._record_payment(order_id, kind, amount, self._clock.now())

    # -- the tool interface -------------------------------------------------

    def call(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Execute a tool and record the attempt in the ground-truth call log.

        Never raises for ordinary refusals — a :class:`ToolError` from a tool
        body becomes a failed :class:`ToolResult`. The only exceptions that
        escape are genuine harness bugs.
        """
        self._clock.advance(TICKS_PER_CALL)
        rt = registry.get(tool)

        if rt is None:
            env = ErrorEnvelope(
                code="NOT_FOUND",
                message=f"No such tool '{tool}'.",
                retryable=False,
                state_may_have_changed=False,
                idempotency_supported=False,
                hint="Call one of the tools listed in the schema.",
            )
            self._log(tool, args, executed=False, ok=False, error_code="NOT_FOUND")
            return ToolResult.failure(env)

        try:
            parsed = rt.args_model.model_validate(args)
        except Exception as exc:  # pydantic.ValidationError
            env = ErrorEnvelope(
                code="INVALID_ARGS",
                message=f"Invalid arguments for '{tool}': {_terse(exc)}",
                retryable=False,
                state_may_have_changed=False,
                idempotency_supported=rt.spec.accepts_idempotency_key,
                hint="Fix the argument types or supply the missing fields.",
            )
            self._log(tool, args, executed=False, ok=False, error_code="INVALID_ARGS")
            return ToolResult.failure(env)

        key = getattr(parsed, "idempotency_key", None) if rt.spec.accepts_idempotency_key else None

        # Idempotency-key replay: return the original payload, execute nothing.
        if key:
            prior = self.q1(
                "SELECT payload_json FROM idempotency WHERE tool = ? AND key = ?",
                (tool, key),
            )
            if prior is not None:
                self._log(
                    tool,
                    args,
                    executed=False,
                    ok=True,
                    idempotent_replay=True,
                    idempotency_key=key,
                )
                return ToolResult.success(json.loads(prior["payload_json"]))

        try:
            payload = rt.fn(self, parsed)
        except ToolError as err:
            self._log(
                tool,
                args,
                executed=False,
                ok=False,
                error_code=err.envelope.code,
                idempotency_key=key,
            )
            return ToolResult.failure(err.envelope)
        except sqlite3.Error as err:
            # A constraint violation is a harness bug, not agent behaviour. It
            # must still not take the run down: one corrupted call is a datum,
            # a crashed sweep is lost work. Surfaced as UNKNOWN and logged as
            # executed, because the transaction state is genuinely uncertain.
            self._conn.rollback()
            self._log(
                tool, args, executed=True, ok=False, error_code="UNKNOWN", idempotency_key=key
            )
            return ToolResult.failure(
                ErrorEnvelope(
                    code="UNKNOWN",
                    message=f"Internal storage error in '{tool}': {_terse(err)}",
                    retryable=False,
                    state_may_have_changed=True,
                    idempotency_supported=rt.spec.accepts_idempotency_key,
                    hint="Read the affected entity to establish its current state.",
                )
            )

        if key:
            self.x(
                "INSERT INTO idempotency (tool, key, payload_json, at) VALUES (?, ?, ?, ?)",
                (tool, key, json.dumps(payload, sort_keys=True), self._clock.now()),
            )

        self._log(tool, args, executed=True, ok=True, idempotency_key=key)
        self._conn.commit()
        return ToolResult.success(payload)

    def _log(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        executed: bool,
        ok: bool,
        error_code: str | None = None,
        idempotent_replay: bool = False,
        idempotency_key: str | None = None,
    ) -> None:
        self._seq += 1
        self.x(
            "INSERT INTO env_call_log "
            "(seq, tool, args_json, executed, ok, error_code, idempotent_replay, "
            " idempotency_key, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._seq,
                tool,
                json.dumps(args, sort_keys=True, default=str),
                int(executed),
                int(ok),
                error_code,
                int(idempotent_replay),
                idempotency_key,
                self._clock.now(),
            ),
        )

    # -- observation --------------------------------------------------------

    def state(self) -> WorldState:
        """A canonical snapshot of everything a state assertion may look at."""
        dump = {t: self._dump(t) for t in STATE_TABLES}
        return WorldState(**dump)

    def _dump(self, table: str) -> list[dict[str, Any]]:
        order = SORT_KEYS[table]
        return [dict(r) for r in self.q(f"SELECT * FROM {table} ORDER BY {order}")]

    def call_log(self) -> list[EnvCall]:
        rows = self.q("SELECT * FROM env_call_log ORDER BY seq")
        return [
            EnvCall(
                seq=r["seq"],
                tool=r["tool"],
                args=json.loads(r["args_json"]),
                executed=bool(r["executed"]),
                ok=bool(r["ok"]),
                error_code=r["error_code"],
                idempotent_replay=bool(r["idempotent_replay"]),
                idempotency_key=r["idempotency_key"],
                at=r["at"],
            )
            for r in rows
        ]

    def check_invariants(self) -> list[InvariantViolation]:
        return check_all(self)

    def digest(self) -> str:
        """Content hash of observable state. Used by the determinism tests."""
        return self.state().digest()

    # -- snapshot / restore -------------------------------------------------

    def snapshot(self) -> bytes:
        """Serialise the whole world, including the call log and the clock."""
        blob = {
            "clock": self._clock.state(),
            "seq": self._seq,
            "payment_seq": self._payment_seq,
            "tables": {t: self._dump_raw(t) for t in ALL_TABLES},
        }
        return json.dumps(blob, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def _dump_raw(self, table: str) -> list[dict[str, Any]]:
        order = SORT_KEYS[table]
        return [dict(r) for r in self.q(f"SELECT * FROM {table} ORDER BY {order}")]

    def restore(self, snap: bytes) -> None:
        blob = json.loads(snap.decode("utf-8"))
        for table in reversed(ALL_TABLES):
            self.x(f"DELETE FROM {table}")
        for table in ALL_TABLES:
            rows = blob["tables"].get(table, [])
            for row in rows:
                cols = ", ".join(row.keys())
                marks = ", ".join("?" for _ in row)
                self.x(
                    f"INSERT INTO {table} ({cols}) VALUES ({marks})",
                    tuple(row.values()),
                )
        self._clock.restore(blob["clock"])
        self._seq = blob["seq"]
        self._payment_seq = blob["payment_seq"]
        self._conn.commit()

    # -- misc ---------------------------------------------------------------

    def tool_specs(self) -> list[Any]:
        return registry.specs()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Environment:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - display only
        n = self.q1("SELECT COUNT(*) c FROM orders")
        return f"<Environment seed={self.seed} orders={n['c'] if n else 0} t={self._clock.now()}>"


def _terse(exc: Exception) -> str:
    """Compact a Pydantic validation error into one readable line."""
    text = str(exc).replace("\n", "; ")
    return text[:240]


__all__ = ["Environment", "TICKS_PER_CALL"]
