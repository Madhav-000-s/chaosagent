"""Scripted reference solver.

No LLM. Drives each template's hand-written optimal solution against a clean
environment and establishes three things at once:

* the task is **solvable** — if the script cannot reach the terminal state, the
  task is malformed, not hard;
* the **optimal call count**, which is the denominator for the call-overhead
  metric — measured, never asserted by hand;
* the terminal-state assertion actually **holds** on the state the optimal
  solution produces, which catches an assertion that is wrong rather than a
  solution that is.

This is the automated substitute for hand-validating a dataset. It is cheap and
it is total: ``chaosagent validate`` runs all 50 in under a second.
"""

from __future__ import annotations

from typing import Any

from chaosagent.env import Environment
from chaosagent.env.invariants import check_all, new_violations
from chaosagent.tasks.templates import ALL_TEMPLATES
from chaosagent.tasks.types import SolveResult, Task, Template

TEMPLATES_BY_NAME: dict[str, Template] = {t.name: t for t in ALL_TEMPLATES}


class PlanError(RuntimeError):
    """The scripted solution did not do what it said it would."""


def run_plan(env: Environment, template: Template, params: dict[str, Any]) -> int:
    """Execute a reference solution against ``env``. Returns the call count.

    Raises :class:`PlanError` if any step's success differs from what the plan
    declared — including steps that are *expected* to fail, such as reserving
    stock that is known to be unavailable.
    """
    generator = template.plan(env, params)
    calls = 0
    try:
        step = next(generator)
        while True:
            result = env.call(step.tool, step.args)
            calls += 1
            if result.ok != step.expect_ok:
                detail = result.error.message if result.error else "(no error)"
                raise PlanError(
                    f"step {calls} ({step.tool}) expected ok={step.expect_ok}, "
                    f"got ok={result.ok}: {detail}"
                )
            step = generator.send(result)
    except StopIteration:
        pass
    return calls


class ReferenceSolver:
    """Solves tasks with the scripted plan, under zero faults."""

    def solve(self, task: Task) -> SolveResult:
        template = TEMPLATES_BY_NAME.get(task.template)
        if template is None:
            return SolveResult(
                task_id=task.id,
                solved=False,
                calls=0,
                error=f"unknown template '{task.template}'",
            )

        env = Environment(seed=0, init_state=task.init_state)
        baseline = check_all(env)

        try:
            calls = run_plan(env, template, task.params)
        except PlanError as exc:
            return SolveResult(task_id=task.id, solved=False, calls=0, error=str(exc))

        failures = task.expected_terminal.failures(env.state())
        violations = [str(v) for v in new_violations(baseline, check_all(env))]
        return SolveResult(
            task_id=task.id,
            solved=not failures,
            calls=calls,
            failures=failures,
            invariant_violations=violations,
        )

    def solve_all(self, tasks: list[Task]) -> list[SolveResult]:
        return [self.solve(t) for t in tasks]


__all__ = ["PlanError", "ReferenceSolver", "TEMPLATES_BY_NAME", "run_plan"]
