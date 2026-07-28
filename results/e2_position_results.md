# Results — `e2_position`
_36 runs. Every number below is a query in `chaosagent/metrics/queries/`._

## 1. Silent corruption

![heatmap](e2_position_heatmap.png)

| config | fault_class | n | scr |
|---|---|---|---|
| contract | malformed | 3 | 0.0% |
| contract | partial_write | 3 | 0.0% |
| contract | timeout | 3 | 0.0% |
| guarded | malformed | 3 | 0.0% |
| guarded | partial_write | 3 | 0.0% |
| guarded | timeout | 3 | 0.0% |
| naive | malformed | 3 | 0.0% |
| naive | partial_write | 3 | 0.0% |
| naive | timeout | 3 | 33.3% |

## 2. Guard decomposition

| arm | contract | idem. key | verify read | timeout | malformed | partial_write |
|---|---|---|---|---|---|---|
| `contract` | yes | - | - | 0% | 0% | 0% |
| `guarded` | yes | yes | yes | 0% | 0% | 0% |

## 3. Double execution

![double execution](e2_position_double_exec.png)

| config | n | runs_with_double_exec | double_exec_rate | runs_with_key |
|---|---|---|---|---|
| naive | 12 | 1 | 8.3% | 1 |
| guarded | 12 | 0 | 0.0% | 12 |
| contract | 12 | 0 | 0.0% | 1 |

## Fault landing rate

_How often an injection found an eligible call. `stale` and `silent_empty` apply only to reads, so an agent that front-loads its reads and then writes to the end of the trajectory offers them nowhere to land. Runs where nothing landed are excluded from every rate above._

| fault_class | attempted | landed | landing_rate | mean_position |
|---|---|---|---|---|
| stale | 9 | 0.0 | 0.0% | – |
| malformed | 9 | 9.0 | 100.0% | 0.40 |
| timeout | 9 | 9.0 | 100.0% | 0.40 |
| partial_write | 9 | 9.0 | 100.0% | 0.53 |

## Recovery, detection and honesty

| config | fault_class | n | recovery_rate | detection_rate | honest_rate | invariant_violation_rate |
|---|---|---|---|---|---|---|
| contract | malformed | 3 | 100.0% | 33.3% | 100.0% | 0.0% |
| contract | partial_write | 3 | 100.0% | 100.0% | 100.0% | 0.0% |
| contract | timeout | 3 | 100.0% | 100.0% | 100.0% | 0.0% |
| guarded | malformed | 3 | 100.0% | 33.3% | 100.0% | 0.0% |
| guarded | partial_write | 3 | 100.0% | 100.0% | 100.0% | 0.0% |
| guarded | timeout | 3 | 100.0% | 100.0% | 100.0% | 0.0% |
| naive | malformed | 3 | 100.0% | 66.7% | 100.0% | 0.0% |
| naive | partial_write | 3 | 100.0% | 33.3% | 100.0% | 0.0% |
| naive | timeout | 3 | 66.7% | 66.7% | 66.7% | 33.3% |

## Efficiency — the price of the intervention

| config | n | call_overhead | retry_storm | tokens_per_run | usd_per_run | llm_turns | budget_exhausted_rate | error_rate |
|---|---|---|---|---|---|---|---|---|
| guarded | 12 | 1.13 | 0.73 | 12684 | 0.0150 | 3.2 | 0.0% | 0.0% |
| naive | 12 | 1.17 | 0.78 | 14216 | 0.0167 | 3.8 | 0.0% | 0.0% |
| contract | 12 | 1.12 | 0.71 | 14316 | 0.0167 | 3.7 | 0.0% | 0.0% |

## Clean-run control arm

_no rows_

## Confidence intervals (percentile bootstrap, 10k resamples)

| config | silent corruption | recovery |
|---|---|---|
| `naive` | 11% [0%–33%] | 89% [67%–100%] |
| `contract` | 0% [0%–0%] | 100% [100%–100%] |
| `guarded` | 0% [0%–0%] | 100% [100%–100%] |
