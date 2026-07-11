"""Task and template types."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict

from chaosagent.tasks.assertions import StateAssertion
from chaosagent.types import InitState, ToolResult


@dataclass(frozen=True)
class Step:
    """One step of a reference solution.

    ``expect_ok=False`` marks a call that is *supposed* to be refused — the
    out-of-stock template reserves stock it knows is unavailable, because
    discovering that refusal is part of the correct behaviour.
    """

    tool: str
    args: dict[str, Any]
    expect_ok: bool = True


#: A reference solution is a generator: it yields the next Step and receives the
#: ToolResult back, so later steps can depend on ids and totals the world
#: generated. Scripted, no LLM, no branching on anything the harness injected.
Plan = Generator[Step, ToolResult, None]


class Task(BaseModel):
    """One unit of work, unambiguous by construction."""

    model_config = ConfigDict(frozen=True)

    id: str
    template: str
    prompt: str
    init_state: InitState
    expected_terminal: StateAssertion
    #: Optimal solution length, measured by running the reference solver.
    min_calls: int
    involves_non_idempotent: bool
    params: dict[str, Any] = {}

    def content_hash(self) -> str:
        blob = json.dumps(
            {
                "id": self.id,
                "prompt": self.prompt,
                "init_state": self.init_state.model_dump(),
                "expected": self.expected_terminal.model_dump(),
                "min_calls": self.min_calls,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Template:
    """A parameterised family of tasks.

    ``build`` produces everything except ``min_calls``, which the generator
    fills in from the reference solver — the optimal call count is measured,
    never asserted by hand.
    """

    name: str
    description: str
    involves_non_idempotent: bool
    params: list[dict[str, Any]]
    build: Callable[[dict[str, Any]], BuiltTask]
    plan: Callable[[Any, dict[str, Any]], Plan]


@dataclass(frozen=True)
class BuiltTask:
    prompt: str
    init_state: InitState
    expected_terminal: StateAssertion


@dataclass
class SolveResult:
    """Outcome of reference-solving one task under zero faults."""

    task_id: str
    solved: bool
    calls: int
    failures: list[str] = field(default_factory=list)
    invariant_violations: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.solved and not self.failures and not self.invariant_violations


class TaskSet(BaseModel):
    """A versioned, content-hashed collection of tasks."""

    model_config = ConfigDict(frozen=True)

    version: str
    tasks: list[Task]

    def content_hash(self) -> str:
        joined = "|".join(t.content_hash() for t in self.tasks)
        return hashlib.sha256(joined.encode()).hexdigest()[:16]

    def by_id(self, task_id: str) -> Task:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise KeyError(f"unknown task '{task_id}'")

    def __len__(self) -> int:
        return len(self.tasks)

    def __iter__(self):  # type: ignore[override]
        return iter(self.tasks)


__all__ = [
    "BuiltTask",
    "Plan",
    "SolveResult",
    "Step",
    "Task",
    "TaskSet",
    "Template",
]
