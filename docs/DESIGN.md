# Design

How the harness is put together, and why each piece is shaped the way it is.

```
CLI (Typer)  run · sweep · report · replay · validate
                        │
        ORCHESTRATOR   experiments/*.yaml → run matrix
        async semaphore · resume · cost ceiling
                        │
   ┌────────────────────┴─────────────────────┐
   │  RUNNER   one run = task × config × seed │
   │                                          │
   │   ┌───────┐  action   ┌───────────────┐  │
   │   │ AGENT │ ────────▶ │ FAULT INJECTOR│  │
   │   │(config)│ ◀──────── │  (middleware) │  │
   │   └───────┘ observation└───────┬───────┘  │
   │       ▲                        ▼          │
   │  ┌────┴────┐           ┌───────────────┐  │
   │  │   LLM   │           │  ENVIRONMENT  │  │
   │  │ (cache) │           │  SQLite world │  │
   │  └─────────┘           └───────┬───────┘  │
   └────────────────────────────────┼──────────┘
                    traces │        │ final state
                           ▼        ▼
              TRACE STORE (DuckDB) + RESPONSE CACHE
                           │
              SCORER → METRICS → REPORT
```

## The three invariants the architecture preserves

**1. The agent cannot distinguish an injected fault from a real one.**
The injector sits *below* the agent's tool interface and returns exactly the
shapes the environment returns. No fault metadata reaches agent-visible content
— asserted by `tests/test_fault_transparency.py`, which checks the visible
error-code vocabulary is shared between real and injected failures, that
injection detail never appears in a payload, and that a control run is
byte-identical to an unwrapped environment. `oracle` is the deliberate exception
and is exempted at the agent layer, never by relaxing the injector.

**2. A run is a pure function of (task, config, model, seed).**
Environment snapshot/restore, a virtual clock, seeded fault selection, and
cached LLM responses. The environment's determinism is tested over 100
repetitions per fault class. The model's is not deterministic and cannot be made
so — see `LIMITATIONS.md`.

**3. Every reported number is derivable from the trace store alone.**
No metric is computed inside the run loop. The runner writes facts; the scorer
derives judgements later. A metric bug therefore costs a re-score, not a re-run.

## Module contracts

### `chaosagent/env` — the world

Deterministic SQLite state, 14 tools, 5 invariants, snapshot/restore, virtual
clock. Knows nothing about agents, faults, or LLMs.

`env.call_log()` is the environment's own ground-truth record of what executed.
It is not derived from anything the agent saw. **Double execution is counted
here**, because under a `partial_write` fault the agent's trace shows one failed
call while the world shows one committed write — and that discrepancy is the
entire measurement.

The write tools deliberately do not defend themselves against repetition. See
`LIMITATIONS.md` for why that is faithful rather than convenient.

**Invariants**, each a pure predicate over world state:

| Name | Assertion |
| --- | --- |
| `reserved_le_onhand` | reserved ≤ on-hand for every SKU, neither negative |
| `no_double_charge` | no (order, amount) charged twice without an intervening refund |
| `order_total_consistent` | `order.total == Σ line_items.subtotal` |
| `shipped_implies_reserved` | every shipment consumed stock that was actually reserved |
| `refund_le_charge` | `Σ refunds(order) ≤ Σ charges(order)` |

Some task fixtures start in a violating state on purpose (the reconciliation
task seeds a duplicate charge), so the scorer reports violations **newly
introduced** during a run rather than absolute counts.

### `chaosagent/tasks` — generation and reference solving

Eight templates expanded over parameter lists into exactly 50 tasks. No task is
hand-authored beyond the templates.

`min_calls` is **measured** by walking the scripted reference solution during
generation, never asserted by hand, so the call-overhead denominator cannot
drift from the solution.

Terminal state is asserted *semantically* — order status, net paid, charge
count, line quantities, stock levels — rather than by whole-state comparison. An
extra read that shifts a generated id is not a failure; a double charge still
is.

**Gate:** `chaosagent validate` must show 50/50 solved with zero attributable
invariant violations before any agent work is trusted.

### `chaosagent/faults` — the injector

Eight classes over four outcome types. See `FAULT_TAXONOMY.md`.

### `chaosagent/agents` — the configurations

All eight configs share one ReAct loop (`agents/loop.py`) and differ only in
three injected objects: an error formatter, a recovery policy, and a call
decorator. Keeping one loop is what makes the guard decomposition honest — no
arm can gain an incidental advantage from a differently-written loop.

The recovery policy is four independent booleans rather than a class hierarchy,
so `contract+idem` and `contract+verify` are literally the same code path as
`guarded` with one flag flipped.

### `chaosagent/runtime` — client, cache, traces, budgets

The response cache keys on the request body. **The seed is deliberately not part
of the key**: it reaches the cache through the messages, because a different
seed places the fault at a different call, which changes the observations. Two
seeds producing byte-identical prompts *should* share an entry.

Budget exhaustion terminates a run with a `stop_reason`, never an exception. A
config that "recovers" by burning ten times the budget has not recovered, so
those runs are reported as their own category.

### `chaosagent/metrics` — the scorer

Pure and offline. Every metric is a `.sql` file in `metrics/queries/`, shipped so
a reviewer who can read the query can check the number without trusting the code
that produced it.

## Schema

The pair that matters:

```sql
CREATE TABLE calls (
    ...
    env_executed      BOOLEAN,   -- did the world actually mutate
    agent_saw_error   BOOLEAN,   -- what the agent observed
    ...
);
```

`env_executed AND agent_saw_error` is the trap, expressed at the schema level.
Almost every headline metric is a rollup of that condition.

## Deviations from the original plan

Recorded rather than quietly absorbed:

1. **The "bulk restock" task template** is not expressible against the declared
   14-tool surface, which has no restock tool. Replaced by `free_stock`, which
   frees stock by cancelling an abandoned order — same shape (stock pressure
   requiring a release before a reservation), same non-idempotent write in the
   tail.

2. **`state_correct` is stored on the `runs` row** as a convenience
   denormalisation rather than being computed purely at score time. The
   underlying facts — the full assertion-failure list and a terminal-state
   summary — are stored alongside it, so the architectural property that
   matters (a metric bug costs a re-score, not a re-run) is preserved.

3. **Grid sizes are reduced** to fit a $10 budget. See `LIMITATIONS.md`.

4. **An experiment was added.** `e6_nonidempotent` did not exist in the original
   plan. It uses the design's *tool-targeted* schedule to aim every fault at
   `charge_payment`, and `e3` and `e4` were retargeted the same way after E1
   showed that random-position injection mostly lands on recoverable calls and
   flattens every configuration to the same result. Both schedules are in the
   original design; which one a grid uses turned out to matter far more than
   anticipated. See `LIMITATIONS.md` → "Random-position injection dilutes the
   signal".
