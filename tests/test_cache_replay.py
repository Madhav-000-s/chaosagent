"""A cached run replays exactly, for free, with no network.

This is what makes the reproducibility claim real rather than aspirational: the
released cache re-derives every number without an API key.
"""

from __future__ import annotations

import asyncio

import pytest

from chaosagent.runtime.cache import ResponseCache, request_hash
from chaosagent.runtime.llm import LLMClient, OfflineCacheMiss
from chaosagent.runtime.pricing import HAIKU, OPUS, SONNET, profile_for
from chaosagent.runtime.types import Completion

BODY = {
    "model": HAIKU,
    "system": "you are an agent",
    "messages": [{"role": "user", "content": "hello"}],
    "tools": [],
    "max_tokens": 2048,
}


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def test_hash_is_stable() -> None:
    assert request_hash(BODY) == request_hash(dict(BODY))


def test_hash_ignores_key_order() -> None:
    """Without sorted keys the hit rate would silently depend on dict ordering."""
    shuffled = {k: BODY[k] for k in reversed(list(BODY))}
    assert request_hash(shuffled) == request_hash(BODY)


def test_hash_changes_with_content() -> None:
    other = {**BODY, "system": "you are a different agent"}
    assert request_hash(other) != request_hash(BODY)


def test_hash_separates_models() -> None:
    """Caches are per-model; sharing an entry across models would be wrong."""
    assert request_hash({**BODY, "model": OPUS}) != request_hash(BODY)


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------


def test_round_trip(tmp_path) -> None:
    cache = ResponseCache(tmp_path)
    cache.put("abc123", {"text": "hi", "tokens_in": 5})
    assert cache.get("abc123") == {"text": "hi", "tokens_in": 5}
    assert cache.stats()["hits"] == 1


def test_miss_returns_none(tmp_path) -> None:
    cache = ResponseCache(tmp_path)
    assert cache.get("nope") is None
    assert cache.stats()["misses"] == 1


def test_corrupt_entry_is_a_miss_not_a_crash(tmp_path) -> None:
    """A sweep killed mid-write must not poison later reads."""
    cache = ResponseCache(tmp_path)
    cache.put("k", {"text": "ok"})
    path = cache._path("k")
    path.write_text("{ this is not json", encoding="utf-8")
    assert cache.get("k") is None


def test_disabled_cache_stores_nothing(tmp_path) -> None:
    cache = ResponseCache(tmp_path, enabled=False)
    cache.put("k", {"text": "x"})
    assert cache.get("k") is None


def test_entries_survive_a_new_instance(tmp_path) -> None:
    ResponseCache(tmp_path).put("k", {"text": "persisted"})
    assert ResponseCache(tmp_path).get("k") == {"text": "persisted"}


# ---------------------------------------------------------------------------
# Replay through the client
# ---------------------------------------------------------------------------


def test_cached_completion_replays_without_network(tmp_path) -> None:
    cache = ResponseCache(tmp_path)
    client = LLMClient(HAIKU, cache=cache, offline=True, api_key=None)

    body = client._body("sys", [{"role": "user", "content": "hi"}], [])
    cache.put(
        request_hash(body),
        Completion(text="done", tokens_in=100, tokens_out=20).model_dump(
            exclude={"cache_hit", "prompt_hash", "latency_ms"}
        ),
    )

    result = asyncio.run(client.complete("sys", [{"role": "user", "content": "hi"}], []))
    assert result.text == "done"
    assert result.cache_hit is True
    assert client.api_calls == 0
    assert client.usd_spent == 0.0


def test_replay_is_byte_identical(tmp_path) -> None:
    cache = ResponseCache(tmp_path)
    client = LLMClient(HAIKU, cache=cache, offline=True)
    body = client._body("sys", [{"role": "user", "content": "hi"}], [])
    cache.put(
        request_hash(body),
        Completion(
            text="same", tokens_in=7, tokens_out=3, stop_reason="end_turn"
        ).model_dump(exclude={"cache_hit", "prompt_hash", "latency_ms"}),
    )

    async def once():
        return await client.complete("sys", [{"role": "user", "content": "hi"}], [])

    a = asyncio.run(once())
    b = asyncio.run(once())
    assert a.model_dump(exclude={"latency_ms"}) == b.model_dump(exclude={"latency_ms"})


def test_offline_miss_is_an_explicit_error(tmp_path) -> None:
    """Better a clear failure than a silent live call during a replay."""
    client = LLMClient(HAIKU, cache=ResponseCache(tmp_path), offline=True)
    with pytest.raises(OfflineCacheMiss, match="no cached completion"):
        asyncio.run(client.complete("sys", [{"role": "user", "content": "hi"}], []))


# ---------------------------------------------------------------------------
# Model profiles — these shape the request body and price the run
# ---------------------------------------------------------------------------


def test_no_sampling_parameters_are_ever_sent() -> None:
    """temperature is a 400 on Sonnet 5 and Opus 5. Determinism is the cache's job."""
    for model in (HAIKU, SONNET, OPUS):
        body = LLMClient(model, cache=ResponseCache(enabled=False))._body("s", [], [])
        assert "temperature" not in body
        assert "top_p" not in body
        assert "top_k" not in body


def test_effort_is_only_sent_where_it_is_supported() -> None:
    """Haiku 4.5 errors on `effort`."""
    haiku = LLMClient(HAIKU, cache=ResponseCache(enabled=False))._body("s", [], [])
    assert "output_config" not in haiku
    for model in (SONNET, OPUS):
        body = LLMClient(model, cache=ResponseCache(enabled=False))._body("s", [], [])
        assert body["output_config"]["effort"] == "low"


def test_thinking_is_sent_only_where_it_is_the_default() -> None:
    """Omitting `thinking` means no thinking on Haiku, but adaptive on 5-series."""
    haiku = LLMClient(HAIKU, cache=ResponseCache(enabled=False))._body("s", [], [])
    assert "thinking" not in haiku
    for model in (SONNET, OPUS):
        body = LLMClient(model, cache=ResponseCache(enabled=False))._body("s", [], [])
        assert body["thinking"] == {"type": "adaptive"}


def test_thinking_models_get_more_output_headroom() -> None:
    """max_tokens caps thinking plus response text together."""
    assert profile_for(SONNET).max_tokens > profile_for(HAIKU).max_tokens


def test_pricing_matches_the_published_rates() -> None:
    assert profile_for(HAIKU).usd_per_mtok_in == 1.00
    assert profile_for(HAIKU).usd_per_mtok_out == 5.00
    assert profile_for(SONNET).usd_per_mtok_in == 3.00
    assert profile_for(OPUS).usd_per_mtok_in == 5.00
    assert profile_for(OPUS).usd_per_mtok_out == 25.00


def test_cost_arithmetic() -> None:
    # 1M in + 1M out on Haiku is $1 + $5.
    assert profile_for(HAIKU).cost(1_000_000, 1_000_000) == pytest.approx(6.00)


def test_unknown_model_is_priced_at_the_frontier_rate() -> None:
    """An unknown model must never look cheaper than it might be."""
    assert profile_for("some-future-model").usd_per_mtok_out >= 25.00
