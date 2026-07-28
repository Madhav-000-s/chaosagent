"""Supporting figures beyond the three headline artifacts.

The three headline artifacts are produced by ``chaosagent report``, which is the
supported path and the one CI exercises. This script adds the figures that
depend on specific experiments having been run:

* position sweep     (E2) — recovery vs. normalised fault position
* model sweep        (E4) — does capability substitute for structure?
* cost vs. benefit   (E1) — silent-corruption reduction against token overhead

Run with:  python analysis/supporting_figures.py --db traces/chaosagent.duckdb

Written as a script rather than a notebook on purpose: it runs in CI, it diffs
cleanly in review, and it cannot carry stale committed output.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from chaosagent.metrics.scorer import open_suite  # noqa: E402

BUCKETS = ["early", "mid", "late"]


def position_figure(suite, experiment: str, out: Path) -> Path | None:
    df = suite.query("position_sweep", experiment)
    if df.empty:
        return None

    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    for config in sorted(df["config"].unique()):
        sub = df[df["config"] == config].groupby("position_bucket")["recovery_rate"].mean()
        xs = [b for b in BUCKETS if b in sub.index]
        ax.plot(xs, [sub[b] for b in xs], marker="o", label=config)

    ax.set_xlabel("fault position (normalised against optimal trajectory length)")
    ax.set_ylabel("recovery rate")
    ax.set_ylim(-0.05, 1.05)
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    ax.set_title("Does it matter where the fault lands?")
    ax.legend(frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=160)
    plt.close(fig)
    return out


def model_figure(suite, experiment: str, out: Path) -> Path | None:
    df = suite.query("model_sweep", experiment)
    if df.empty:
        return None

    models = sorted(df["model"].unique())
    configs = sorted(df["config"].unique())
    width = 0.8 / max(len(configs), 1)

    fig, ax = plt.subplots(figsize=(7.6, 4.4))
    for i, config in enumerate(configs):
        sub = df[df["config"] == config].set_index("model")
        xs = [j + i * width for j in range(len(models))]
        ys = [float(sub.loc[m, "scr"]) if m in sub.index else 0.0 for m in models]
        ax.bar(xs, ys, width=width, label=config)

    ax.set_xticks([j + 0.4 - width / 2 for j in range(len(models))], models, fontsize=9)
    ax.set_ylabel("silent corruption rate")
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    ax.set_title("Does model capability substitute for structural guards?")
    ax.legend(frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=160)
    plt.close(fig)
    return out


def cost_benefit_figure(suite, experiment: str, out: Path) -> Path | None:
    scr = suite.query("silent_corruption", experiment)
    eff = suite.query("efficiency", experiment)
    if scr.empty or eff.empty:
        return None

    mean_scr = scr.groupby("config")["scr"].mean()
    merged = eff.set_index("config").join(mean_scr.rename("scr")).dropna(subset=["scr"])
    if merged.empty:
        return None

    fig, ax = plt.subplots(figsize=(7.0, 4.6))
    ax.scatter(merged["tokens_per_run"], merged["scr"], s=90, zorder=3)
    for config, row in merged.iterrows():
        ax.annotate(
            config,
            (row["tokens_per_run"], row["scr"]),
            textcoords="offset points",
            xytext=(7, 5),
            fontsize=9,
        )

    ax.set_xlabel("tokens per run")
    ax.set_ylabel("silent corruption rate")
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    ax.set_title("Cost against benefit — down and to the left is better")
    ax.grid(alpha=0.25, zorder=0)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=160)
    plt.close(fig)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="traces/chaosagent.duckdb")
    parser.add_argument("--out", default="results")
    args = parser.parse_args()

    suite, conn = open_suite(args.db)
    out = Path(args.out)
    try:
        available = set(suite.experiments())
        made: list[Path] = []
        for experiment, builder, filename in (
            ("e2_position", position_figure, "e2_position_sweep.png"),
            ("e4_models", model_figure, "e4_model_sweep.png"),
            ("e1_main", cost_benefit_figure, "e1_cost_benefit.png"),
        ):
            if experiment not in available:
                print(f"skip {filename}: experiment '{experiment}' has no runs")
                continue
            path = builder(suite, experiment, out / filename)
            if path:
                made.append(path)
                print(f"wrote {path}")
        if not made:
            print("nothing to plot — run `chaosagent sweep` first")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
