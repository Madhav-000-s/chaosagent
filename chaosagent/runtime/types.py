"""Runtime types: budgets, completions, run results."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from chaosagent.faults.types import FaultSpec

StopReason = Literal["done", "budget", "max_calls", "unrecoverable", "error"]


class Budget(BaseModel):
    """Per-run limits. Exhaustion ends a run cleanly; it never raises.

    A configuration that "recovers" by burning ten times the budget has not
    recovered, so budget-terminated runs are reported as their own category
    rather than folded into failures.
    """

    model_config = ConfigDict(frozen=True)

    max_calls: int = 30
    max_tokens: int = 40_000
    max_usd: float = 0.25
    #: Hard ceiling on LLM turns, independent of tool calls. Guards against a
    #: model that talks without ever acting.
    max_steps: int = 24


class ToolCall(BaseModel):
    """A tool invocation requested by the model."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class Completion(BaseModel):
    """One LLM turn."""

    model_config = ConfigDict(frozen=True)

    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: str = "end_turn"
    tokens_in: int = 0
    tokens_out: int = 0
    cache_hit: bool = False
    prompt_hash: str = ""
    latency_ms: int = 0
    #: Populated when the API declined the request outright.
    refusal: str | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMCallRecord(BaseModel):
    """One row of the ``llm_calls`` trace table."""

    model_config = ConfigDict(frozen=True)

    step: int
    cache_hit: bool
    prompt_hash: str
    tokens_in: int
    tokens_out: int
    latency_ms: int


class RunResult(BaseModel):
    """Everything one run produces. Facts only — no judgements.

    The runner records what the agent claimed and what the world ended up as;
    whether that counts as success is decided later by the scorer, from the
    trace. A metric bug therefore costs a re-score, not a re-run.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    experiment: str
    task_id: str
    config: str
    model: str
    seed: int

    fault_spec: FaultSpec | None = None
    #: True when a fault actually found an eligible call. A single-fault run
    #: where nothing landed is not a faulted run and must not be pooled with
    #: those that were.
    fault_landed: bool = False
    fault_position: int | None = None

    agent_claimed_success: bool = False
    agent_final_message: str = ""

    trajectory_len: int = 0
    optimal_len: int = 0

    #: Assertion failures against the task's expected terminal state. Empty
    #: means the world ended up correct.
    assertion_failures: list[str] = Field(default_factory=list)
    terminal_state: dict[str, Any] = Field(default_factory=dict)
    #: Violations introduced during this run, excluding any present at init.
    invariant_violations: list[str] = Field(default_factory=list)

    stop_reason: StopReason = "done"
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    wall_ms: int = 0
    cache_hits: int = 0
    llm_calls: int = 0
    error: str | None = None

    @property
    def state_correct(self) -> bool:
        """Derived, not decided: a pure function of the recorded failures."""
        return not self.assertion_failures

    @property
    def silent_corruption(self) -> bool:
        """The headline event: claimed success, wrong world."""
        return self.agent_claimed_success and not self.state_correct


__all__ = [
    "Budget",
    "Completion",
    "LLMCallRecord",
    "RunResult",
    "StopReason",
    "ToolCall",
]
