"""Task generation.

Templates are expanded programmatically over their parameter lists — there is
no hand-authored task anywhere in the suite beyond the eight templates
themselves. ``min_calls`` is measured by running the reference solver during
generation, so the efficiency denominator can never drift from the solution.
"""

from __future__ import annotations

from chaosagent.env import Environment
from chaosagent.tasks.templates import ALL_TEMPLATES
from chaosagent.tasks.types import Task, TaskSet, Template

#: Bump when a template, parameter list or assertion changes. The content hash
#: is what actually identifies a task set; this is for humans.
TASK_SET_VERSION = "v1"


def _expand(template: Template) -> list[Task]:
    from chaosagent.tasks.solver import run_plan

    tasks: list[Task] = []
    for index, params in enumerate(template.params, start=1):
        built = template.build(params)
        # Measure the optimal length by actually walking it.
        env = Environment(seed=0, init_state=built.init_state)
        min_calls = run_plan(env, template, params)
        tasks.append(
            Task(
                id=f"{template.name}_{index:02d}",
                template=template.name,
                prompt=built.prompt,
                init_state=built.init_state,
                expected_terminal=built.expected_terminal,
                min_calls=min_calls,
                involves_non_idempotent=template.involves_non_idempotent,
                params=params,
            )
        )
    return tasks


class TaskGenerator:
    """Expands templates into a versioned, content-hashed task set."""

    def generate(
        self,
        templates: list[Template] | None = None,
        version: str = TASK_SET_VERSION,
    ) -> TaskSet:
        templates = templates if templates is not None else ALL_TEMPLATES
        tasks: list[Task] = []
        for template in templates:
            tasks.extend(_expand(template))
        return TaskSet(version=version, tasks=tasks)


_CACHE: TaskSet | None = None


def default_task_set() -> TaskSet:
    """The canonical 50-task suite. Generated once per process."""
    global _CACHE
    if _CACHE is None:
        _CACHE = TaskGenerator().generate()
    return _CACHE


def select(task_set: TaskSet, spec: str) -> list[Task]:
    """Resolve a task selector.

    ``all`` · ``template:place_and_charge`` · ``ord_id,ord_id`` ·
    ``sample:12`` (a deterministic stratified sample — one task per template,
    round-robin, so a reduced grid still covers every template).
    """
    if spec in ("all", "*"):
        return list(task_set.tasks)
    if spec.startswith("template:"):
        name = spec.split(":", 1)[1]
        return [t for t in task_set.tasks if t.template == name]
    if spec.startswith("sample:"):
        n = int(spec.split(":", 1)[1])
        by_template: dict[str, list[Task]] = {}
        for t in task_set.tasks:
            by_template.setdefault(t.template, []).append(t)
        out: list[Task] = []
        depth = 0
        while len(out) < n:
            added = False
            for name in sorted(by_template):
                bucket = by_template[name]
                if depth < len(bucket) and len(out) < n:
                    out.append(bucket[depth])
                    added = True
            if not added:
                break
            depth += 1
        return out
    ids = [s.strip() for s in spec.split(",") if s.strip()]
    return [task_set.by_id(i) for i in ids]


__all__ = ["TASK_SET_VERSION", "TaskGenerator", "default_task_set", "select"]
