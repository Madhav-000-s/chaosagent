# Analysis

Everything here is a pure function of `traces/chaosagent.duckdb`. Nothing is
hand-drawn, and no figure is committed as a one-off image — every artifact
regenerates from the trace store.

## The three headline artifacts

Produced by the CLI, not by anything in this directory:

```bash
chaosagent report --experiment e1_main --out results/
```

That writes `results/e1_main_results.md` plus:

1. **Silent-corruption heatmap** — config (rows) × fault class (columns).
2. **Guard decomposition table** — which mechanism fixes which fault.
3. **Double-execution bar chart** — one bar per config.

It is the supported path and the one CI exercises, so it cannot rot.

## Supporting figures

These depend on specific experiments having been run:

```bash
python analysis/supporting_figures.py --db traces/chaosagent.duckdb --out results/
```

| Figure | Needs | Question |
| --- | --- | --- |
| `e2_position_sweep.png` | `e2_position` | Does *where* the fault lands matter? |
| `e4_model_sweep.png` | `e4_models` | Does capability substitute for structure? |
| `e1_cost_benefit.png` | `e1_main` | What does each configuration cost for its reliability? |

Experiments with no runs are skipped with a message rather than failing.

## Why scripts and not notebooks

Notebooks carry committed output that drifts from the code that produced it,
diff badly in review, and don't run in CI. A script that regenerates the same
figure on every run is strictly more reproducible, which is the property this
project is actually claiming.

## Checking a number by hand

Every metric is a `.sql` file in `chaosagent/metrics/queries/`. To verify one
without trusting any Python:

```bash
python -c "from chaosagent.metrics.scorer import load_query; print(load_query('double_execution'))"
```

Then run it against the released trace database in any DuckDB shell.
