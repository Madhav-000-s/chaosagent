"""The three injected strategy objects that separate the configurations.

All eight configs share one ReAct loop. They differ only in these objects, which
is what makes the guard decomposition honest: no arm can gain an incidental
advantage from a differently-written loop.

* :class:`ErrorFormatter` — what an error *looks like* to the agent. This is the
  interface intervention: the same envelope, flattened to a string or shown in
  full.
* :class:`CallDecorator` — whether writes carry an idempotency key. A structural
  mechanism, not a prompted one; the agent is never asked to generate keys.
* :class:`RecoveryPolicy` — what the harness does after an error. Four
  independent flags rather than a class hierarchy, so the ablation table is
  literally a set of booleans over one code path.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from chaosagent.types import ErrorEnvelope

# ---------------------------------------------------------------------------
# Error formatting
# ---------------------------------------------------------------------------


class ErrorFormatter:
    """Flattens an envelope to the untyped string most frameworks hand back."""

    name = "raw"

    def format(self, envelope: ErrorEnvelope) -> str:
        return envelope.to_raw_string()


class EnvelopeFormatter(ErrorFormatter):
    """Shows the full typed contract. The interface contribution.

    ``state_may_have_changed`` is the field that does the work: it is the only
    thing distinguishing a failure that may have committed from one that
    certainly did not.
    """

    name = "envelope"

    def format(self, envelope: ErrorEnvelope) -> str:
        return json.dumps(
            {
                "error": envelope.code,
                "message": envelope.message,
                "retryable": envelope.retryable,
                "state_may_have_changed": envelope.state_may_have_changed,
                "idempotency_supported": envelope.idempotency_supported,
                "hint": envelope.hint,
            },
            indent=2,
        )


class OracleFormatter(EnvelopeFormatter):
    """Envelope plus a disclosure of exactly what was injected.

    The deliberate exception to fault transparency, and an upper bound rather
    than a strategy: it assumes perfect fault attribution, which no real system
    has. The gap between `guarded` and `oracle` is the remaining headroom.
    """

    name = "oracle"

    def format_with_disclosure(self, envelope: ErrorEnvelope, disclosure: str) -> str:
        return self.format(envelope) + f"\n\n[FAULT DISCLOSURE] {disclosure}"


# ---------------------------------------------------------------------------
# Call decoration
# ---------------------------------------------------------------------------


class CallDecorator:
    """Identity. The agent's arguments reach the tool untouched."""

    name = "identity"

    def decorate(self, tool: str, args: dict[str, Any], spec: Any) -> dict[str, Any]:
        return args


class IdempotencyKeyDecorator(CallDecorator):
    """Attaches a content-addressed idempotency key to non-idempotent writes.

    The key is a hash of the tool plus its arguments, so a *retry of the same
    logical operation* carries the same key and is deduplicated by the
    environment, while a genuinely different call gets a different key. That is
    the entire mechanism — no agent cooperation required, which is the point:
    it is an interface property, not a prompting one.
    """

    name = "idempotency_key"

    def decorate(self, tool: str, args: dict[str, Any], spec: Any) -> dict[str, Any]:
        if spec is None or not spec.accepts_idempotency_key:
            return args
        if args.get("idempotency_key"):
            return args
        payload = {k: v for k, v in args.items() if k != "idempotency_key"}
        digest = hashlib.sha256(
            json.dumps({"tool": tool, "args": payload}, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        return {**args, "idempotency_key": f"ck_{digest}"}


# ---------------------------------------------------------------------------
# Recovery policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryPolicy:
    """What the harness does after the agent sees an error.

    Flags rather than subclasses, so ``contract+idem`` and ``contract+verify``
    run the identical code path as ``guarded`` with one flag flipped.
    """

    name: str = "none"
    #: Re-issue the identical call up to n times before surfacing the error.
    #: The framework default, and the one that double-charges.
    blind_retry: int = 0
    #: Insert an extra LLM turn asking what went wrong. Costs a full turn.
    reflect: bool = False
    #: Append protocol guidance derived from the envelope's own fields.
    contract_aware: bool = False
    #: After an error on a write that may have committed, automatically issue a
    #: verification read. Costs one extra tool call per error — reported.
    verify_read: bool = False

    @property
    def is_control(self) -> bool:
        return not (self.blind_retry or self.reflect or self.contract_aware or self.verify_read)


#: Which read answers "did that write land?", per write tool.
VERIFICATION_READS: dict[str, tuple[str, tuple[str, ...]]] = {
    "charge_payment": ("get_payment_status", ("order_id",)),
    "refund_payment": ("get_payment_status", ("order_id",)),
    "create_order": ("list_orders", ("customer_id",)),
    "add_line_item": ("get_order", ("order_id",)),
    "reserve_stock": ("get_order", ("order_id",)),
    "release_stock": ("get_order", ("order_id",)),
    "cancel_order": ("get_order", ("order_id",)),
    "ship_order": ("get_order", ("order_id",)),
}


def verification_call(tool: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The read that establishes whether ``tool`` actually executed."""
    entry = VERIFICATION_READS.get(tool)
    if entry is None:
        return None
    read_tool, fields = entry
    read_args = {f: args[f] for f in fields if f in args}
    if len(read_args) != len(fields):
        return None
    return read_tool, read_args


def contract_guidance(envelope: ErrorEnvelope) -> str:
    """Protocol advice derived from the envelope, not from the fault.

    Reads only fields the agent can already see, so this adds no information —
    it makes explicit what a careful reader of the contract would conclude.
    """
    if envelope.state_may_have_changed:
        base = (
            "This request may or may not have been applied. Do NOT simply repeat it: "
            "read the affected entity first and decide based on what you find."
        )
        if envelope.idempotency_supported:
            base += (
                " If you do repeat it, reuse the exact same idempotency_key so it "
                "cannot execute twice."
            )
        return base
    if envelope.retryable:
        return "Nothing was applied. Retrying this call is safe; back off and bound your retries."
    return "This will not succeed as issued. Re-plan rather than retrying."


__all__ = [
    "VERIFICATION_READS",
    "CallDecorator",
    "EnvelopeFormatter",
    "ErrorFormatter",
    "IdempotencyKeyDecorator",
    "OracleFormatter",
    "RecoveryPolicy",
    "contract_guidance",
    "verification_call",
]
