"""``chaosagent`` command line interface.

Subcommands are added as the layers below them land:

* ``tools``    — inspect the tool surface (this phase)
* ``validate`` — reference-solve every task (tasks phase)
* ``run`` / ``sweep`` / ``replay`` (runtime phase)
* ``report``   (metrics phase)
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from chaosagent import __version__

app = typer.Typer(
    name="chaosagent",
    help="Deterministic fault injection for LLM agent loops.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.command()
def version() -> None:
    """Print the installed version."""
    console.print(f"chaosagent {__version__}")


@app.command()
def tools(verbose: bool = typer.Option(False, "--verbose", "-v", help="Show preconditions.")) -> None:
    """List the environment's tool surface and its safety metadata."""
    from chaosagent.env import registry

    table = Table(title="chaosagent tool surface", header_style="bold")
    table.add_column("tool")
    table.add_column("kind")
    table.add_column("idempotent")
    table.add_column("idem. key")
    table.add_column("invariants touched")
    if verbose:
        table.add_column("preconditions")

    for spec in registry.specs():
        row = [
            spec.name,
            spec.kind,
            "yes" if spec.idempotent else "[red]no[/red]",
            "yes" if spec.accepts_idempotency_key else "-",
            ", ".join(spec.invariants_touched) or "-",
        ]
        if verbose:
            row.append("; ".join(spec.preconditions) or "-")
        table.add_row(*row)

    console.print(table)
    console.print(
        f"[dim]{len(registry.specs())} tools · "
        f"{sum(1 for s in registry.specs() if not s.idempotent)} non-idempotent[/dim]"
    )


@app.command()
def invariants() -> None:
    """List the state invariants checked after every run."""
    from chaosagent.env.invariants import INVARIANT_NAMES, INVARIANTS

    table = Table(title="state invariants", header_style="bold")
    table.add_column("name")
    table.add_column("assertion")
    for name in INVARIANT_NAMES:
        doc = (INVARIANTS[name].__doc__ or "").strip().split("\n")[0]
        table.add_row(name, doc)
    console.print(table)


@app.command()
def faults() -> None:
    """List the fault classes and the outcome each produces."""
    from chaosagent.env import registry
    from chaosagent.faults import FAULT_CLASSES, get_fault

    write_spec = registry.spec_for("charge_payment")
    read_spec = registry.spec_for("get_order")

    table = Table(title="fault taxonomy", header_style="bold")
    table.add_column("class")
    table.add_column("applies to")
    table.add_column("world executes")
    table.add_column("agent sees")
    for name in FAULT_CLASSES:
        fault = get_fault(name)
        kinds = [
            k
            for k, spec in (("read", read_spec), ("write", write_spec))
            if fault.applies_to(spec.name, spec)
        ]
        spec = write_spec if "write" in kinds else read_spec
        mode = fault.mode({}, spec)
        executes, sees = {
            "suppress": ("[red]yes[/red]", "an error"),
            "block": ("no", "an error"),
            "corrupt": ("[red]yes[/red]", "a bad payload"),
            "delay": ("no", "n errors, then success"),
        }[mode]
        table.add_row(name, "/".join(kinds), executes, sees)
    console.print(table)
    console.print(
        "[dim]'world executes' + 'agent sees an error' is the silent-corruption trap.[/dim]"
    )


