"""Cached async LLM client.

Wraps the Anthropic Messages API with the response cache. A cache hit costs
nothing and returns byte-identical content, which is the whole basis of the
reproducibility claim: the released cache re-derives every number with no API
key at all.

Note what is *not* here: no ``temperature``. It is rejected outright by Sonnet 5
and Opus 5, and on the models that still accept it, ``temperature=0`` never
guaranteed identical outputs anyway. Reproducibility is the cache's job.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Any

from chaosagent.runtime.cache import ResponseCache, request_hash
from chaosagent.runtime.pricing import profile_for
from chaosagent.runtime.types import Completion, ToolCall


class OfflineCacheMiss(RuntimeError):
    """A cache-only client was asked for a completion it does not have."""


class LLMClient:
    """Async, cached, budget-aware wrapper over the Messages API."""

    def __init__(
        self,
        model: str,
        cache: ResponseCache | None = None,
        *,
        api_key: str | None = None,
        offline: bool = False,
        max_retries: int = 4,
    ) -> None:
        self.model = model
        self.profile = profile_for(model)
        self.cache = cache if cache is not None else ResponseCache()
        self.offline = offline
        self.max_retries = max_retries
        self._client: Any = None
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.api_calls = 0
        self.usd_spent = 0.0

    # -- request construction ----------------------------------------------

    def _body(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "system": system,
            "messages": messages,
            "tools": tools,
        }
        body.update(self.profile.request_extras())
        return body

    # -- the one public entry point ----------------------------------------

    async def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Completion:
        body = self._body(system, messages, tools)
        key = request_hash(body)

        cached = self.cache.get(key)
        if cached is not None:
            return Completion(**cached, cache_hit=True, prompt_hash=key, latency_ms=0)

        if self.offline:
            raise OfflineCacheMiss(
                f"no cached completion for prompt {key[:12]} (model={self.model}). "
                "Run with an API key to populate the cache, or use a released cache."
            )

        started = time.perf_counter()
        completion = await self._call_api(body)
        latency_ms = int((time.perf_counter() - started) * 1000)

        payload = completion.model_dump(
            exclude={"cache_hit", "prompt_hash", "latency_ms"}
        )
        self.cache.put(key, payload)
        self.usd_spent += self.profile.cost(completion.tokens_in, completion.tokens_out)
        return completion.model_copy(
            update={"prompt_hash": key, "latency_ms": latency_ms, "cache_hit": False}
        )

    # -- the API ------------------------------------------------------------

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:  # pragma: no cover - install-time issue
                raise RuntimeError(
                    "the 'anthropic' package is required for live runs; "
                    "install with: pip install -e '.[run]'"
                ) from exc
            if not self._api_key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is not set. Put it in .env (see .env.example) "
                    "or run offline against a populated cache."
                )
            self._client = AsyncAnthropic(api_key=self._api_key, max_retries=self.max_retries)
        return self._client

    async def _call_api(self, body: dict[str, Any]) -> Completion:
        import anthropic

        client = self._ensure_client()
        last: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = await client.messages.create(**body)
                self.api_calls += 1
                return _parse(response)
            # Most specific first. The SDK already retries 429/5xx internally;
            # this outer loop covers the case where it exhausts its own budget.
            except anthropic.RateLimitError as exc:
                last = exc
            except anthropic.APIStatusError as exc:
                if exc.status_code < 500:
                    raise  # 400/404 are our bug, not the server's — fail loudly.
                last = exc
            except anthropic.APIConnectionError as exc:
                last = exc

            if attempt < self.max_retries:
                delay = min(2**attempt + random.random(), 30.0)
                await asyncio.sleep(delay)

        raise RuntimeError(f"LLM call failed after {self.max_retries + 1} attempts: {last}")


def _parse(response: Any) -> Completion:
    """Turn an SDK response into our own frozen type."""
    # Check the stop reason before touching content: a refusal has none.
    if getattr(response, "stop_reason", None) == "refusal":
        detail = getattr(response, "stop_details", None)
        category = getattr(detail, "category", None) if detail else None
        return Completion(
            text="",
            stop_reason="refusal",
            refusal=str(category or "unspecified"),
            tokens_in=getattr(response.usage, "input_tokens", 0),
            tokens_out=getattr(response.usage, "output_tokens", 0),
        )

    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in response.content:
        kind = getattr(block, "type", None)
        if kind == "text":
            text_parts.append(block.text)
        elif kind == "tool_use":
            tool_calls.append(
                ToolCall(id=block.id, name=block.name, args=dict(block.input or {}))
            )
        # thinking blocks are intentionally ignored: they are never shown to the
        # environment and never scored.

    return Completion(
        text="\n".join(text_parts).strip(),
        tool_calls=tool_calls,
        stop_reason=str(response.stop_reason or "end_turn"),
        tokens_in=response.usage.input_tokens,
        tokens_out=response.usage.output_tokens,
    )


__all__ = ["LLMClient", "OfflineCacheMiss"]
