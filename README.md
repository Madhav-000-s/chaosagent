# chaosagent

**Chaos engineering for LLM agent loops.** A deterministic fault-injection harness that sits
between a tool-calling agent and its tools, injects realistic failures at controlled positions in
a trajectory, and measures what the agent actually does about it.

> 🚧 Under construction. The results-first README lands with the final release.

## Status

| Layer | State |
| --- | --- |
| `chaosagent/env` — SQLite world, 14 tools, 5 invariants | ✅ |
| `chaosagent/tasks` — templates, generator, reference solver | ⏳ |
| `chaosagent/faults` — 8 fault classes | ⏳ |
| `chaosagent/agents` — ReAct loop, 8 configurations | ⏳ |
| `chaosagent/runtime` — cache, traces, budgets, orchestrator | ⏳ |
| `chaosagent/metrics` — scorer, SQL metrics, bootstrap CIs | ⏳ |

## License

MIT — see [LICENSE](LICENSE).
