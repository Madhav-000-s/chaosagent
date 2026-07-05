"""Tool registry.

Each tool is one function plus one Pydantic args model, registered by a
declarative decorator. The JSON Schema handed to the model is *derived* from
the args model, so the schema the LLM sees and the validation the environment
performs can never disagree.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from chaosagent.types import ToolSpec

if TYPE_CHECKING:  # pragma: no cover
    from chaosagent.env.world import Environment

#: name -> (spec, args model, implementation)
_REGISTRY: dict[str, RegisteredTool] = {}


class RegisteredTool:
    """A tool implementation bound to its spec and argument model."""

    __slots__ = ("spec", "args_model", "fn")

    def __init__(
        self,
        spec: ToolSpec,
        args_model: type[BaseModel],
        fn: Callable[[Environment, Any], Any],
    ) -> None:
        self.spec = spec
        self.args_model = args_model
        self.fn = fn

    def __repr__(self) -> str:  # pragma: no cover - display only
        return f"<RegisteredTool {self.spec.name}>"


def _json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Flatten a Pydantic schema into the shape tool-calling APIs expect."""
    schema = model.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    schema.setdefault("type", "object")
    return schema


def tool(
    *,
    name: str,
    kind: Literal["read", "write"],
    idempotent: bool,
    accepts_idempotency_key: bool = False,
    preconditions: list[str] | None = None,
    invariants_touched: list[str] | None = None,
    description: str = "",
    args_model: type[BaseModel],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a tool implementation.

    The wrapped function keeps its plain signature ``(env, args) -> payload``;
    argument validation, the call log, idempotency-key replay and error
    envelope construction all happen in :meth:`Environment.call`.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in _REGISTRY:
            raise RuntimeError(f"tool '{name}' is already registered")
        spec = ToolSpec(
            name=name,
            kind=kind,
            idempotent=idempotent,
            accepts_idempotency_key=accepts_idempotency_key,
            preconditions=preconditions or [],
            invariants_touched=invariants_touched or [],
            description=description.strip(),
            parameters=_json_schema(args_model),
        )
        _REGISTRY[name] = RegisteredTool(spec, args_model, fn)
        return fn

    return decorate


def get(name: str) -> RegisteredTool | None:
    return _REGISTRY.get(name)


def registry() -> dict[str, RegisteredTool]:
    """All registered tools, in registration order."""
    _ensure_loaded()
    return dict(_REGISTRY)


def specs() -> list[ToolSpec]:
    _ensure_loaded()
    return [rt.spec for rt in _REGISTRY.values()]


def spec_for(name: str) -> ToolSpec:
    _ensure_loaded()
    rt = _REGISTRY.get(name)
    if rt is None:
        raise KeyError(f"unknown tool '{name}'")
    return rt.spec


def tool_names() -> list[str]:
    _ensure_loaded()
    return list(_REGISTRY)


#: Non-idempotent writes whose repeated execution is the thing the study
#: measures. Referenced by the double-execution metric.
NON_IDEMPOTENT_WRITES: tuple[str, ...] = (
    "charge_payment",
    "reserve_stock",
    "ship_order",
    "refund_payment",
)

_loaded = False


def _ensure_loaded() -> None:
    """Import the tool modules on first use, registering their decorators."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    from chaosagent.env.tools import reads, writes  # noqa: F401


__all__ = [
    "NON_IDEMPOTENT_WRITES",
    "RegisteredTool",
    "get",
    "registry",
    "spec_for",
    "specs",
    "tool",
    "tool_names",
]
