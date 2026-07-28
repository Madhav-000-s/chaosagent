# Fault taxonomy

Eight fault classes over four outcome types. For each: the real-world thing it
models, how it is implemented, and what a correct agent would do about it.

The outcome type is the load-bearing part. It separates **did the world execute
the call** from **what did the agent see** — which is precisely the axis silent
corruption lives on.

| Outcome | World executed | Agent sees | Classes |
| --- | --- | --- | --- |
| `suppress` | **yes** | an error | `partial_write`, `timeout` (post-commit) |
| `block` | no | an error | `timeout` (pre-commit) |
| `corrupt` | **yes** | a bad payload | `stale`, `malformed`, `wrong_type`, `silent_empty` |
| `delay` | eventually | *n* errors, then success | `flaky`, `rate_limit` |

A call in the `suppress` row is **the trap**: the world moved and the agent was
told it did not. Every headline metric is a rollup of that condition.

---

## 1. `timeout`

**Real analogue.** A request that exceeds its deadline. The client learns
nothing about whether the server processed it — the ambiguity is the whole
problem, and it is why "retry on timeout" is the most dangerous default in
distributed systems.

**Implementation.** Parameterised by `committed`. On a write with
`committed=true` the environment executes and the response is suppressed
(`suppress`); otherwise the call is blocked before execution (`block`). Reads
always block, because a read has nothing to commit.

**The critical detail:** both variants emit a **byte-identical error envelope**.
`state_may_have_changed` is true for every write regardless of which variant
fired. If the pre-commit envelope said "nothing happened", the agent could read
ground truth straight off the error, and the `guarded` config's advantage would
be an artefact of the harness rather than a property of the interface. A test
asserts this equality directly.

**Correct response.** Verify state before retrying. Never blind-retry a
non-idempotent write, or retry it with the same idempotency key.

---

## 2. `malformed`

**Real analogue.** A truncated response — a connection dropped mid-body, a
proxy buffer limit, a serializer that failed halfway. A truncated JSON payload
and a complete one are indistinguishable to a reader that isn't checking, and
models don't check unless told.

**Implementation.** Two styles, chosen by the seeded RNG. `truncate` serialises
the real payload and cuts it at 35–75% of its length, returning the fragment as
a raw string. `drop_keys` removes one or two *load-bearing* keys — `total_cents`,
`status`, `order_id` — rather than incidental ones, because dropping `created_at`
teaches nothing.

**Correct response.** Detect the shape is wrong, re-call. Do not invent the
missing fields.

---

## 3. `stale`

**Real analogue.** A read served from a replica that hasn't caught up, or from a
cache with no invalidation. Well-formed, plausible, and out of date.

**Implementation.** The injector keeps a snapshot of the world taken immediately
before the most recent write. When `stale` fires on a read, it restores that
snapshot into a scratch environment and replays **the same read** against it.
The result is therefore a genuinely valid earlier view rather than a hand-mangled
one — same keys, same types, older values. If the stale view happens to equal the
fresh one, the injector declines to fire and stays armed, so the run does not
silently become a control.

**Correct response.** Cross-check against another read, or against an action the
agent knows it took. This is the one fault a verification read alone does not
solve — the verification read can itself be stale — which is why it is expected
to be the hardest column in the results table.

---

## 4. `partial_write`

**Real analogue.** The write commits and the response is lost on the way back.
The single most common cause of duplicate charges in production payment systems.

**Implementation.** The environment executes the call normally, then the result
is replaced with an `UNAVAILABLE` envelope carrying
`state_may_have_changed=true`. Writes only.

**Correct response.** An idempotency key, or a verification read. Nothing else
works, because from the agent's side this is *identical* to a pre-commit
timeout — a fact the transparency tests assert.

---

## 5. `wrong_type`

**Real analogue.** A unit mismatch across a service boundary. Cents rendered as
dollars, an integer stringified by a JSON layer that doesn't preserve types.
Schema-valid, semantically wrong.

**Implementation.** Two styles. `cents_to_dollars` divides every `*_cents` field
by 100 and returns a float, so `12900` becomes `129.0` — still a number, still
in a field named `_cents`, now wrong by two orders of magnitude.
`stringify` returns money fields as strings.

**Correct response.** Sanity-check magnitudes against context. Two keyboards do
not cost 258 of anything.

---

## 6. `silent_empty`

**Real analogue.** A query that fails and returns an empty result set instead of
an error — a swallowed exception, a filter that matched nothing because an index
was rebuilding.

**Implementation.** A list payload becomes `[]`; an object payload becomes
`null`. Reads only. The injector declines to fire on payloads that were already
empty, since that would be a no-op.

**Correct response.** Distinguish "no results" from "lookup failed". Given only
this response, that requires a second read.

---

## 7. `rate_limit`

**Real analogue.** HTTP 429 with a `retry_after`.

**Implementation.** `DelayThenSucceed(n)` — the call fails *n* times (n ∈ {1,2},
seeded) and then passes through untouched. The envelope carries the retry delay
in its message and hint.

**Correct response.** Back off and retry. Do not hammer, and do not treat it as
a permanent failure.

---

## 8. `flaky`

**Real analogue.** A transient upstream 503. The service was never reached, so
nothing happened.

**Implementation.** Same `DelayThenSucceed` machinery as `rate_limit`, failing
1–3 times.

**The deliberate contrast.** `flaky` sets `state_may_have_changed=**false**`
while `partial_write` sets it **true**. This is the one transport failure where
blind retry is genuinely correct, and it exists so the results table can
distinguish a configuration that retries *appropriately* from one that treats
every error as dangerous. A config that issues a verification read here is
paying for nothing, and that cost shows up in the efficiency table.

---

## Injection schedules

| Target | Meaning |
| --- | --- |
| `random` | One fault, at a seeded random position within the optimal trajectory. |
| `call_index:N` | One fault, at or after call *N*. |
| `position:early\|mid\|late` | One fault, at a position normalised against the task's optimal length. Powers the position sweep. |
| `tool:NAME` | One fault, on the first call to a named tool. Isolates the non-idempotent-write case. |
| `stochastic` | Each call independently at rate *p*. Faults compound within one trajectory. |

Two policies protect validity, and both are tested:

1. **Faults never fire on a call the world would have refused anyway.** If a
   call fails on its own merits — wrong order status, insufficient stock — the
   refusal passes through untouched and the fault stays armed. Corrupting an
   already-failing call would measure the agent's reaction to the environment
   rather than to the fault.

2. **Single-fault schedules stay armed until an eligible call appears.** A
   `partial_write` scheduled at index 0 of a trajectory that opens with three
   reads would otherwise silently degrade into a control run, contaminating the
   control arm with runs the analysis believes were faulted. Runs where the
   fault never landed are recorded as such and excluded from every faulted-arm
   rate.
