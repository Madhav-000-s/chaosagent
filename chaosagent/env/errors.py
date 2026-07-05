"""Environment-level failures.

The environment raises :class:`ToolError` for *genuine* refusals — a missing
order, a violated precondition, insufficient stock. The fault injector never
raises these; it synthesises transport-shaped envelopes instead. Keeping the
two sources distinct is what lets the scorer separate "the agent asked for
something impossible" from "the harness broke the call".
"""

from __future__ import annotations

from chaosagent.types import ErrorCode, ErrorEnvelope


class ToolError(Exception):
    """A tool refused to execute. Carries a fully-formed error envelope."""

    def __init__(self, envelope: ErrorEnvelope) -> None:
        super().__init__(envelope.message)
        self.envelope = envelope


def not_found(what: str, ident: str) -> ToolError:
    return ToolError(
        ErrorEnvelope(
            code="NOT_FOUND",
            message=f"{what} '{ident}' does not exist.",
            retryable=False,
            state_may_have_changed=False,
            idempotency_supported=False,
            hint=f"List available {what.lower()}s before referencing one by id.",
        )
    )


def precondition(message: str, hint: str | None = None) -> ToolError:
    return ToolError(
        ErrorEnvelope(
            code="PRECONDITION",
            message=message,
            retryable=False,
            state_may_have_changed=False,
            idempotency_supported=False,
            hint=hint or "Read the current state of the entity before acting on it.",
        )
    )


def invalid_args(message: str) -> ToolError:
    return ToolError(
        ErrorEnvelope(
            code="INVALID_ARGS",
            message=message,
            retryable=False,
            state_may_have_changed=False,
            idempotency_supported=False,
            hint="Correct the arguments and call again.",
        )
    )


def insufficient_stock(sku: str, wanted: int, available: int) -> ToolError:
    return ToolError(
        ErrorEnvelope(
            code="INSUFFICIENT_STOCK",
            message=(
                f"Cannot reserve {wanted} of '{sku}': only {available} available "
                f"(on-hand minus already-reserved)."
            ),
            retryable=False,
            state_may_have_changed=False,
            idempotency_supported=False,
            hint="Reduce the quantity, restock, or release competing reservations.",
        )
    )


def envelope(
    code: ErrorCode,
    message: str,
    *,
    retryable: bool,
    state_may_have_changed: bool,
    idempotency_supported: bool = False,
    hint: str | None = None,
) -> ErrorEnvelope:
    """Construct an envelope directly — used by the fault injector."""
    return ErrorEnvelope(
        code=code,
        message=message,
        retryable=retryable,
        state_may_have_changed=state_may_have_changed,
        idempotency_supported=idempotency_supported,
        hint=hint,
    )


__all__ = [
    "ToolError",
    "envelope",
    "insufficient_stock",
    "invalid_args",
    "not_found",
    "precondition",
]
