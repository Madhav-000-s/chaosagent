"""Experiment orchestration: yaml → run matrix → bounded-concurrency sweep.

Three properties the sweep must have, and how each is achieved:

* **Resumable.** Run ids are a deterministic hash of the cell coordinates, so a
  restarted sweep skips what the trace store already contains. Killing a
  3000-run sweep at 80% costs nothing.
* **Cost-bounded.** Cumulative USD is checked against a ceiling declared in the
  yaml, and the *sweep* aborts when it is hit — not the process. Completed runs
  stay in the store and the partial grid is analysable.
* **Isolated.** Every run builds its own environment from the task's init state,
  so runs never see each other and can be executed in any order.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from chaosagent.agents.configs import get_config
from chaosagent.faults.types import CONTROL, FaultSpec
from chaosagent.runtime.cache import ResponseCache
from chaosagent.runtime.llm import LLMClient
from chaosagent.runtime.pricing import HAIKU
from chaosagent.runtime.runner import execute_run, make_run_id
from chaosagent.runtime.trace import TraceWriter
from chaosagent.runtime.types import Budget
from chaosagent.tasks import default_task_set, select
from chaosagent.tasks.types import Task


class BudgetExceeded(RuntimeError):
    """The sweep's cumulative USD ceiling was reached."""


@dataclass(frozen=True)
class Cell:
    """One point in the experiment grid."""

    task: Task
    config: str
    model: str
    seed: int
    fault: FaultSpec | None

    def run_id(self, experiment: str) -> str:
        return make_run_id(experiment, self.task.id, self.config, self.model, self.seed, self.fault)


@dataclass
class Experiment:
    name: str
    configs: list[str]
    faults: list[str]
    tasks: str
    seeds: list[int]
    models: list[str]
    schedule: str = "random"
    rates: list[float] = field(default_factory=list)
    positions: list[str] = field(default_factory=list)
    budget: Budget = field(default_factory=Budget)
    max_usd_total: float = 2.0
    concurrency: int = 4
    fault_params: dict[str, Any] = field(default_factory=dict)
    #: A literal FaultSpec.target such as ``tool:charge_payment`` or
    #: ``call_index:3``, used by ad-hoc runs to aim a fault precisely.
    explicit_target: str | None = None

    @classmethod
    def from_yaml(cls, path: Path | str) -> Experiment:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        budget_raw = raw.get("budget", {}) or {}
        model = raw.get("model")
        models = raw.get("models") or ([model] if model else [HAIKU])
        return cls(
            name=raw["name"],
            configs=list(raw["configs"]),
            faults=list(raw.get("faults", [CONTROL])),
            tasks=str(raw.get("tasks", "all")),
            seeds=list(raw.get("seeds", [1])),
            models=list(models),
            schedule=str(raw.get("schedule", "random")),
            rates=list(raw.get("rates", [])),
            positions=list(raw.get("positions", [])),
            budget=Budget(**budget_raw),
            max_usd_total=float(raw.get("max_usd_total", 2.0)),
            concurrency=int(raw.get("concurrency", 4)),
            fault_params=dict(raw.get("fault_params", {})),
            explicit_target=raw.get("target"),
        )

    # -- the grid -----------------------------------------------------------

    def _targets(self, fault_class: str) -> list[FaultSpec]:
        """Expand one fault class into the specs this experiment wants."""
        if fault_class in (CONTROL, "none"):
            return [FaultSpec(fault_class=CONTROL)]
        params = dict(self.fault_params.get(fault_class, {}))
        if self.explicit_target:
            return [
                FaultSpec(fault_class=fault_class, target=self.explicit_target, params=params)
            ]
        if self.schedule == "stochastic":
            return [
                FaultSpec(fault_class=fault_class, target="stochastic", rate=r, params=params)
                for r in (self.rates or [0.15])
            ]
        if self.schedule == "position":
            return [
                FaultSpec(fault_class=fault_class, target=f"position:{p}", params=params)
                for p in (self.positions or ["early", "mid", "late"])
            ]
        return [FaultSpec(fault_class=fault_class, target="random", params=params)]

    def cells(self) -> list[Cell]:
        tasks = select(default_task_set(), self.tasks)
        out: list[Cell] = []
        for model in self.models:
            for config in self.configs:
                for fault_class in self.faults:
                    for spec in self._targets(fault_class):
                        for task in tasks:
                            for seed in self.seeds:
                                out.append(Cell(task, config, model, seed, spec))
        return out


