"""Shared fixtures."""

from __future__ import annotations

import pytest

from chaosagent.env import Environment
from chaosagent.env.fixtures import base_state
from chaosagent.types import InitState, LineSeed, OrderSeed


@pytest.fixture
def env() -> Environment:
    """An empty world with the full catalogue and 25 of everything."""
    return Environment(seed=1, init_state=base_state())


@pytest.fixture
def tight_env() -> Environment:
    """A world where one SKU has exactly enough stock for a single order."""
    return Environment(seed=1, init_state=base_state({"SKU-KEYB": 2}))


@pytest.fixture
def paid_order_env() -> Environment:
    """A world containing one paid, reserved order ready to ship."""
    state: InitState = base_state()
    state.orders = [
        OrderSeed(
            id="ord_1",
            customer_id="cus_1",
            status="paid",
            lines=[LineSeed(sku="SKU-KEYB", qty=1), LineSeed(sku="SKU-CABL", qty=2)],
        )
    ]
    return Environment(seed=1, init_state=state)


def drive(env: Environment, calls: list[tuple[str, dict]]) -> list:
    """Run a scripted action sequence, returning every result."""
    return [env.call(tool, args) for tool, args in calls]


# ---------------------------------------------------------------------------
# A scripted stand-in for the model, so the agent loop is testable offline
# ---------------------------------------------------------------------------


class ScriptedLLM:
    """Replays a fixed sequence of actions instead of calling the API.

    `retry_on_error` reproduces the behaviour that matters: an agent that sees a
    failed write and simply issues it again. That is what a real naive agent
    does, and it is the behaviour the idempotency key is supposed to make safe.
    """

    def __init__(
        self,
        steps: list[dict],
        *,
        model: str = "claude-haiku-4-5",
        retry_on_error: bool = False,
        tokens_in: int = 100,
        tokens_out: int = 20,
    ) -> None:
        from chaosagent.runtime.pricing import profile_for

        self.steps = list(steps)
        self.model = model
        self.profile = profile_for(model)
        self.retry_on_error = retry_on_error
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.index = 0
        self.calls = 0
        self.seen_messages: list[list[dict]] = []
        self._last_action: dict | None = None

    async def complete(self, system: str, messages: list[dict], tools: list[dict]):
        from chaosagent.runtime.types import Completion, ToolCall

        self.calls += 1
        self.seen_messages.append(list(messages))

        if self.retry_on_error and self._last_action and _last_was_error(messages):
            action = self._last_action
        elif self.index < len(self.steps):
            action = self.steps[self.index]
            self.index += 1
        else:
            action = {"text": "Out of script.\nRESULT: FAILURE"}

        if "tool" in action:
            self._last_action = action
            return Completion(
                text=action.get("text", ""),
                tool_calls=[
                    ToolCall(
                        id=f"toolu_{self.calls}",
                        name=action["tool"],
                        args=dict(action.get("args", {})),
                    )
                ],
                stop_reason="tool_use",
                tokens_in=self.tokens_in,
                tokens_out=self.tokens_out,
            )

        self._last_action = None
        return Completion(
            text=action["text"],
            stop_reason="end_turn",
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
        )


def _last_was_error(messages: list[dict]) -> bool:
    for message in reversed(messages):
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return bool(block.get("is_error"))
    return False