@app.command()
def validate(
    tasks: str = typer.Option("all", "--tasks", help="all | template:NAME | id,id | sample:N"),
    show_failures: bool = typer.Option(True, help="Print the assertion failures for bad tasks."),
) -> None:
    """Reference-solve every task under zero faults. The gate before agent work.

    Exits non-zero if any task is unsolvable, any assertion fails, or the
    optimal solution introduces an invariant violation.
    """
    from chaosagent.tasks import ReferenceSolver, default_task_set, select

    task_set = default_task_set()
    selected = select(task_set, tasks)
    results = ReferenceSolver().solve_all(selected)

    bad = [r for r in results if not r.ok]
    table = Table(title=f"reference solver · task set {task_set.version}", header_style="bold")
    table.add_column("task")
    table.add_column("calls", justify="right")
    table.add_column("result")
    for result in results:
        if result.ok:
            table.add_row(result.task_id, str(result.calls), "[green]solved[/green]")
        else:
            detail = result.error or "; ".join(result.failures + result.invariant_violations)
            table.add_row(result.task_id, str(result.calls), f"[red]{detail}[/red]")

    if bad or show_failures:
        console.print(table)

    lengths = [r.calls for r in results if r.ok]
    console.print(
        f"[bold]{len(results) - len(bad)}/{len(results)}[/bold] solved · "
        f"optimal length {min(lengths) if lengths else 0}–{max(lengths) if lengths else 0} calls · "
        f"task set hash [cyan]{task_set.content_hash()}[/cyan]"
    )
    if bad:
        console.print(f"[red]GATE FAILED: {len(bad)} task(s) malformed.[/red]")
        raise typer.Exit(code=1)
    console.print("[green]GATE PASSED[/green]")


@app.command("tasks")
def tasks_cmd(
    task_id: str = typer.Option(None, "--id", help="Print one task's prompt in full."),
) -> None:
    """Summarise the task suite, or show one task."""
    from chaosagent.tasks import default_task_set

    task_set = default_task_set()
    if task_id:
        task = task_set.by_id(task_id)
        console.print(f"[bold]{task.id}[/bold]  (template {task.template})")
        console.print(f"[dim]optimal {task.min_calls} calls · hash {task.content_hash()}[/dim]\n")
        console.print(task.prompt)
        console.print("\n[bold]expected terminal state[/bold]")
        for check in task.expected_terminal.checks:
            console.print(f"  · {check.model_dump(exclude_none=True)}")
        return

    table = Table(title=f"task suite {task_set.version}", header_style="bold")
    table.add_column("template")
    table.add_column("tasks", justify="right")
    table.add_column("optimal calls")
    table.add_column("non-idempotent")
    by_template: dict[str, list] = {}
    for task in task_set.tasks:
        by_template.setdefault(task.template, []).append(task)
    for name, group in by_template.items():
        lengths = sorted({t.min_calls for t in group})
        table.add_row(
            name,
            str(len(group)),
            ", ".join(str(n) for n in lengths),
            "yes" if group[0].involves_non_idempotent else "no",
        )
    console.print(table)
    console.print(
        f"[dim]{len(task_set)} tasks · hash {task_set.content_hash()}[/dim]"
    )


@app.command()
def configs() -> None:
    """Show the agent configurations and the decomposition table."""
    from chaosagent.agents.configs import CONFIGS

    table = Table(title="agent configurations", header_style="bold")
    table.add_column("config")
    table.add_column("error format")
    table.add_column("retry")
    table.add_column("reflect")
    table.add_column("contract")
    table.add_column("verify read")
    table.add_column("idem. key")
    for config in CONFIGS.values():
        policy = config.recovery_policy
        tick = "[green]yes[/green]"
        table.add_row(
            config.name,
            config.error_formatter.name,
            str(policy.blind_retry) if policy.blind_retry else "-",
            tick if policy.reflect else "-",
            tick if policy.contract_aware else "-",
            tick if policy.verify_read else "-",
            tick if config.call_decorator.name != "identity" else "-",
        )
    console.print(table)
    console.print("[dim]All eight share one ReAct loop; only these objects differ.[/dim]")


def _load_env_file() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover
        pass


