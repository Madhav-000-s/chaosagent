"""The injector: middleware wrapping every tool call.

Deterministic given ``(run_seed, fault_spec)``. Sits *below* the agent's tool
interface and returns exactly the shapes the environment returns, so the agent
cannot distinguish an injected failure from a real one.

Two rules that protect the validity of every downstream number:

1. **Faults only fire on calls that would otherwise have succeeded.** If the
   real call is refused on its own merits — wrong order status, insufficient
   stock — the injector passes the refusal through untouched and stays armed
   for the next eligible call. Corrupting an already-failing call would measure
   the agent's reaction to the environment, not to the fault.
2. **A single-fault run injects exactly one fault, or none at all.** The
   schedule stays armed until an eligible call appears. A run whose fault never
   found a home is recorded as such, so the analysis can exclude it rather than
   silently counting it as a clean run in the faulted arm.
"""

from __future__ import annotations

from random import Random
from typing import Any

from chaosagent.env import registry
from chaosagent.env.world import Environment
from chaosagent.faults.classes import InjectionContext, get_fault
from chaosagent.faults.schedule import Schedule, build_schedule
from chaosagent.faults.types import (
    BlockAndError,
    CallOutcome,
    CorruptResult,
    DelayThenSucceed,
    FaultSpec,
    InjectionRecord,
    PassThrough,
    PendingCall,
    SuppressResult,
)
from chaosagent.types import ToolResult

#: Offset so the schedule's randomness and the fault's randomness never share a
#: stream — otherwise changing a fault's parameters would move the injection
#: position, and the position sweep would stop being a controlled comparison.
_FAULT_STREAM_OFFSET = 10_000_019


