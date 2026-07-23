"""The eight configurations.

Six headline arms plus two decomposition arms. Every one is the same loop with
different strategy objects — the table below is the experiment.

| config          | formatter | retry | reflect | contract | verify | idem key |
|-----------------|-----------|-------|---------|----------|--------|----------|
| naive           | raw       |       |         |          |        |          |
| retry           | raw       | 3     |         |          |        |          |
| reflect         | raw       |       | ✓       |          |        |          |
| contract        | envelope  |       |         | ✓        |        |          |
| contract+idem   | envelope  |       |         | ✓        |        | ✓        |
| contract+verify | envelope  |       |         | ✓        | ✓      |          |
| guarded         | envelope  |       |         | ✓        | ✓      | ✓        |
| oracle          | oracle    |       |         | ✓        |        | ✓        |
"""

from __future__ import annotations

from dataclasses import dataclass

from chaosagent.agents import prompts
from chaosagent.agents.strategies import (
    CallDecorator,
    EnvelopeFormatter,
    ErrorFormatter,
    IdempotencyKeyDecorator,
    OracleFormatter,
    RecoveryPolicy,
)


@dataclass(frozen=True)
class AgentConfig:
    name: str
    error_formatter: ErrorFormatter
    recovery_policy: RecoveryPolicy
    call_decorator: CallDecorator
    system_prompt: str
    #: The one arm allowed to see fault metadata. Upper bound, not a strategy.
    sees_oracle: bool = False


NAIVE = AgentConfig(
    name="naive",
    error_formatter=ErrorFormatter(),
    recovery_policy=RecoveryPolicy(name="none"),
    call_decorator=CallDecorator(),
    system_prompt=prompts.build(),
)

RETRY = AgentConfig(
    name="retry",
    error_formatter=ErrorFormatter(),
    recovery_policy=RecoveryPolicy(name="blind_retry", blind_retry=3),
    call_decorator=CallDecorator(),
    system_prompt=prompts.build(retry=True),
)

REFLECT = AgentConfig(
    name="reflect",
    error_formatter=ErrorFormatter(),
    recovery_policy=RecoveryPolicy(name="reflect", reflect=True),
    call_decorator=CallDecorator(),
    system_prompt=prompts.build(reflect=True),
)

CONTRACT = AgentConfig(
    name="contract",
    error_formatter=EnvelopeFormatter(),
    recovery_policy=RecoveryPolicy(name="contract_aware", contract_aware=True),
    call_decorator=CallDecorator(),
    system_prompt=prompts.build(contract=True),
)

CONTRACT_IDEM = AgentConfig(
    name="contract+idem",
    error_formatter=EnvelopeFormatter(),
    recovery_policy=RecoveryPolicy(name="contract_aware", contract_aware=True),
    call_decorator=IdempotencyKeyDecorator(),
    system_prompt=prompts.build(contract=True),
)

CONTRACT_VERIFY = AgentConfig(
    name="contract+verify",
    error_formatter=EnvelopeFormatter(),
    recovery_policy=RecoveryPolicy(
        name="contract_aware+verify", contract_aware=True, verify_read=True
    ),
    call_decorator=CallDecorator(),
    system_prompt=prompts.build(contract=True),
)

GUARDED = AgentConfig(
    name="guarded",
    error_formatter=EnvelopeFormatter(),
    recovery_policy=RecoveryPolicy(
        name="contract_aware+verify", contract_aware=True, verify_read=True
    ),
    call_decorator=IdempotencyKeyDecorator(),
    system_prompt=prompts.build(contract=True, guarded=True),
)

ORACLE = AgentConfig(
    name="oracle",
    error_formatter=OracleFormatter(),
    recovery_policy=RecoveryPolicy(name="contract_aware", contract_aware=True),
    call_decorator=IdempotencyKeyDecorator(),
    system_prompt=prompts.build(contract=True, oracle=True),
    sees_oracle=True,
)


CONFIGS: dict[str, AgentConfig] = {
    c.name: c
    for c in (
        NAIVE,
        RETRY,
        REFLECT,
        CONTRACT,
        CONTRACT_IDEM,
        CONTRACT_VERIFY,
        GUARDED,
        ORACLE,
    )
}

#: The six reported in the main grid.
HEADLINE: tuple[str, ...] = ("naive", "retry", "reflect", "contract", "guarded", "oracle")

#: The four arms of the guard-decomposition ablation.
DECOMPOSITION: tuple[str, ...] = (
    "contract",
    "contract+idem",
    "contract+verify",
    "guarded",
)


def get_config(name: str) -> AgentConfig:
    try:
        return CONFIGS[name]
    except KeyError:
        raise KeyError(f"unknown config '{name}'; known: {sorted(CONFIGS)}") from None


__all__ = [
    "CONFIGS",
    "DECOMPOSITION",
    "HEADLINE",
    "AgentConfig",
    "get_config",
]
