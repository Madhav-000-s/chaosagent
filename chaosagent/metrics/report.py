"""The three headline artifacts, regenerated from the trace store.

Every figure here is a pure function of the DuckDB file. Nothing is hand-drawn
and nothing is cached in an image checked in by hand — ``chaosagent report``
rebuilds all of it, which is what makes the reproducibility claim checkable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # no display in CI
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from chaosagent.agents.configs import DECOMPOSITION, HEADLINE  # noqa: E402
from chaosagent.faults.types import FAULT_CLASSES  # noqa: E402
from chaosagent.metrics.scorer import MetricSuite  # noqa: E402

#: Ordered so the figures read left-to-right from least to most guarded.
CONFIG_ORDER = list(HEADLINE)
FAULT_ORDER = list(FAULT_CLASSES)


def _ordered(values: list[str], preferred: list[str]) -> list[str]:
    known = [v for v in preferred if v in values]
    return known + sorted(v for v in values if v not in preferred)


# ---------------------------------------------------------------------------
# Artifact 1 — silent-corruption heatmap
# ---------------------------------------------------------------------------


def heatmap(suite: MetricSuite, experiment: str, out: Path) -> Path | None:
    """Config (rows) x fault class (columns), colour = silent corruption rate."""
    df = suite.query("silent_corruption", experiment)
    if df.empty:
        return None

    configs = _ordered(sorted(df["config"].unique()), CONFIG_ORDER)
    faults = _ordered(sorted(df["fault_class"].unique()), FAULT_ORDER)
    grid = np.full((len(configs), len(faults)), np.nan)
    counts = np.zeros_like(grid)
    for _, row in df.iterrows():
        i, j = configs.index(row["config"]), faults.index(row["fault_class"])
        grid[i, j] = row["scr"]
        counts[i, j] = row["n"]

    fig, ax = plt.subplots(figsize=(1.05 * len(faults) + 3.2, 0.62 * len(configs) + 2.4))
    image = ax.imshow(grid, cmap="RdYlGn_r", vmin=0.0, vmax=1.0, aspect="auto")

    ax.set_xticks(range(len(faults)), faults, rotation=40, ha="right", fontsize=9)
    ax.set_yticks(range(len(configs)), configs, fontsize=9)
    for i in range(len(configs)):
        for j in range(len(faults)):
            if np.isnan(grid[i, j]):
                ax.text(j, i, "–", ha="center", va="center", color="grey", fontsize=8)
                continue
            ax.text(
                j,
                i,
                f"{grid[i, j]:.0%}\nn={int(counts[i, j])}",
                ha="center",
                va="center",
                fontsize=7.5,
                color="black" if grid[i, j] < 0.6 else "white",
            )

    ax.set_title(
        "Silent corruption rate — agent reported success, world is wrong\n"
        f"experiment: {experiment}",
        fontsize=11,
        pad=12,
    )
    fig.colorbar(image, ax=ax, shrink=0.8, label="silent corruption rate")
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=160)
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------
# Artifact 2 — guard decomposition table
# ---------------------------------------------------------------------------

MECHANISMS = {
    "contract": ("yes", "-", "-"),
    "contract+idem": ("yes", "yes", "-"),
    "contract+verify": ("yes", "-", "yes"),
    "guarded": ("yes", "yes", "yes"),
}


def decomposition_table(suite: MetricSuite, experiment: str) -> str:
    """Which mechanism fixes which fault — the attribution table."""
    df = suite.query("silent_corruption", experiment)
    if df.empty:
        return "_No decomposition data for this experiment._\n"

    arms = [c for c in DECOMPOSITION if c in set(df["config"])]
    faults = _ordered(sorted(df["fault_class"].unique()), FAULT_ORDER)
    lookup = {(r["config"], r["fault_class"]): r["scr"] for _, r in df.iterrows()}

    header = "| arm | contract | idem. key | verify read | " + " | ".join(faults) + " |"
    rule = "|" + "---|" * (4 + len(faults))
    lines = [header, rule]
    for arm in arms:
        contract, idem, verify = MECHANISMS.get(arm, ("?", "?", "?"))
        cells = []
        for fault in faults:
            value = lookup.get((arm, fault))
            cells.append("–" if value is None else f"{value:.0%}")
        lines.append(f"| `{arm}` | {contract} | {idem} | {verify} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Artifact 3 — double-execution bar chart
# ---------------------------------------------------------------------------


def double_exec_chart(suite: MetricSuite, experiment: str, out: Path) -> Path | None:
    """One bar per config. The single most legible number in the project."""
    df = suite.query("double_execution", experiment)
    if df.empty:
        return None

    order = _ordered(sorted(df["config"].unique()), CONFIG_ORDER)
    df = df.set_index("config").reindex(order).dropna(subset=["double_exec_rate"])
    if df.empty:
        return None

    rates = df["double_exec_rate"].to_numpy(dtype=float)
    labels = list(df.index)
    colours = ["#b2182b" if r > 0 else "#1a9850" for r in rates]

    fig, ax = plt.subplots(figsize=(max(6.0, 1.15 * len(labels) + 2.0), 4.2))
    bars = ax.bar(labels, rates, color=colours)
    for bar, rate, n in zip(bars, rates, df["n"], strict=False):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 0.015,
            f"{rate:.0%}\nn={int(n)}",
            ha="center",
            fontsize=8.5,
        )

    ax.set_ylabel("runs with a non-idempotent op executed twice")
    ax.set_ylim(0, max(0.12, float(rates.max()) * 1.28))
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    ax.set_title(
        "Double-execution rate, counted from the environment's own call log\n"
        f"experiment: {experiment}",
        fontsize=11,
    )
    ax.spines[["top", "right"]].set_visible(False)
    plt.xticks(rotation=20, ha="right")
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=160)
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------
# Markdown assembly
# ---------------------------------------------------------------------------


def _frame_to_md(df: Any, floats: dict[str, str] | None = None) -> str:
    if df is None or df.empty:
        return "_no rows_\n"
    floats = floats or {}
    frame = df.copy()
    for column, spec in floats.items():
        if column in frame.columns:
            frame[column] = frame[column].map(
                lambda v, s=spec: "–" if v is None or (isinstance(v, float) and np.isnan(v))
                else format(v, s)
            )
    header = "| " + " | ".join(str(c) for c in frame.columns) + " |"
    rule = "|" + "---|" * len(frame.columns)
    rows = [
        "| " + " | ".join("" if v is None else str(v) for v in record) + " |"
        for record in frame.itertuples(index=False, name=None)
    ]
    return "\n".join([header, rule, *rows]) + "\n"


def write_report(
    suite: MetricSuite, experiment: str, out_dir: Path
) -> tuple[Path, list[Path]]:
    """Write results.md plus the figures. Returns (markdown path, figures)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    figures: list[Path] = []

    heat = heatmap(suite, experiment, out_dir / f"{experiment}_heatmap.png")
    bars = double_exec_chart(suite, experiment, out_dir / f"{experiment}_double_exec.png")
    figures += [p for p in (heat, bars) if p]

    parts: list[str] = [
        f"# Results — `{experiment}`\n",
        f"_{suite.run_count(experiment)} runs. "
        "Every number below is a query in `chaosagent/metrics/queries/`._\n",
    ]

    if heat:
        parts.append(f"\n## 1. Silent corruption\n\n![heatmap]({heat.name})\n")
    parts.append("\n" + _frame_to_md(suite.query("silent_corruption", experiment), {"scr": ".1%"}))

    parts.append("\n## 2. Guard decomposition\n\n")
    parts.append(decomposition_table(suite, experiment))

    if bars:
        parts.append(f"\n## 3. Double execution\n\n![double execution]({bars.name})\n")
    parts.append(
        "\n"
        + _frame_to_md(
            suite.query("double_execution", experiment), {"double_exec_rate": ".1%"}
        )
    )

    parts.append(
        "\n## Fault landing rate\n\n"
        "_How often an injection found an eligible call. `stale` and "
        "`silent_empty` apply only to reads, so an agent that front-loads its "
        "reads and then writes to the end of the trajectory offers them nowhere "
        "to land. Runs where nothing landed are excluded from every rate above._\n\n"
    )
    parts.append(
        _frame_to_md(
            suite.query("fault_landing", experiment),
            {"landing_rate": ".1%", "mean_position": ".2f"},
        )
    )

    parts.append("\n## Recovery, detection and honesty\n\n")
    parts.append(
        _frame_to_md(
            suite.query("recovery", experiment),
            {
                "recovery_rate": ".1%",
                "detection_rate": ".1%",
                "honest_rate": ".1%",
                "invariant_violation_rate": ".1%",
            },
        )
    )

    parts.append("\n## Efficiency — the price of the intervention\n\n")
    parts.append(
        _frame_to_md(
            suite.query("efficiency", experiment),
            {
                "call_overhead": ".2f",
                "retry_storm": ".2f",
                "tokens_per_run": ".0f",
                "usd_per_run": ".4f",
                "llm_turns": ".1f",
                "budget_exhausted_rate": ".1%",
                "error_rate": ".1%",
            },
        )
    )

    parts.append("\n## Clean-run control arm\n\n")
    parts.append(
        _frame_to_md(
            suite.query("clean_run", experiment),
            {
                "clean_success_rate": ".1%",
                "clean_scr": ".1%",
                "clean_call_overhead": ".2f",
                "clean_usd_per_run": ".4f",
            },
        )
    )

    parts.append("\n## Confidence intervals (percentile bootstrap, 10k resamples)\n\n")
    parts.append("| config | silent corruption | recovery |\n|---|---|---|\n")
    for config in _ordered(
        [c for c in suite.query("efficiency", experiment)["config"]], CONFIG_ORDER
    ):
        scr = suite.scr_interval(experiment, config)
        rec = suite.recovery_interval(experiment, config)
        if scr.n:
            parts.append(f"| `{config}` | {scr} | {rec} |\n")

    path = out_dir / f"{experiment}_results.md"
    path.write_text("".join(parts), encoding="utf-8")
    return path, figures


__all__ = [
    "decomposition_table",
    "double_exec_chart",
    "heatmap",
    "write_report",
]
