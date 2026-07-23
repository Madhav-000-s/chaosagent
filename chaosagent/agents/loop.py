"""The one ReAct loop.

Every configuration runs this code. Read the branches carefully — especially the
retry branch, which is where the double charge comes from:

    result = env.call(tool, args)
    while not result.ok and attempts <= policy.blind_retry:
        result = env.call(tool, args)      # identical args, identical call

With the identity decorator that is a second charge. With the idempotency-key
decorator the arguments carry a content-addressed key, the environment
recognises the repeat, and nothing executes twice. Same line of code, opposite
outcome — which is the finding the whole project is built to state precisely.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from chaosagent.agents.configs import AgentConfig
from chaosagent.agents.prompts import REFLECTION_TURN
from chaosagent.agents.strategies import (
    OracleFormatter,
    contract_guidance,
    verification_call,
)
from chaosagent.env import registry
from chaosagent.runtime.llm import LLMClient
from chaosagent.runtime.types import Budget, LLMCallRecord, StopReason
from chaosagent.tasks.types import Task

#: Cap on how much of a payload is shown to the model. A 50-order list_orders
#: response would otherwise dominate the context and the token bill.
MAX_OBSERVATION_CHARS = 4_000


@dataclass
class AgentOutcome:
    claimed_success: bool = False
    #: False when the agent never emitted a parseable RESULT line. Recorded so
    #: the scorer can separate "claimed wrongly" from "never claimed".
    claim_explicit: bool = False
    final_message: str = ""
    stop_reason: StopReason = "done"
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: list[LLMCallRecord] = field(default_factory=list)
    tool_calls: int = 0
    cache_hits: int = 0
    error: str | None = None


def tool_schemas() -> list[dict[str, Any]]:
    """Tool definitions in the shape the Messages API expects."""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.parameters,
        }
        for spec in registry.specs()
    ]


def parse_claim(text: str) -> bool | None:
    """Read the agent's own success claim off its final message.

    Silent corruption is "claimed success while the world is wrong", so the
    claim has to be a fact rather than a reading of tone. Returns None when the
    agent never emitted the line.
    """
    for line in reversed(text.strip().splitlines()):
        stripped = line.strip().upper().lstrip("*_# ")
        if stripped.startswith("RESULT:"):
            return "SUCCESS" in stripped
    return None


def _truncate(text: str) -> str:
    if len(text) <= MAX_OBSERVATION_CHARS:
        return text
    return text[:MAX_OBSERVATION_CHARS] + f"\n... [{len(text)} chars total, truncated]"


async def run_agent(
    task: Task,
    env: Any,
    llm: LLMClient,
    config: AgentConfig,
    budget: Budget,
) -> AgentOutcome:
    """Run one task to completion, budget exhaustion, or failure."""
    outcome = AgentOutcome()
    tools = tool_schemas()
    messages: list[dict[str, Any]] = [{"role": "user", "content": task.prompt}]
    policy = config.recovery_policy
    step = 0
    usd = 0.0

    while True:
        if step >= budget.max_steps:
            outcome.stop_reason = "budget"
            break
        if outcome.tokens_in + outcome.tokens_out >= budget.max_tokens:
            outcome.stop_reason = "budget"
            break
        if usd >= budget.max_usd:
            outcome.stop_reason = "budget"
            break

        try:
            completion = await llm.complete(config.system_prompt, messages, tools)
        except Exception as exc:  # noqa: BLE001 - one bad run must not kill a sweep
            outcome.stop_reason = "error"
            outcome.error = f"{type(exc).__name__}: {exc}"
            break

        step += 1
        outcome.tokens_in += completion.tokens_in
        outcome.tokens_out += completion.tokens_out
        outcome.cache_hits += int(completion.cache_hit)
        outcome.llm_calls.append(
            LLMCallRecord(
                step=step,
                cache_hit=completion.cache_hit,
                prompt_hash=completion.prompt_hash,
                tokens_in=completion.tokens_in,
                tokens_out=completion.tokens_out,
                latency_ms=completion.latency_ms,
            )
        )
        if not completion.cache_hit:
            usd += llm.profile.cost(completion.tokens_in, completion.tokens_out)

        if completion.refusal is not None:
            outcome.stop_reason = "unrecoverable"
            outcome.error = f"api_refusal:{completion.refusal}"
            break

        # No tool calls means the agent is finished and is reporting.
        if not completion.tool_calls:
            outcome.final_message = completion.text
            claim = parse_claim(completion.text)
            outcome.claim_explicit = claim is not None
            outcome.claimed_success = bool(claim)
            outcome.stop_reason = "done"
            break

        assistant_blocks: list[dict[str, Any]] = []
        if completion.text:
            assistant_blocks.append({"type": "text", "text": completion.text})
        for call in completion.tool_calls:
            assistant_blocks.append(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.args}
            )
        messages.append({"role": "assistant", "content": assistant_blocks})

        results: list[dict[str, Any]] = []
        saw_error = False
        budget_hit = False

        for call in completion.tool_calls:
            if outcome.tool_calls >= budget.max_calls:
                budget_hit = True
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": "Call budget exhausted. Stop and report your result.",
                        "is_error": True,
                    }
                )
                continue

            observation, is_error, used = _execute(call.name, call.args, env, config, policy)
            outcome.tool_calls += used
            saw_error = saw_error or is_error
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": _truncate(observation),
                    "is_error": is_error,
                }
            )

        messages.append({"role": "user", "content": results})

        if budget_hit:
            outcome.stop_reason = "max_calls"
            # Give the agent one final turn to report, rather than cutting it
            # off mid-trajectory with no claim to score.
            budget = budget.model_copy(update={"max_steps": min(budget.max_steps, step + 1)})

        if saw_error and policy.reflect:
            messages.append({"role": "user", "content": REFLECTION_TURN})

    return outcome


def _execute(
    tool: str,
    raw_args: dict[str, Any],
    env: Any,
    config: AgentConfig,
    policy: Any,
) -> tuple[str, bool, int]:
    """Run one tool call under the config's policy.

    Returns ``(observation, is_error, tool_calls_consumed)``. The count includes
    blind retries and automatic verification reads, because those are real calls
    against the world and their cost belongs in the results table.
    """
    spec = registry.spec_for(tool) if registry.get(tool) else None
    args = config.call_decorator.decorate(tool, raw_args, spec)

    result = env.call(tool, args)
    consumed = 1
    record = _last_injection(env)

    # Blind retry: identical call, identical arguments. Safe only because the
    # decorator may have put a stable idempotency key in them.
    attempts = 0
    while not result.ok and attempts < policy.blind_retry:
        result = env.call(tool, args)
        consumed += 1
        attempts += 1

    if result.ok:
        payload = json.dumps(result.payload, indent=2, default=str)
        if config.sees_oracle and record is not None:
            payload += f"\n\n[FAULT DISCLOSURE] {_disclose(record)}"
        return payload, False, consumed

    envelope = result.error
    assert envelope is not None

    if isinstance(config.error_formatter, OracleFormatter) and record is not None:
        text = config.error_formatter.format_with_disclosure(envelope, _disclose(record))
    else:
        text = config.error_formatter.format(envelope)

    if policy.contract_aware:
        text += "\n\n" + contract_guidance(envelope)

    # Verification read: the mechanism under test, and the one that costs a call.
    if (
        policy.verify_read
        and envelope.state_may_have_changed
        and spec is not None
        and spec.kind == "write"
    ):
        verify = verification_call(tool, args)
        if verify is not None:
            read_tool, read_args = verify
            check = env.call(read_tool, read_args)
            consumed += 1
            if check.ok:
                text += (
                    f"\n\nVerification read issued automatically ({read_tool}). "
                    f"Current state:\n{_truncate(json.dumps(check.payload, indent=2, default=str))}"
                )
            else:
                text += f"\n\nVerification read ({read_tool}) also failed; state remains unknown."

    return text, True, consumed


def _last_injection(env: Any) -> Any:
    history = getattr(env, "history", None)
    if not history:
        return None
    return history[-1].injection


def _disclose(record: Any) -> str:
    executed = "DID execute against the world" if record.env_executed else "did NOT execute"
    return (
        f"The call to '{record.tool}' {executed}. "
        f"The harness applied a '{record.fault_class}' fault ({record.outcome}): {record.detail}."
    )


def now_ms() -> int:
    return int(time.perf_counter() * 1000)


__all__ = ["AgentOutcome", "parse_claim", "run_agent", "tool_schemas"]
