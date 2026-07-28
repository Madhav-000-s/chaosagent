# Datasheet — the chaosagent task set

Following the spirit of *Datasheets for Datasets* (Gebru et al.), scaled to what
this artifact actually is: a small, fully synthetic, programmatically generated
task suite.

## Motivation

**Why was it created?** To measure what tool-calling agents do when their tools
misbehave. That requires tasks whose correct terminal state is knowable without
human annotation, so that every metric is a state assertion rather than a
judgement call.

**Who created it?** Generated programmatically by this repository. No external
data, no third-party dataset, no human annotators.

## Composition

- **50 tasks**, expanded from **8 templates** over fixed parameter lists.
- Optimal trajectories of **4–7 tool calls** (measured by the reference solver,
  not asserted).
- Every task involves at least one non-idempotent write. There are no
  "safe" tasks by construction — the study is about non-idempotent operations.

| Template | Tasks | Optimal calls | What it exercises |
| --- | ---: | ---: | --- |
| `place_and_charge` | 7 | 5 | The basic lifecycle through a charge |
| `out_of_stock` | 6 | 5 | Failing cleanly instead of half-fulfilling |
| `refund_then_cancel` | 6 | 4 | Ordering constraint: refund precedes cancel |
| `modify_reserved` | 6 | 6 | Release → modify → re-reserve → charge |
| `reconcile` | 6 | 5 | Finding and refunding a duplicate charge |
| `ship_paid` | 6 | 4 | Selective action across several orders |
| `free_stock` | 6 | 5 | Releasing stock held by an abandoned order |
| `tight_lifecycle` | 7 | 7 | Full lifecycle where availability is exact |

**Instances.** Each task is a prompt, an initial world state, a declarative
terminal-state assertion, and a measured optimal call count. No labels in the
supervised-learning sense — correctness is computed, not annotated.

**Is anything missing?** The task set contains no ambiguous tasks by design.
That is a deliberate restriction of scope: ambiguity would reintroduce the
judgement calls the design exists to eliminate.

**Does it contain personal data?** No. Customer names are historical computing
figures (Ada Byron, Grace Hopper, Alan Turing, Katherine Johnson) with
`@example.com` addresses. Products are generic peripherals. Nothing is derived
from real people, real orders, or real transactions.

## Collection process

Fully programmatic. `TaskGenerator` expands each template over its parameter
list; the reference solver walks each resulting task to measure its optimal
length and to confirm the terminal-state assertion actually holds on the state
an optimal solution produces.

**Validation.** `chaosagent validate` reference-solves all 50 under zero faults
and must report 50/50 with no attributable invariant violations. This is the
automated substitute for hand-validating a dataset — it is cheap, total, and
runs in under a second.

## Preprocessing

Two prompt-hygiene rules are enforced by tests, because breaking either would
confound the study:

1. **No prompt states a monetary amount.** The agent must read the order total
   before charging it. That read dependency is exactly what the `stale` and
   `wrong_type` faults attack; handing over the number makes half the fault
   classes inert.
2. **No prompt gives tool-calling advice.** No mention of idempotency, retries,
   or verifying before acting. Such guidance belongs to a *configuration*, or
   every arm receives the intervention and the comparison collapses. Describing
   the *goal* is allowed — the reconciliation task may say "duplicate charge",
   because finding one is the job.

## Uses

**Intended.** Benchmarking agent robustness under tool faults; regression-testing
an agent's tool layer; as a worked example of a fault-injection harness.

**Not appropriate.** Measuring general agent capability — the tasks are
deliberately easy under zero faults, and the control arm exists to confirm that.
Any claim about agent performance on real-world operations work; this is a
synthetic world of 14 tools.

**Risk of misuse.** Reporting the headline rates as though they characterise
"agents" in general. They characterise these agents, on this environment, at
this grid size. See `docs/LIMITATIONS.md`, particularly the section on grid
scale.

## Distribution and maintenance

- Distributed with the repository under **MIT**.
- **Versioned by content hash.** `chaosagent tasks` prints the hash; any change
  to a template, parameter list, or assertion changes it. Results are only
  comparable across runs sharing a hash.
- Regenerated deterministically on every import — there is no stored data file to
  drift out of sync with the code.
- Maintained as part of this repository; issues and changes go through the same
  git history.