@app.command()
def run(
    config: str = typer.Option("naive", help="Agent configuration."),
    fault: str = typer.Option("none", help="Fault class, or 'none' for the control arm."),
    tasks: str = typer.Option("sample:1", help="all | template:NAME | id,id | sample:N"),
    seeds: int = typer.Option(1, help="Number of seeds, starting at 1."),
    model: str = typer.Option("claude-haiku-4-5", help="Model id."),
    target: str = typer.Option("random", help="random | call_index:N | tool:NAME | position:early"),
    experiment: str = typer.Option("adhoc", help="Experiment label recorded in the trace."),
    offline: bool = typer.Option(False, help="Cache only; never call the API."),
    db: str = typer.Option(None, help="Trace database path."),
) -> None:
    """Run one cell of the grid, or a small set of them."""
    import asyncio

    from chaosagent.runtime.cache import ResponseCache
    from chaosagent.runtime.orchestrator import Experiment, run_experiment
    from chaosagent.runtime.trace import DEFAULT_DB, TraceWriter
    from chaosagent.runtime.types import Budget

    _load_env_file()
    exp = Experiment(
        name=experiment,
        configs=[config],
        faults=[fault],
        tasks=tasks,
        seeds=list(range(1, seeds + 1)),
        models=[model],
        budget=Budget(),
        max_usd_total=1.0,
        # `random` is the default schedule; anything else is aimed explicitly.
        explicit_target=None if target == "random" else target,
    )

    with TraceWriter(db or DEFAULT_DB) as trace:
        report = asyncio.run(
            run_experiment(
                exp,
                trace=trace,
                cache=ResponseCache(),
                offline=offline,
                progress=lambda line: console.print(f"  {line}"),
            )
        )
    console.print(report.summary())