@dataclass
class SweepReport:
    experiment: str
    planned: int = 0
    skipped: int = 0
    executed: int = 0
    failed: int = 0
    usd: float = 0.0
    aborted_on_budget: bool = False

    def summary(self) -> str:
        parts = [
            f"{self.executed} run(s) executed",
            f"{self.skipped} skipped (already recorded)",
            f"${self.usd:.4f} spent",
        ]
        if self.failed:
            parts.append(f"[red]{self.failed} errored[/red]")
        if self.aborted_on_budget:
            parts.append("[yellow]stopped on the cost ceiling[/yellow]")
        return " · ".join(parts)


async def run_experiment(
    experiment: Experiment,
    *,
    trace: TraceWriter,
    cache: ResponseCache,
    offline: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
    progress: Any = None,
) -> SweepReport:
    """Execute a whole experiment with bounded concurrency and resume."""
    report = SweepReport(experiment=experiment.name)
    cells = experiment.cells()
    if limit is not None:
        cells = cells[:limit]
    report.planned = len(cells)

    done = trace.completed_run_ids(experiment.name)
    pending = [c for c in cells if c.run_id(experiment.name) not in done]
    report.skipped = len(cells) - len(pending)
    report.usd = trace.spent_usd(experiment.name)

    if dry_run:
        return report

    clients = {
        model: LLMClient(model, cache=cache, offline=offline) for model in experiment.models
    }
    semaphore = asyncio.Semaphore(experiment.concurrency)
    # DuckDB connections are not safe to share across concurrent writers, so all
    # writes funnel through one lock while the LLM calls stay parallel.
    write_lock = asyncio.Lock()
    stop = asyncio.Event()

    async def one(cell: Cell) -> None:
        if stop.is_set():
            return
        async with semaphore:
            if stop.is_set():
                return
            try:
                result, outcome, calls = await execute_run(
                    experiment=experiment.name,
                    task=cell.task,
                    config=get_config(cell.config),
                    llm=clients[cell.model],
                    seed=cell.seed,
                    fault_spec=cell.fault,
                    budget=experiment.budget,
                )
            except Exception as exc:  # noqa: BLE001 - one bad cell must not kill the sweep
                report.failed += 1
                if progress is not None:
                    progress(f"[red]error[/red] {cell.task.id}/{cell.config}: {exc}")
                return

            async with write_lock:
                trace.write_run(
                    result,
                    template=cell.task.template,
                    claim_explicit=outcome.claim_explicit,
                    calls=calls,
                    llm_calls=[r.model_dump() for r in outcome.llm_calls],
                )
                report.executed += 1
                report.usd += result.usd
                if result.error:
                    report.failed += 1
                if progress is not None:
                    progress(
                        f"{cell.task.id} · {cell.config} · "
                        f"{cell.fault.fault_class if cell.fault else 'none'} → "
                        f"{'claimed ok' if result.agent_claimed_success else 'claimed fail'}, "
                        f"state {'correct' if result.state_correct else 'WRONG'}"
                    )
                if report.usd >= experiment.max_usd_total:
                    report.aborted_on_budget = True
                    stop.set()

    await asyncio.gather(*(one(cell) for cell in pending))
    return report


__all__ = [
    "BudgetExceeded",
    "Cell",
    "Experiment",
    "SweepReport",
    "run_experiment",
]
