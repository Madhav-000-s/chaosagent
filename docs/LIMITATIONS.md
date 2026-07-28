# Limitations

Written before the results section, on purpose. Everything here constrains how
far the numbers in the README generalise.

## The scale of the shipped grids

**This is the most important limitation and it is a budget constraint, not a
design one.** The experiment as designed is 8,100 runs for E1 alone
(6 configs × 9 fault classes × 50 tasks × 3 seeds), costing on the order of
$250 on Haiku 4.5. The grids that were actually executed and reported are
reduced to fit a **$10 total budget**: full width on the two axes the headline
figure is plotted over (config × fault class), with tasks cut to a stratified
sample and seeds cut to one.

The consequences are concrete:

- **Cell counts are small**, often single digits. Every rate in the README is
  reported with a bootstrap confidence interval, and many of those intervals are
  wide enough that adjacent configurations are not distinguishable. Where an
  interval straddles zero, the honest reading is "this grid cannot tell", not
  "no effect".
- **Task-level variance is under-sampled.** Two tasks cannot represent eight
  templates. A configuration that happens to suit the sampled tasks will look
  better than it is.
- **Single seed** means fault position is drawn once per cell rather than
  averaged over three draws.

The full grids are committed (`experiments/e1_main_full.yaml`) and the sweep is
resumable and cost-bounded, so this is a matter of spend rather than of
re-engineering. Treat the shipped numbers as a demonstration that the harness
measures what it claims to measure, not as a settled estimate of any rate.

## Random-position injection dilutes the signal, and that shaped the results

This was discovered by running E1, not predicted, and it is the single most
important thing to understand about the headline grid.

**Not all writes are equally dangerous.** Injecting at a uniformly random
position means most faults land on a *recoverable* call. Cancelling an
already-cancelled order or releasing already-released stock is harmless — the
agent retries, the world absorbs it, and every configuration looks identical.
In E1, the injected faults landed on `cancel_order` and `release_stock`, not on
`charge_payment`, and the resulting silent-corruption rate is near zero across
the board.

That is a real result about random-position injection, and it is reported as
one. But it is not the question anybody cares about, so `e3`, `e4` and `e6` use
the design's other schedule — *single, targeted at a specific tool* — aimed at
`charge_payment`. Those grids answer the conditional question: **given that a
fault hits a non-idempotent write, what does each configuration do?** That is
where the double-execution signal lives, and the two schedules should be read as
answering different questions rather than as one being a harder version of the
other.

The generalisable point: **a fault-injection result is only as meaningful as the
call the fault landed on.** A harness that reports an aggregate over uniformly
sampled positions will understate risk, because production incidents are not
uniformly distributed over your call graph either — they concentrate wherever
load and retries concentrate.

## Some fault classes rarely land at all

`stale` landed on **0 of 12** attempts in E1, and `silent_empty` on 7 of 12.
This is not an injector bug — it was verified to fire correctly when targeted.
Both classes apply only to *reads*, and `stale` additionally needs a read that
occurs **after** a write, so it can replay a genuine pre-mutation view. The
agents in this study front-load their reads and then write to the end of the
trajectory, leaving `stale` nowhere to land.

Landing rate is therefore reported as a first-class column rather than assumed,
and runs where nothing landed are excluded from every faulted-arm rate. The
consequence is that `stale` — which the design expected to be the hardest
column, and the one verification reads should dominate — is effectively
**unmeasured** at random position. Any claim about verification reads in this
write-up is correspondingly weak.

## One synthetic environment

A single order/fulfilment world with 14 tools. Findings may not transfer to
code-editing agents, browser agents, or long-horizon planning. **The harness
generalises; the numbers do not.**

The environment was also designed knowing what would be measured, which is a
real risk: the write tools deliberately do not deduplicate themselves, because
if their preconditions rejected a repeat charge there would be no double-charge
to observe. That is faithful to how payment gateways actually behave, but it is
a choice that makes the phenomenon visible rather than one that discovered it.

## Injected faults are a caricature

Real outages are correlated, bursty, and partially observable. The injector
models none of that. Faults arrive independently, at controlled positions, from
a fixed taxonomy of eight. The stochastic schedule (E5) is the only place
compounding is exercised at all, and it compounds independently rather than in
the correlated way real incidents do.

## Short trajectories

Optimal solutions are 4–7 calls. Long-horizon compounding — where a corrupted
early read poisons twenty subsequent decisions — is unexplored. The blast-radius
metric is bounded by trajectory length, so its absolute values are small by
construction.

## Single agent, single user turn

No multi-agent fault propagation, no user in the loop to notice something is
wrong. The agent gets one prompt and runs to completion.

## The `oracle` bound is optimistic

`oracle` is told exactly which call was corrupted and how. That assumes perfect
fault attribution, which no real system has. The gap between `guarded` and
`oracle` is therefore an *upper* bound on remaining headroom, and a real system
would capture less of it.

## Model-tier comparison is confounded

The three tiers in the model sweep do not accept the same request body, and this
could not be neutralised:

| | Haiku 4.5 | Sonnet 5 | Opus 5 |
| --- | --- | --- | --- |
| `temperature` | accepted | **400** | **400** |
| `effort` | **unsupported** | supported | supported |
| omitting `thinking` | no thinking | **adaptive thinking on** | **adaptive thinking on** |

No sampling parameters are sent to any model, which resolves the first row
cleanly. The third row cannot be resolved: Haiku 4.5 has no adaptive thinking to
enable, and disabling thinking on Opus 5 has documented failure modes — tool
calls emitted as plain text instead of structured calls — that would corrupt
*this study specifically*, since a tool call that silently never runs is
indistinguishable from the fault being measured.

The thinking-capable arms therefore run at the lowest effort setting. The model
sweep compares **models as they are actually deployed**, not models holding
reasoning budget fixed. A frontier model that recovers better may be doing so
because it reasons more, not because it is more capable per token.

## Claim parsing

Silent corruption requires knowing what the agent claimed. The base prompt asks
every configuration for a `RESULT: SUCCESS` / `RESULT: FAILURE` line, and the
claim is parsed from it. Runs where the agent never emitted a parseable line are
recorded with `claim_explicit = false` and counted as *not* claiming success,
which biases the silent-corruption rate **downward**. That is the conservative
direction, but it is a bias.

## Determinism has a boundary

The environment, the task set, and the fault injector are fully deterministic
and tested to be so over 100 repetitions. **The model is not.** Reproducibility
across runs comes from the response cache, not from the API: no sampling
parameters are sent, and `temperature=0` would not have guaranteed identical
outputs even where it is accepted. A cached run replays exactly; an uncached
re-run may diverge.

## Model versions drift

Results are timestamped and the response cache is released so they can be
re-derived exactly. Re-running against the same model alias at a later date may
produce different numbers, and that is a property of the ecosystem rather than
of this harness.