@app.command()
def sweep(
    experiment: str = typer.Argument(..., help="Path to an experiment yaml, or its name."),
    offline: bool = typer.Option(False, help="Cache only; never call the API."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan and exit."),
    limit: int = typer.Option(None, help="Cap the number of runs (smoke testing)."),
    db: str = typer.Option(None, help="Trace database path."),
) -> None:
    """Run a full experiment. Resumable, cost-bounded, safe to interrupt."""
    import asyncio
    from pathlib import Path

    from chaosagent.runtime.cache import ResponseCache
    from chaosagent.runtime.orchestrator import Experiment, run_experiment
    from chaosagent.runtime.trace import DEFAULT_DB, TraceWriter

    _load_env_file()
    path = Path(experiment)
    if not path.exists():
        candidates = sorted(Path("experiments").glob(f"{experiment}*.yaml"))
        if not candidates:
            console.print(f"[red]no experiment matching '{experiment}'[/red]")
            raise typer.Exit(code=1)
        path = candidates[0]

    exp = Experiment.from_yaml(path)
    console.print(
        f"[bold]{exp.name}[/bold] · {len(exp.cells())} cells · "
        f"models {', '.join(exp.models)} · ceiling ${exp.max_usd_total:.2f}"
    )

    with TraceWriter(db or DEFAULT_DB) as trace:
        report = asyncio.run(
            run_experiment(
                exp,
                trace=trace,
                cache=ResponseCache(),
                offline=offline,
                dry_run=dry_run,
                limit=limit,
                progress=None if dry_run else (lambda line: console.print(f"  {line}")),
            )
        )
    if dry_run:
        console.print(
            f"[dim]planned {report.planned} · {report.skipped} already recorded · "
            f"${report.usd:.4f} already spent[/dim]"
        )
        return
    console.print(report.summary())


@app.command()
def replay(
    run_id: str = typer.Argument(..., help="Run id to inspect."),
    db: str = typer.Option(None, help="Trace database path."),
) -> None:
    """Show a recorded run: what executed, what the agent saw, how it ended."""
    from chaosagent.runtime.trace import DEFAULT_DB, connect

    conn = connect(db or DEFAULT_DB, read_only=True)
    row = conn.execute(
        "SELECT task_id, config, model, fault_class, claimed_success, state_correct, "
        "stop_reason, invariants_broken, assertion_failures, usd, trajectory_len, optimal_len "
        "FROM runs WHERE run_id = ?",
        [run_id],
    ).fetchone()
    if row is None:
        console.print(f"[red]no run '{run_id}'[/red]")
        raise typer.Exit(code=1)

    (task, cfg, model, fault, claimed, correct, stop, broken, failures, usd, tlen, olen) = row
    verdict = (
        "[red]SILENT CORRUPTION[/red]"
        if claimed and not correct
        else ("[green]recovered[/green]" if correct else "reported failure")
    )
    console.print(
        f"[bold]{run_id}[/bold]  {task} · {cfg} · {model} · fault={fault or 'none'}\n"
        f"claimed_success={claimed}  state_correct={correct}  → {verdict}\n"
        f"stop={stop}  invariants_broken={broken}  calls={tlen}/{olen} optimal  ${usd:.4f}"
    )
    if failures and failures != "[]":
        console.print(f"[yellow]assertion failures:[/yellow] {failures}")

    table = Table(title="tool calls", header_style="bold")
    for col in ("#", "tool", "fault", "world executed", "agent saw error"):
        table.add_column(col)
    for c in conn.execute(
        "SELECT call_index, tool, fault_applied, env_executed, agent_saw_error "
        "FROM calls WHERE run_id = ? ORDER BY call_index",
        [run_id],
    ).fetchall():
        trap = c[3] and c[4]
        table.add_row(
            str(c[0]),
            c[1],
            c[2] or "-",
            ("[red]yes[/red]" if trap else "yes") if c[3] else "no",
            ("[red]yes[/red]" if trap else "yes") if c[4] else "no",
        )
    console.print(table)
    console.print("[dim]red = world executed AND agent saw an error: the trap.[/dim]")
    conn.close()


@app.command()
def report(
    experiment: str = typer.Option(None, "--experiment", "-e", help="Experiment name."),
    out: str = typer.Option("results", "--out", help="Output directory."),
    db: str = typer.Option(None, help="Trace database path."),
) -> None:
    """Regenerate every table and figure from the trace store.

    Takes no API key: reporting reads the DuckDB file and nothing else.
    """
    from pathlib import Path

    from chaosagent.metrics.report import write_report
    from chaosagent.metrics.scorer import open_suite
    from chaosagent.runtime.trace import DEFAULT_DB

    suite, conn = open_suite(db or DEFAULT_DB)
    try:
        names = [experiment] if experiment else suite.experiments()
        if not names:
            console.print("[yellow]no runs recorded yet — run `chaosagent sweep` first[/yellow]")
            raise typer.Exit(code=1)

        for name in names:
            path, figures = write_report(suite, name, Path(out))
            console.print(f"[green]wrote[/green] {path}  ({suite.run_count(name)} runs)")
            for figure in figures:
                console.print(f"       {figure}")

            scr = suite.query("silent_corruption", name)
            if not scr.empty:
                table = Table(title=f"{name} · silent corruption rate", header_style="bold")
                table.add_column("config")
                for fault in sorted(scr["fault_class"].unique()):
                    table.add_column(fault, justify="right")
                for config in sorted(scr["config"].unique()):
                    rows = scr[scr["config"] == config].set_index("fault_class")
                    cells = []
                    for fault in sorted(scr["fault_class"].unique()):
                        if fault in rows.index:
                            value = float(rows.loc[fault, "scr"])
                            colour = "red" if value >= 0.5 else ("yellow" if value > 0 else "green")
                            cells.append(f"[{colour}]{value:.0%}[/{colour}]")
                        else:
                            cells.append("–")
                    table.add_row(config, *cells)
                console.print(table)
    finally:
        conn.close()


@app.command()
def schema(
    out: str = typer.Option(None, "--out", help="Write to this path instead of stdout."),
) -> None:
    """Emit the environment DDL. Keeps `docs/env_schema.sql` in sync."""
    from pathlib import Path

    from chaosagent.env.schema import DDL

    text = DDL.strip() + "\n"
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text, encoding="utf-8")
        console.print(f"[green]wrote[/green] {out}")
    else:
        console.print(text)


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
