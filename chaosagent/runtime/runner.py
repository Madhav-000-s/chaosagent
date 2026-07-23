"""One run = one task × config × model × seed × fault spec.

Follows the control flow in the design exactly:

    env = Environment(seed, task.init_state)
    faulty = FaultInjector(spec, seed, task.min_calls).wrap(env)
    result = await agent.run(task, faulty, llm, budget)
    terminal   = env.state()
    violations = env.check_invariants()
    trace.finish(...)

The runner writes facts and nothing else. It records what the agent claimed and
what the world became; whether that pair counts as a success is the scorer's
call, made later, from the trace.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from chaosagent.agents.configs import AgentConfig
from chaosagent.agents.loop import AgentOutcome, run_agent
from chaosagent.env import registry
from chaosagent.env.invariants import check_all, new_violations
from chaosagent.env.world import Environment
from chaosagent.faults.injector import FaultInjector
from chaosagent.faults.types import FaultSpec
from chaosagent.runtime.llm import LLMClient
from chaosagent.runtime.types import Budget, RunResult
from chaosagent.tasks.types import Task


def make_run_id(
    experiment: str, task_id: str, config: str, model: str, seed: int, spec: FaultSpec | None
) -> str:
    """Deterministic id, so a resumed sweep recognises what it already did."""
    fault = "none" if spec is None or spec.is_control else spec.label()
    blob = f"{experiment}|{task_id}|{config}|{model}|{seed}|{fault}"
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


async def execute_run(
    *,
    experiment: str,
    task: Task,
    config: AgentConfig,
    llm: LLMClient,
    seed: int,
    fault_spec: FaultSpec | None,
    budget: Budget,
) -> tuple[RunResult, AgentOutcome, list[dict[str, Any]]]:
    """Run one cell of the experiment grid."""
    run_id = make_run_id(experiment, task.id, config.name, llm.model, seed, fault_spec)
    started = time.perf_counter()

    env = Environment(seed=seed, init_state=task.init_state)
    baseline = check_all(env)

    spec = fault_spec or FaultSpec(fault_class="none")
    injector = FaultInjector(spec, seed=seed, trajectory_hint=task.min_calls)
    faulty = injector.wrap(env)

    outcome = await run_agent(task, faulty, llm, config, budget)

    terminal = env.state()
    failures = task.expected_terminal.failures(terminal)
    violations = [str(v) for v in new_violations(baseline, check_all(env))]
    injected = faulty.injected()

    result = RunResult(
        run_id=run_id,
        experiment=experiment,
        task_id=task.id,
        config=config.name,
        model=llm.model,
        seed=seed,
        fault_spec=spec,
        fault_landed=bool(injected),
        fault_position=injected[0].call_index if injected else None,
        agent_claimed_success=outcome.claimed_success,
        agent_final_message=outcome.final_message,
        trajectory_len=len(faulty.history),
        optimal_len=task.min_calls,
        assertion_failures=failures,
        terminal_state=_summarise(terminal),
        invariant_violations=violations,
        stop_reason=outcome.stop_reason,
        tokens_in=outcome.tokens_in,
        tokens_out=outcome.tokens_out,
        usd=llm.profile.cost(outcome.tokens_in, outcome.tokens_out),
        wall_ms=int((time.perf_counter() - started) * 1000),
        cache_hits=outcome.cache_hits,
        llm_calls=len(outcome.llm_calls),
        error=outcome.error,
    )

    calls = _call_rows(faulty)
    env.close()
    return result, outcome, calls


def _call_rows(faulty: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for call in faulty.history:
        spec = registry.spec_for(call.tool) if registry.get(call.tool) else None
        payload = call.result.payload
        rows.append(
            {
                "call_index": call.call_index,
                "tool": call.tool,
                "tool_kind": spec.kind if spec else "unknown",
                "args_json": json.dumps(call.args, sort_keys=True, default=str),
                "idempotency_key": call.idempotency_key,
                "fault_applied": call.fault_applied,
                "fault_outcome": call.injection.outcome if call.injection else None,
                "env_executed": call.env_executed,
                "agent_saw_error": call.agent_saw_error,
                "payload_json": json.dumps(payload, default=str)[:4000]
                if payload is not None
                else None,
            }
        )
    return rows


def _summarise(state: Any) -> dict[str, Any]:
    """A compact terminal-state record. The full state lives in the digest."""
    return {
        "digest": state.digest(),
        "orders": [
            {
                "id": o["id"],
                "customer_id": o["customer_id"],
                "status": o["status"],
                "total_cents": o["total_cents"],
            }
            for o in state.orders
        ],
        "payments": [
            {"order_id": p["order_id"], "kind": p["kind"], "amount_cents": p["amount_cents"]}
            for p in state.payments
        ],
        "shipments": [{"order_id": s["order_id"]} for s in state.shipments],
        "inventory": [
            {"sku": i["sku"], "onhand": i["onhand"], "reserved": i["reserved"]}
            for i in state.inventory
        ],
    }


__all__ = ["execute_run", "make_run_id"]
