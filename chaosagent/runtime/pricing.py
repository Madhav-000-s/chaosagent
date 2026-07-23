"""Model profiles: pricing, and the per-model request shaping the API forces.

The three tiers used in the model sweep do not accept the same request body, and
pretending otherwise produces 400s rather than results:

* ``temperature`` is **removed** on Sonnet 5 and Opus 5 — sending it is a 400.
  It is still accepted on Haiku 4.5. Rather than branch, the client sends it
  nowhere: reproducibility here comes from the response cache, not from
  sampling parameters, which never guaranteed identical outputs anyway.
* ``effort`` is not supported on Haiku 4.5 and errors if sent.
* Thinking defaults differ. Omitting ``thinking`` on Haiku 4.5 means no
  thinking; on Sonnet 5 and Opus 5 it means *adaptive thinking is on*. Left
  alone, the frontier arm of the model sweep would silently buy a reasoning
  budget the small model never had.

That last one cannot be fully neutralised — Haiku 4.5 cannot do adaptive
thinking, and disabling thinking on Opus 5 has documented failure modes (tool
calls emitted as plain text, which would corrupt this study specifically). The
compromise is to run the thinking-capable models at their lowest effort, and to
state the residual confound in ``docs/LIMITATIONS.md`` rather than hide it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

#: Model IDs used by the experiments. Aliases, not date-suffixed snapshots.
HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-5"
OPUS = "claude-opus-5"


@dataclass(frozen=True)
class ModelProfile:
    """Pricing and request shaping for one model."""

    model: str
    tier: Literal["small", "mid", "frontier"]
    #: USD per million tokens.
    usd_per_mtok_in: float
    usd_per_mtok_out: float
    max_tokens: int
    #: "omit" — send no thinking field (Haiku 4.5: means no thinking).
    #: "adaptive" — thinking is on regardless; bound the cost with effort.
    thinking: Literal["omit", "adaptive"]
    effort: str | None = None

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (
            tokens_in * self.usd_per_mtok_in + tokens_out * self.usd_per_mtok_out
        ) / 1_000_000

    def request_extras(self) -> dict[str, Any]:
        """The model-specific half of a request body."""
        extras: dict[str, Any] = {"max_tokens": self.max_tokens}
        if self.thinking == "adaptive":
            extras["thinking"] = {"type": "adaptive"}
        if self.effort is not None:
            extras["output_config"] = {"effort": self.effort}
        return extras


PROFILES: dict[str, ModelProfile] = {
    HAIKU: ModelProfile(
        model=HAIKU,
        tier="small",
        usd_per_mtok_in=1.00,
        usd_per_mtok_out=5.00,
        max_tokens=2048,
        thinking="omit",
        effort=None,  # not supported on Haiku 4.5
    ),
    SONNET: ModelProfile(
        model=SONNET,
        tier="mid",
        usd_per_mtok_in=3.00,
        usd_per_mtok_out=15.00,
        # Thinking is on by default and shares this ceiling with the response,
        # so it needs more headroom than the Haiku arm.
        max_tokens=6144,
        thinking="adaptive",
        effort="low",
    ),
    OPUS: ModelProfile(
        model=OPUS,
        tier="frontier",
        usd_per_mtok_in=5.00,
        usd_per_mtok_out=25.00,
        max_tokens=6144,
        thinking="adaptive",
        effort="low",
    ),
}

#: Fallback for a model we have no profile for — priced at the frontier rate so
#: an unknown model can never look cheaper than it is.
FALLBACK = ModelProfile(
    model="unknown",
    tier="frontier",
    usd_per_mtok_in=5.00,
    usd_per_mtok_out=25.00,
    max_tokens=4096,
    thinking="omit",
)


def profile_for(model: str) -> ModelProfile:
    return PROFILES.get(model, FALLBACK)


def estimate_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    return profile_for(model).cost(tokens_in, tokens_out)


__all__ = [
    "FALLBACK",
    "HAIKU",
    "OPUS",
    "PROFILES",
    "SONNET",
    "ModelProfile",
    "estimate_usd",
    "profile_for",
]
