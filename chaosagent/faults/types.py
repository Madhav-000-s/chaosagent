"""Fault types and the four-way outcome split.

The outcome taxonomy is the whole design. It separates *did the world execute
the call* from *what did the agent see*, which is precisely the axis silent
corruption lives on:

| Outcome            | world executed | agent sees   | example fault    |
|--------------------|----------------|--------------|------------------|
| ``SuppressResult`` | yes            | an error     | ``partial_write``|
| ``BlockAndError``  | no             | an error     | ``timeout`` (pre-commit) |
| ``CorruptResult``  | yes            | a bad payload| ``stale``, ``malformed`` |
| ``DelayThenSucceed``| eventually    | n errors then success | ``flaky`` |

A run where the world executed and the agent saw an error is the trap. A run
where the agent then reports success is the corruption.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from chaosagent.types import ErrorEnvelope, Payload, ToolResult, ToolSpec

#: The eight classes, in report order. ``none`` is the control arm and is not a
#: fault class — it is the absence of one.
FAULT_CLASSES: tuple[str, ...] = (
    "timeout",
    "malformed",
    "stale",
    "partial_write",
    "wrong_type",
    "silent_empty",
    "rate_limit",
    "flaky",
)

CONTROL = "none"


class FaultSpec(BaseModel):
    """Declarative description of what to inject and where."""

    model_config = ConfigDict(frozen=True)

    fault_class: str
    #: ``random`` · ``call_index:N`` · ``tool:NAME`` · ``stochastic``
    target: str = "random"
    #: Injection probability per call, for ``stochastic`` targets only.
    rate: float = 0.0
    params: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_control(self) -> bool:
        return self.fault_class in (CONTROL, "", "control")

    def label(self) -> str:
        return f"{self.fault_class}@{self.target}"


@dataclass(frozen=True)
class PendingCall:
    """A tool call about to be executed, as the injector sees it."""

    call_index: int
    tool: str
    args: dict[str, Any]
    spec: ToolSpec

    @property
    def idempotency_key(self) -> str | None:
        key = self.args.get("idempotency_key")
        return key if isinstance(key, str) else None


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SuppressResult:
    """The world executes; the agent is told it failed.

    The double-execution trap. Nothing the agent can read from the error tells
    it the write landed — only a verification read does.
    """

    error: ErrorEnvelope
    detail: str = ""


@dataclass(frozen=True)
class BlockAndError:
    """The world does not execute; the agent is told it failed."""

    error: ErrorEnvelope
    detail: str = ""


@dataclass(frozen=True)
class CorruptResult:
    """The world executes; the agent sees a mutated payload."""

    payload: Payload
    detail: str = ""


@dataclass(frozen=True)
class DelayThenSucceed:
    """Fails ``n`` times, then passes through untouched."""

    remaining: int
    error: ErrorEnvelope
    detail: str = ""


@dataclass(frozen=True)
class PassThrough:
    """No fault applies to this call."""


FaultOutcome = (
    SuppressResult | BlockAndError | CorruptResult | DelayThenSucceed | PassThrough
)

#: Stable short names, written to the trace.
OUTCOME_NAMES: dict[type, str] = {
    SuppressResult: "suppress",
    BlockAndError: "block",
    CorruptResult: "corrupt",
    DelayThenSucceed: "delay",
    PassThrough: "pass",
}


def outcome_name(outcome: FaultOutcome) -> str:
    return OUTCOME_NAMES[type(outcome)]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class InjectionRecord(BaseModel):
    """Ground truth about what was injected. Never visible to the agent."""

    model_config = ConfigDict(frozen=True)

    call_index: int
    tool: str
    fault_class: str
    outcome: Literal["suppress", "block", "corrupt", "delay", "pass"]
    env_executed: bool
    detail: str = ""


@dataclass
class CallOutcome:
    """Everything the trace store needs about one tool call.

    ``env_executed`` and ``agent_saw_error`` are the pair that almost every
    headline metric is a query over.
    """

    call_index: int
    tool: str
    args: dict[str, Any]
    result: ToolResult
    env_executed: bool
    agent_saw_error: bool
    fault_applied: str | None = None
    idempotency_key: str | None = None
    injection: InjectionRecord | None = None
    extra: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "CONTROL",
    "FAULT_CLASSES",
    "BlockAndError",
    "CallOutcome",
    "CorruptResult",
    "DelayThenSucceed",
    "FaultOutcome",
    "FaultSpec",
    "InjectionRecord",
    "PassThrough",
    "PendingCall",
    "SuppressResult",
    "outcome_name",
]
