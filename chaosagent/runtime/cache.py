"""Response cache — what makes replay free and CI possible.

Every LLM call is keyed by a hash of the exact request body. A cached run
replays bit-exact at zero cost, which is what lets the whole analysis be re-run
after a metric bug without re-spending, and what lets CI run a smoke eval with
no network.

**The seed is deliberately not part of the key.** It reaches the cache
indirectly and correctly: a different seed places the fault at a different call,
which changes the observations, which changes the messages, which changes the
key. Two seeds that produce byte-identical prompts *should* share an entry —
including the seed directly would triple API spend to re-derive answers already
known.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_CACHE_DIR = Path(os.environ.get("CHAOSAGENT_CACHE_DIR", "traces/cache"))


def request_hash(payload: dict[str, Any]) -> str:
    """Stable hash of a request body.

    ``sort_keys`` is load-bearing: an unsorted dump would make the key depend on
    Python's dict ordering and silently halve the hit rate.
    """
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ResponseCache:
    """Content-addressed store of completions, one JSON file per entry."""

    def __init__(self, directory: Path | str = DEFAULT_CACHE_DIR, *, enabled: bool = True) -> None:
        self.dir = Path(directory)
        self.enabled = enabled
        self.hits = 0
        self.misses = 0
        self.writes = 0
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Two-level fan-out; a flat directory of 10k+ files is miserable on
        # Windows and slow to list anywhere.
        return self.dir / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        path = self._path(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A truncated entry from an interrupted write is a miss, not a crash.
            self.misses += 1
            return None
        self.hits += 1
        return data

    def put(self, key: str, value: dict[str, Any]) -> None:
        if not self.enabled:
            return
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a killed sweep must not leave a half-written entry
        # that later reads as a corrupt hit.
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, sort_keys=True, indent=None), encoding="utf-8")
        tmp.replace(path)
        self.writes += 1

    def __contains__(self, key: str) -> bool:
        return self.enabled and self._path(key).exists()

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "writes": self.writes}

    def entry_count(self) -> int:
        return sum(1 for _ in self.dir.rglob("*.json")) if self.dir.exists() else 0

    def __repr__(self) -> str:  # pragma: no cover - display only
        return f"<ResponseCache {self.dir} hits={self.hits} misses={self.misses}>"


__all__ = ["DEFAULT_CACHE_DIR", "ResponseCache", "request_hash"]
