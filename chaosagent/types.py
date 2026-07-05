"""Core types shared across the harness.

Everything that crosses a module boundary is a Pydantic model or a frozen
dataclass. The two that matter most:

* :class:`ToolResult` — what the environment returns, and therefore the exact
  shape the fault injector must also return. If an injected fault produced a
  differently-shaped object the agent could distinguish it from a real failure
  and the whole study would be invalid.
* :class:`ErrorEnvelope` — the interface contribution. Which of its fields the
  agent gets to see is the independent variable in the `contract` family of
  configurations.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------

ErrorCode = Literal[
    # Genuine environment refusals — the world said no.
    "NOT_FOUND",
    "PRECONDITION",
    "INVALID_ARGS",
    "INSUFFICIENT_STOCK",
    # Transport-shaped failures — these are what the injector synthesises.
    "TIMEOUT",
    "MALFORMED",
    "RATE_LIMIT",
    "UNAVAILABLE",
    # Harness-level.
    "UNKNOWN",
]

#: Error codes an agent may legitimately see as a *transport* problem, i.e. the
#: request may or may not have been applied. Used by the contract configs.
TRANSPORT_CODES: frozenset[str] = frozenset(
    {"TIMEOUT", "MALFORMED", "RATE_LIMIT", "UNAVAILABLE"}
)


class ErrorEnvelope(BaseModel):
    """Typed error contract returned by every failing tool call.

    The `naive`/`retry`/`reflect` configurations flatten this to a bare string
    before it reaches the agent; the `contract` family shows it in full. That
    difference is the entire interface intervention under test.
    """

    model_config = ConfigDict(frozen=True)

    code: ErrorCode
    message: str
    retryable: bool
    #: The field that prevents double-charges. True when the harness cannot
    #: promise the write did *not* land — e.g. a timeout after commit, or a
    #: dropped response.
    state_may_have_changed: bool
    idempotency_supported: bool
    hint: str | None = None

    def to_raw_string(self) -> str:
        """Flatten to the untyped form the `naive` family sees.

        Deliberately lossy: it keeps the human-readable message and drops every
        machine-actionable field. This is what most agent frameworks actually
        hand back to the model today.
        """
        return f"Error: {self.message}"


class ToolResult(BaseModel):
    """Uniform return shape for every tool call, successful or not."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    payload: dict[str, Any] | list[Any] | None = None
    error: ErrorEnvelope | None = None

    @classmethod
    def success(cls, payload: dict[str, Any] | list[Any] | None) -> ToolResult:
        return cls(ok=True, payload=payload, error=None)

    @classmethod
    def failure(cls, error: ErrorEnvelope) -> ToolResult:
        return cls(ok=False, payload=None, error=error)


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------


class ToolSpec(BaseModel):
    """Declarative metadata the harness reads and the agent may or may not see.

    `idempotent` and `accepts_idempotency_key` drive both the fault injector
    (which faults apply to which tools) and the scorer (which repeated calls
    count as a double execution).
    """

    model_config = ConfigDict(frozen=True)

    name: str
    kind: Literal["read", "write"]
    idempotent: bool
    accepts_idempotency_key: bool
    preconditions: list[str] = Field(default_factory=list)
    invariants_touched: list[str] = Field(default_factory=list)
    description: str = ""
    #: JSON Schema for the tool arguments, derived from the Pydantic args model.
    parameters: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Environment records
# ---------------------------------------------------------------------------


class EnvCall(BaseModel):
    """One entry in the environment's own ground-truth call log.

    This is *not* the agent's trace. In a `partial_write` fault the agent's
    trace shows one failed call while this log shows one committed write; that
    discrepancy is the measurement the whole project is built around.
    """

    model_config = ConfigDict(frozen=True)

    seq: int
    tool: str
    args: dict[str, Any]
    #: True when the tool body actually ran and could have mutated state.
    #: False for precondition failures and for idempotency-key replays.
    executed: bool
    ok: bool
    error_code: str | None = None
    #: True when an idempotency key short-circuited a repeat execution.
    idempotent_replay: bool = False
    idempotency_key: str | None = None
    at: int = 0


class InvariantViolation(BaseModel):
    """A broken state invariant. Any violation is a hard failure."""

    model_config = ConfigDict(frozen=True)

    name: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.name}: {self.detail}"


class WorldState(BaseModel):
    """A canonical, comparable snapshot of the world.

    Ordering is fixed and all values are primitives so that two states compare
    with ``==`` and hash identically across processes.
    """

    model_config = ConfigDict(frozen=True)

    customers: list[dict[str, Any]]
    products: list[dict[str, Any]]
    inventory: list[dict[str, Any]]
    orders: list[dict[str, Any]]
    line_items: list[dict[str, Any]]
    payments: list[dict[str, Any]]
    shipments: list[dict[str, Any]]

    def digest(self) -> str:
        """Stable content hash — the determinism tests compare these."""
        import hashlib
        import json

        blob = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Initial-state seeds (task fixtures)
# ---------------------------------------------------------------------------


class CustomerSeed(BaseModel):
    id: str
    name: str
    email: str


class ProductSeed(BaseModel):
    sku: str
    name: str
    price_cents: int
    category: str = "general"


class InventorySeed(BaseModel):
    sku: str
    onhand: int
    reserved: int = 0


class LineSeed(BaseModel):
    sku: str
    qty: int


class OrderSeed(BaseModel):
    """A pre-existing order in the initial state.

    `status` drives which side effects are materialised at seed time:
    ``reserved``/``paid``/``shipped`` create reservations, ``paid``/``shipped``
    create a charge. `extra_charges` deliberately seeds duplicate charges for
    the reconciliation task — such a state starts with a `no_double_charge`
    violation on purpose, which is why the scorer reports *newly introduced*
    violations rather than absolute ones.
    """

    id: str
    customer_id: str
    status: Literal["draft", "reserved", "paid", "shipped", "cancelled"] = "draft"
    lines: list[LineSeed] = Field(default_factory=list)
    extra_charges: list[int] = Field(default_factory=list)


class InitState(BaseModel):
    customers: list[CustomerSeed] = Field(default_factory=list)
    products: list[ProductSeed] = Field(default_factory=list)
    inventory: list[InventorySeed] = Field(default_factory=list)
    orders: list[OrderSeed] = Field(default_factory=list)


__all__ = [
    "TRANSPORT_CODES",
    "CustomerSeed",
    "EnvCall",
    "ErrorCode",
    "ErrorEnvelope",
    "InitState",
    "InventorySeed",
    "InvariantViolation",
    "LineSeed",
    "OrderSeed",
    "ProductSeed",
    "ToolResult",
    "ToolSpec",
    "WorldState",
]
