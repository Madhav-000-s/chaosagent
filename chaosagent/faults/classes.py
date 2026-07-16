"""The eight fault classes.

Each is one small, independently testable class. Every source of randomness
flows through the ``rng`` handed to ``apply``, so a fixed seed reproduces a
fault byte for byte.

The single most important property in this file is that **the envelopes an
injected fault produces are indistinguishable from ones the real world could
produce**, and that the two variants of ``timeout`` are indistinguishable from
*each other*. If a pre-commit timeout said "nothing happened" and a post-commit
timeout said "something might have", the agent could read ground truth off the
error and the `guarded` config's advantage would be an artefact. Both set
``state_may_have_changed`` on writes; only a verification read can tell them
apart, which is exactly the point.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from random import Random
from typing import TYPE_CHECKING, Any, Protocol

from chaosagent.faults.types import (
    BlockAndError,
    CorruptResult,
    DelayThenSucceed,
    FaultOutcome,
    PassThrough,
    SuppressResult,
)
from chaosagent.types import ErrorEnvelope, Payload, ToolResult, ToolSpec

if TYPE_CHECKING:  # pragma: no cover
    from chaosagent.env.world import Environment


@dataclass
class InjectionContext:
    """Everything a fault needs beyond the call itself."""

    params: dict[str, Any] = field(default_factory=dict)
    #: Environment snapshot taken immediately before the most recent write.
    #: `stale` replays a read against it to synthesise a pre-mutation view.
    pre_write_snapshot: bytes | None = None
    #: The genuine result, present only for `corrupt`-mode faults, which need
    #: something real to corrupt.
    executed_result: ToolResult | None = None


class Fault(Protocol):
    name: str

    def mode(self, params: dict[str, Any], spec: ToolSpec) -> str:
        """One of suppress | block | corrupt | delay.

        Declared up front because the injector must know whether to execute the
        call before asking for the outcome. It takes the tool spec because the
        answer can depend on it: a post-commit timeout suppresses a *write*, but
        there is nothing to commit on a read, so the same fault blocks instead.
        """
        ...

    def applies_to(self, tool: str, spec: ToolSpec) -> bool: ...

    def apply(
        self, call: Any, env: Environment, rng: Random, ctx: InjectionContext
    ) -> FaultOutcome: ...


# ---------------------------------------------------------------------------
# Shared envelope builders
# ---------------------------------------------------------------------------


def _timeout_envelope(tool: str, spec: ToolSpec, ms: int) -> ErrorEnvelope:
    return ErrorEnvelope(
        code="TIMEOUT",
        message=f"Upstream request to '{tool}' timed out after {ms}ms.",
        retryable=True,
        # True for every write, regardless of whether this particular timeout
        # happened before or after the commit. The agent cannot tell, and
        # neither can the envelope.
        state_may_have_changed=spec.kind == "write",
        idempotency_supported=spec.accepts_idempotency_key,
        hint=(
            "The outcome of this request is unknown. Read the affected entity to "
            "establish its current state before deciding what to do next."
            if spec.kind == "write"
            else "The read did not complete. Retrying is safe."
        ),
    )


def _dropped_response_envelope(tool: str, spec: ToolSpec) -> ErrorEnvelope:
    return ErrorEnvelope(
        code="UNAVAILABLE",
        message=f"Connection reset before the response from '{tool}' was received.",
        retryable=True,
        state_may_have_changed=spec.kind == "write",
        idempotency_supported=spec.accepts_idempotency_key,
        hint=(
            "The outcome of this request is unknown. Read the affected entity to "
            "establish its current state before deciding what to do next."
            if spec.kind == "write"
            else "The read did not complete. Retrying is safe."
        ),
    )


# ---------------------------------------------------------------------------
# Payload surgery helpers
# ---------------------------------------------------------------------------

#: Keys whose removal actually hurts — dropping `created_at` teaches nothing.
LOAD_BEARING_KEYS: tuple[str, ...] = (
    "total_cents",
    "net_paid_cents",
    "amount_charged_cents",
    "status",
    "order_status",
    "order_id",
    "available",
    "onhand",
    "amount_cents",
    "payment_id",
    "shipment_id",
)


def _money_keys(obj: Any) -> bool:
    """True if the payload contains a monetary field anywhere."""
    if isinstance(obj, dict):
        return any(k.endswith("_cents") for k in obj) or any(
            _money_keys(v) for v in obj.values()
        )
    if isinstance(obj, list):
        return any(_money_keys(v) for v in obj)
    return False


def _map_money(obj: Any, fn: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: (fn(v) if k.endswith("_cents") and isinstance(v, int) else _map_money(v, fn))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_map_money(v, fn) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# 1. timeout
# ---------------------------------------------------------------------------


class TimeoutFault:
    """The call raises a timeout. Whether the write committed is parameterised.

    ``committed=True`` is the nastier variant and the one that makes blind
    retry dangerous: the world moved, the agent was told nothing did.
    """

    name = "timeout"

    def mode(self, params: dict[str, Any], spec: ToolSpec) -> str:
        # A read has nothing to commit, so the post-commit variant is only
        # meaningful on writes.
        if params.get("committed", True) and spec.kind == "write":
            return "suppress"
        return "block"

    def applies_to(self, tool: str, spec: ToolSpec) -> bool:
        return True

    def apply(
        self, call: Any, env: Environment, rng: Random, ctx: InjectionContext
    ) -> FaultOutcome:
        ms = int(ctx.params.get("timeout_ms", rng.choice([5_000, 10_000, 30_000])))
        envelope = _timeout_envelope(call.tool, call.spec, ms)
        if ctx.params.get("committed", True) and call.spec.kind == "write":
            return SuppressResult(envelope, detail=f"timeout after commit ({ms}ms)")
        return BlockAndError(envelope, detail=f"timeout before commit ({ms}ms)")


# ---------------------------------------------------------------------------
# 2. malformed
# ---------------------------------------------------------------------------


class MalformedFault:
    """The response is truncated mid-JSON, or is valid JSON missing keys."""

    name = "malformed"

    def mode(self, params: dict[str, Any], spec: ToolSpec) -> str:
        return "corrupt"

    def applies_to(self, tool: str, spec: ToolSpec) -> bool:
        return True

    def apply(
        self, call: Any, env: Environment, rng: Random, ctx: InjectionContext
    ) -> FaultOutcome:
        result = ctx.executed_result
        if result is None or result.payload is None:
            return PassThrough()
        style = ctx.params.get("style") or rng.choice(["truncate", "drop_keys"])

        if style == "truncate":
            text = json.dumps(result.payload)
            cut = max(8, int(len(text) * rng.uniform(0.35, 0.75)))
            return CorruptResult(text[:cut], detail=f"truncated to {cut}/{len(text)} chars")

        payload = json.loads(json.dumps(result.payload))
        dropped = _drop_keys(payload, rng)
        if not dropped:
            return PassThrough()
        return CorruptResult(payload, detail=f"dropped keys {sorted(dropped)}")


def _drop_keys(payload: Payload, rng: Random) -> set[str]:
    """Remove one or two load-bearing keys in place. Returns what went."""
    target = payload
    if isinstance(target, list):
        if not target:
            return set()
        target = target[0]
    if not isinstance(target, dict):
        return set()
    candidates = [k for k in LOAD_BEARING_KEYS if k in target]
    if not candidates:
        candidates = [k for k in target if not isinstance(target[k], list | dict)]
    if not candidates:
        return set()
    n = min(len(candidates), rng.choice([1, 2]))
    chosen = set(rng.sample(sorted(candidates), n))
    for key in chosen:
        target.pop(key, None)
    return chosen


# ---------------------------------------------------------------------------
# 3. stale
# ---------------------------------------------------------------------------


class StaleFault:
    """A well-formed response reflecting pre-mutation state.

    Synthesised by replaying the very same read against a snapshot taken before
    the last write, so the payload is genuinely a valid earlier view rather than
    a hand-mangled one. Detectable only by cross-checking against another read
    or against an action the agent knows it took.
    """

    name = "stale"

    def mode(self, params: dict[str, Any], spec: ToolSpec) -> str:
        return "corrupt"

    def applies_to(self, tool: str, spec: ToolSpec) -> bool:
        return spec.kind == "read"

    def apply(
        self, call: Any, env: Environment, rng: Random, ctx: InjectionContext
    ) -> FaultOutcome:
        if ctx.pre_write_snapshot is None or ctx.executed_result is None:
            # Nothing has been written yet, so there is no "before" to show.
            return PassThrough()

        from chaosagent.env.world import Environment as Env

        scratch = Env(seed=0)
        scratch.restore(ctx.pre_write_snapshot)
        try:
            past = scratch.call(call.tool, call.args)
        finally:
            scratch.close()

        if not past.ok or past.payload == ctx.executed_result.payload:
            # The stale view is identical to the fresh one; injecting it would
            # be a no-op and would silently turn this run into a control.
            return PassThrough()
        return CorruptResult(past.payload, detail="replayed pre-write snapshot")


# ---------------------------------------------------------------------------
# 4. partial_write
# ---------------------------------------------------------------------------


class PartialWriteFault:
    """The write commits; the response is lost. The double-execution trap.

    Indistinguishable from a pre-commit failure without a verification read,
    which is the entire argument for idempotency keys.
    """

    name = "partial_write"

    def mode(self, params: dict[str, Any], spec: ToolSpec) -> str:
        return "suppress"

    def applies_to(self, tool: str, spec: ToolSpec) -> bool:
        return spec.kind == "write"

    def apply(
        self, call: Any, env: Environment, rng: Random, ctx: InjectionContext
    ) -> FaultOutcome:
        return SuppressResult(
            _dropped_response_envelope(call.tool, call.spec),
            detail="write committed, response dropped",
        )


# ---------------------------------------------------------------------------
# 5. wrong_type
# ---------------------------------------------------------------------------


class WrongTypeFault:
    """Schema-valid, semantically wrong: 1200 cents rendered as 12.0.

    Correct response is to sanity-check magnitudes against context — an order
    of two keyboards does not cost 258 of anything.
    """

    name = "wrong_type"

    def mode(self, params: dict[str, Any], spec: ToolSpec) -> str:
        return "corrupt"

    def applies_to(self, tool: str, spec: ToolSpec) -> bool:
        return True

    def apply(
        self, call: Any, env: Environment, rng: Random, ctx: InjectionContext
    ) -> FaultOutcome:
        result = ctx.executed_result
        if result is None or not _money_keys(result.payload):
            return PassThrough()
        style = ctx.params.get("style") or rng.choice(["cents_to_dollars", "stringify"])
        if style == "cents_to_dollars":
            corrupted = _map_money(result.payload, lambda v: round(v / 100, 2))
            detail = "money fields divided by 100 (dollars in a cents field)"
        else:
            corrupted = _map_money(result.payload, str)
            detail = "money fields stringified"
        if corrupted == result.payload:
            return PassThrough()
        return CorruptResult(corrupted, detail=detail)


# ---------------------------------------------------------------------------
# 6. silent_empty
# ---------------------------------------------------------------------------


class SilentEmptyFault:
    """Returns [] or null where an error was warranted.

    The correct response is to distinguish "no results" from "lookup failed" —
    which, given this response, requires a second read.
    """

    name = "silent_empty"

    def mode(self, params: dict[str, Any], spec: ToolSpec) -> str:
        return "corrupt"

    def applies_to(self, tool: str, spec: ToolSpec) -> bool:
        return spec.kind == "read"

    def apply(
        self, call: Any, env: Environment, rng: Random, ctx: InjectionContext
    ) -> FaultOutcome:
        result = ctx.executed_result
        if result is None or result.payload in (None, [], {}):
            return PassThrough()
        if isinstance(result.payload, list):
            return CorruptResult([], detail="list emptied")
        return CorruptResult(None, detail="object nulled")


# ---------------------------------------------------------------------------
# 7. rate_limit
# ---------------------------------------------------------------------------


class RateLimitFault:
    """429 with a retry_after. Correct response is to back off, not hammer."""

    name = "rate_limit"

    def mode(self, params: dict[str, Any], spec: ToolSpec) -> str:
        return "delay"

    def applies_to(self, tool: str, spec: ToolSpec) -> bool:
        return True

    def apply(
        self, call: Any, env: Environment, rng: Random, ctx: InjectionContext
    ) -> FaultOutcome:
        n = int(ctx.params.get("times", rng.choice([1, 2])))
        retry_after = int(ctx.params.get("retry_after", rng.choice([1, 2, 5])))
        return DelayThenSucceed(
            remaining=n,
            error=ErrorEnvelope(
                code="RATE_LIMIT",
                message=f"Rate limit exceeded for '{call.tool}'. Retry after {retry_after}s.",
                retryable=True,
                state_may_have_changed=False,
                idempotency_supported=call.spec.accepts_idempotency_key,
                hint=f"Wait {retry_after}s before retrying this call.",
            ),
            detail=f"429 x{n}, retry_after={retry_after}s",
        )


# ---------------------------------------------------------------------------
# 8. flaky
# ---------------------------------------------------------------------------


class FlakyFault:
    """Fails n times, then succeeds. Bounded retry is the correct response.

    Deliberately sets ``state_may_have_changed=False``: this is the one
    transport failure where blind retry is genuinely safe, and a config that
    treats every error as dangerous should pay for it here.
    """

    name = "flaky"

    def mode(self, params: dict[str, Any], spec: ToolSpec) -> str:
        return "delay"

    def applies_to(self, tool: str, spec: ToolSpec) -> bool:
        return True

    def apply(
        self, call: Any, env: Environment, rng: Random, ctx: InjectionContext
    ) -> FaultOutcome:
        n = int(ctx.params.get("times", rng.choice([1, 2, 3])))
        return DelayThenSucceed(
            remaining=n,
            error=ErrorEnvelope(
                code="UNAVAILABLE",
                message=f"'{call.tool}' is temporarily unavailable (upstream 503).",
                retryable=True,
                state_may_have_changed=False,
                idempotency_supported=call.spec.accepts_idempotency_key,
                hint="Transient upstream failure. Retry with backoff.",
            ),
            detail=f"503 x{n}",
        )


# ---------------------------------------------------------------------------

REGISTRY: dict[str, Fault] = {
    f.name: f  # type: ignore[misc]
    for f in (
        TimeoutFault(),
        MalformedFault(),
        StaleFault(),
        PartialWriteFault(),
        WrongTypeFault(),
        SilentEmptyFault(),
        RateLimitFault(),
        FlakyFault(),
    )
}


def get_fault(name: str) -> Fault:
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown fault class '{name}'; known: {sorted(REGISTRY)}"
        ) from None


__all__ = [
    "REGISTRY",
    "Fault",
    "FlakyFault",
    "InjectionContext",
    "MalformedFault",
    "PartialWriteFault",
    "RateLimitFault",
    "SilentEmptyFault",
    "StaleFault",
    "TimeoutFault",
    "WrongTypeFault",
    "get_fault",
]
