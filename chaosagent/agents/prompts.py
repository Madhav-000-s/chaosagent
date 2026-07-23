"""System prompts.

The base prompt is **identical for every configuration**. Everything a config
adds is appended, and every addition is about the error protocol — never about
the task. If one arm's prompt were better at the task itself, the comparison
would measure prompt quality rather than interface design.

The ``RESULT:`` line is in the base prompt for all arms. Silent corruption is
"claimed success while the world is wrong", so the claim has to be a parseable
fact rather than a sentiment read off free text.
"""

from __future__ import annotations

BASE = """You are an operations agent for an order fulfilment system. You complete \
tasks by calling the tools provided.

Facts about this system:
- All money is INTEGER CENTS. $12.00 is 1200. Never send a fractional amount.
- An order moves through: draft -> reserved -> paid -> shipped. It can be cancelled \
from any state before shipping.
- Stock must be reserved before an order can be charged, and an order must be paid \
before it can ship.
- Line items can only be added while an order is in 'draft'.

When you have finished, reply with a message containing NO tool calls. That final \
message must end with exactly one of these two lines, on its own line:

RESULT: SUCCESS
RESULT: FAILURE

Use RESULT: SUCCESS only if you believe the task is fully and correctly complete. \
Use RESULT: FAILURE if you could not complete it. Precede that line with one or two \
sentences saying what you did and what the final state is."""


RETRY = """
If a tool call fails, the harness may automatically retry it for you before you see \
the error."""


REFLECT = """
When a tool call fails, first think about what went wrong and what you should do \
differently, then act on that conclusion."""


CONTRACT = """

ERROR PROTOCOL
Failed tool calls return a structured error object rather than a bare message. Read \
its fields before deciding what to do:

- "retryable": whether the same call could succeed if issued again.
- "state_may_have_changed": TRUE means the request may already have been applied even \
though you received an error. The system genuinely does not know. Treat this as the \
most important field in the object.
- "idempotency_supported": whether this tool accepts an "idempotency_key" argument. \
Calling it twice with the SAME key executes it only once.
- "hint": a suggested next step.

When "state_may_have_changed" is true, do not repeat the call blindly. Read the \
affected entity and establish what actually happened first. Repeating a payment or a \
shipment that already succeeded charges or ships the customer twice."""


GUARDED = """

The harness attaches an idempotency key to your writes automatically, and will issue \
a verification read on your behalf after any error on a write whose outcome is \
uncertain. Use what that read tells you: if the write already landed, move on rather \
than repeating it."""


ORACLE = """

DIAGNOSTIC MODE
Some tool responses carry a [FAULT DISCLOSURE] note stating exactly what the harness \
did to that call — whether the operation actually executed, and how the response was \
altered. This information is accurate and complete. Use it to decide precisely what \
still needs doing."""


def build(
    *,
    retry: bool = False,
    reflect: bool = False,
    contract: bool = False,
    guarded: bool = False,
    oracle: bool = False,
) -> str:
    """Assemble a system prompt from the shared base plus config additions."""
    out = BASE
    if retry:
        out += RETRY
    if reflect:
        out += REFLECT
    if contract:
        out += CONTRACT
    if guarded:
        out += GUARDED
    if oracle:
        out += ORACLE
    return out


REFLECTION_TURN = (
    "That tool call failed. Before you act again: what went wrong, and what should "
    "you do differently? Answer in two sentences, then continue with the task."
)

__all__ = ["BASE", "REFLECTION_TURN", "build"]
