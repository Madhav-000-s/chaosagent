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