class FaultyEnvironment:
    """An environment-shaped object whose ``call`` may corrupt.

    Exposes the same surface the runner needs from :class:`Environment`, so the
    agent loop is written against one interface and never learns which it has.
    """

    def __init__(
        self,
        env: Environment,
        spec: FaultSpec,
        schedule: Schedule,
        fault_rng: Random,
        max_injections: int | None,
    ) -> None:
        self.env = env
        self.spec = spec
        self.schedule = schedule
        self._rng = fault_rng
        self._max_injections = max_injections
        self.history: list[CallOutcome] = []
        self._records: list[InjectionRecord] = []
        self._delays: dict[str, int] = {}
        self._delay_errors: dict[str, Any] = {}
        self._pre_write_snapshot: bytes | None = None
        # Only `stale` needs the rolling snapshot; skip the cost otherwise.
        self._track_snapshots = spec.fault_class == "stale"

    # -- the tool interface -------------------------------------------------

    def call(self, tool: str, args: dict[str, Any]) -> ToolResult:
        call_index = len(self.history)
        registered = registry.get(tool)

        if registered is None:
            # Unknown tool: nothing to corrupt, and the environment's own
            # NOT_FOUND is the honest answer.
            result = self.env.call(tool, args)
            self._record(call_index, tool, args, result, executed=False, fault=None)
            return result

        pending = PendingCall(call_index, tool, args, registered.spec)

        # An in-flight `flaky`/`rate_limit` still owes this call a failure.
        pending_delay = self._delays.get(tool, 0)
        if pending_delay > 0:
            self._delays[tool] = pending_delay - 1
            error = self._delay_errors[tool]
            result = ToolResult.failure(error)
            self._record(
                call_index,
                tool,
                args,
                result,
                executed=False,
                fault=self.spec.fault_class,
                record=InjectionRecord(
                    call_index=call_index,
                    tool=tool,
                    fault_class=self.spec.fault_class,
                    outcome="delay",
                    env_executed=False,
                    detail=f"delayed call, {pending_delay - 1} remaining",
                ),
            )
            return result

        if not self._should_attempt(pending):
            return self._plain(pending)

        return self._inject(pending)

    # -- injection ----------------------------------------------------------

    def _should_attempt(self, call: PendingCall) -> bool:
        if self.spec.is_control:
            return False
        if self._max_injections is not None and len(self._records) >= self._max_injections:
            return False
        if self.schedule.single and self._records:
            return False
        if not self.schedule.wants(call.call_index, call.tool):
            return False
        return get_fault(self.spec.fault_class).applies_to(call.tool, call.spec)

    def _inject(self, call: PendingCall) -> ToolResult:
        fault = get_fault(self.spec.fault_class)
        mode = fault.mode(self.spec.params, call.spec)
        ctx = InjectionContext(
            params=dict(self.spec.params),
            pre_write_snapshot=self._pre_write_snapshot,
        )

        if mode in ("corrupt", "suppress"):
            real = self._execute(call)
            if not real.ok:
                # A genuine refusal. Pass it through and stay armed.
                self._record(
                    call.call_index, call.tool, call.args, real, executed=False, fault=None
                )
                return real
            ctx.executed_result = real
            outcome = fault.apply(call, self.env, self._rng, ctx)
            if isinstance(outcome, PassThrough):
                self._record(
                    call.call_index, call.tool, call.args, real, executed=True, fault=None
                )
                return real
            if isinstance(outcome, CorruptResult):
                result = ToolResult.success(outcome.payload)
                self._finish(call, result, outcome, executed=True, agent_saw_error=False)
                return result
            if isinstance(outcome, SuppressResult):
                result = ToolResult.failure(outcome.error)
                self._finish(call, result, outcome, executed=True, agent_saw_error=True)
                return result
            raise AssertionError(f"{fault.name} declared mode={mode} but returned {outcome!r}")

        outcome = fault.apply(call, self.env, self._rng, ctx)

        if isinstance(outcome, PassThrough):
            return self._plain(call)

        if isinstance(outcome, BlockAndError):
            result = ToolResult.failure(outcome.error)
            self._finish(call, result, outcome, executed=False, agent_saw_error=True)
            return result

        if isinstance(outcome, DelayThenSucceed):
            # This call is the first of the n failures.
            self._delays[call.tool] = max(0, outcome.remaining - 1)
            self._delay_errors[call.tool] = outcome.error
            result = ToolResult.failure(outcome.error)
            self._finish(call, result, outcome, executed=False, agent_saw_error=True)
            return result

        raise AssertionError(f"{fault.name} declared mode={mode} but returned {outcome!r}")

    def _execute(self, call: PendingCall) -> ToolResult:
        if self._track_snapshots and call.spec.kind == "write":
            self._pre_write_snapshot = self.env.snapshot()
        return self.env.call(call.tool, call.args)

    def _plain(self, call: PendingCall) -> ToolResult:
        result = self._execute(call)
        self._record(
            call.call_index,
            call.tool,
            call.args,
            result,
            executed=result.ok,
            fault=None,
        )
        return result

    # -- bookkeeping --------------------------------------------------------

    def _finish(
        self,
        call: PendingCall,
        result: ToolResult,
        outcome: Any,
        *,
        executed: bool,
        agent_saw_error: bool,
    ) -> None:
        from chaosagent.faults.types import outcome_name

        record = InjectionRecord(
            call_index=call.call_index,
            tool=call.tool,
            fault_class=self.spec.fault_class,
            outcome=outcome_name(outcome),  # type: ignore[arg-type]
            env_executed=executed,
            detail=getattr(outcome, "detail", ""),
        )
        self._record(
            call.call_index,
            call.tool,
            call.args,
            result,
            executed=executed,
            fault=self.spec.fault_class,
            record=record,
            agent_saw_error=agent_saw_error,
        )

    def _record(
        self,
        call_index: int,
        tool: str,
        args: dict[str, Any],
        result: ToolResult,
        *,
        executed: bool,
        fault: str | None,
        record: InjectionRecord | None = None,
        agent_saw_error: bool | None = None,
    ) -> None:
        if record is not None:
            self._records.append(record)
        key = args.get("idempotency_key")
        self.history.append(
            CallOutcome(
                call_index=call_index,
                tool=tool,
                args=dict(args),
                result=result,
                env_executed=executed,
                agent_saw_error=(not result.ok) if agent_saw_error is None else agent_saw_error,
                fault_applied=fault,
                idempotency_key=key if isinstance(key, str) else None,
                injection=record,
            )
        )

    # -- observation (delegated) -------------------------------------------

    def injected(self) -> list[InjectionRecord]:
        """What was actually injected. Ground truth, never shown to the agent."""
        return list(self._records)

    @property
    def fault_landed(self) -> bool:
        return bool(self._records)

    def state(self):  # type: ignore[no-untyped-def]
        return self.env.state()

    def call_log(self):  # type: ignore[no-untyped-def]
        return self.env.call_log()

    def check_invariants(self):  # type: ignore[no-untyped-def]
        return self.env.check_invariants()

    def snapshot(self) -> bytes:
        return self.env.snapshot()

    def restore(self, snap: bytes) -> None:
        self.env.restore(snap)

    @property
    def clock(self):  # type: ignore[no-untyped-def]
        return self.env.clock

    def tool_specs(self):  # type: ignore[no-untyped-def]
        return self.env.tool_specs()


class FaultInjector:
    """Builds a :class:`FaultyEnvironment` for one run."""

    def __init__(
        self,
        spec: FaultSpec,
        seed: int,
        trajectory_hint: int = 6,
        max_injections: int | None = None,
    ) -> None:
        self.spec = spec
        self.seed = seed
        self.trajectory_hint = trajectory_hint
        self.max_injections = max_injections
        self._schedule_rng = Random(seed)
        self._fault_rng = Random(seed + _FAULT_STREAM_OFFSET)
        self.schedule = build_schedule(spec, self._schedule_rng, trajectory_hint)
        self._wrapped: FaultyEnvironment | None = None

    def wrap(self, env: Environment) -> FaultyEnvironment:
        self._wrapped = FaultyEnvironment(
            env,
            self.spec,
            self.schedule,
            self._fault_rng,
            self.max_injections,
        )
        return self._wrapped

    def injected(self) -> list[InjectionRecord]:
        return self._wrapped.injected() if self._wrapped else []

    def describe(self) -> str:
        return f"{self.spec.fault_class} · {self.schedule.describe()} · seed={self.seed}"


__all__ = ["FaultInjector", "FaultyEnvironment"]
