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
