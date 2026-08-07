# Event schema & bus contract

Per CLAUDE.md's working-style rule, changes to the `Event` schema or the bus
contract get an Opus review before implementation, since everything
downstream depends on both. `src/chao/events.py` and `src/chao/bus.py` were
first drafted without that review (the `advisor` tool was repeatedly
overloaded the prior session); the review happened at the start of this
session, against that draft, and the findings below are already applied to
`bus.py`. This file is now a record of the decisions, not a pending item.

## What's implemented

**`events.py`** — `Event` is exactly design doc §13's frozen dataclass
(`id`, `ts`, `turn_id`, `kind`, `payload`). `kind` is a set of plain string
constants (`Kind.BRAIN_TOKEN` etc.), not an `Enum` — deliberate, so it
round-trips through JSON (dashboard websocket, JSONL session log) without a
custom encoder, and compares equal to a bare string in tests/fakes.

**`bus.py`** — single `asyncio.Queue`-driven loop plus a subscriber
fan-out list (each of dashboard / SQLite writer / JSONL log calls
`subscribe()` once, per §11.1's "one stream, three consumers"). Two lanes:

- **Arbitrated**: `input.chat`, `input.voice`, `input.manual` compete for
  one in-flight turn. A strictly higher `Priority` preempts (sets the
  current turn's `asyncio.Event` cancel token, waits up to `teardown_timeout_s`
  (default 2s) for cooperative teardown, then hard-cancels the task if it
  hasn't finished); anything else gets a `decision.dropped` event and is
  discarded.
- **Un-arbitrated / always fanned out**: everything else, including
  `input.ambient` and — as of this review — `input.chat` events scored as
  background tier (§4.1 priority 4-5).

Every turn-triggering event is fanned out to subscribers *as well as* (when
applicable) entering the arbitration queue, with its `turn_id` minted at
`publish()` time so a `decision.dropped` event correlates back to the raw
input that produced it.

## Review outcomes

1. **`input.ambient` outside arbitration — confirmed as designed.** Fanned
   straight through, unblocked, so aliveness reactions (§10.4's ~10:1
   non-verbal-to-spoken ratio) never queue behind or get dropped by whatever
   the brain is doing. Escalation seam for a future "this ambient moment
   deserves a spoken response" trigger: aliveness code re-emits it as
   `input.manual` or a new arbitrated kind. Not built now.

2. **5→3 chat priority tiers — collapse confirmed, but exposed a real bug.**
   Collapsing §4.1's tiers 4-5 ("velocity spike" / "everything else") into a
   single `CHAT_BACKGROUND` priority is fine for ranking purposes, but tiers
   4-5 are *no-full-response* outcomes by design, and the original
   arbitration logic didn't encode that — a `CHAT_BACKGROUND` event arriving
   with an empty turn slot would win arbitration and produce a full spoken
   response, which contradicts the spec. Fixed: `publish()` now routes
   `input.chat` events scored `CHAT_BACKGROUND` straight to fan-out, the same
   as `input.ambient` — they never enter the arbitration queue at all, so
   they can only ever be seen by the director (for an emote-only reaction)
   or the loggers, never spoken. `inputs/twitch.py` (not yet written) is free
   to score chat 1-5 as before; the bus enforces the never-speaks invariant
   for tiers 4-5 regardless of what that module does.

3. **Preemption teardown — the specific stomp fear was unfounded; the real
   bug was different.** The `finally` guard in `_run_turn` (matching on
   `turn_id`) already prevented a stuck old turn from clearing a newer
   turn's bookkeeping. What was missing: after the `teardown_timeout_s`
   grace period, the stuck turn was never actually stopped — it kept running
   concurrently with the new turn, which is exactly what the single-in-flight
   rule exists to prevent. Fixed: `_await_current_teardown` now uses
   `asyncio.wait({task}, timeout=...)`, which — unlike `wait_for` — never
   touches the awaited task's own cancellation, so the timeout firing can't
   be confused with the task being cancelled; only the explicit
   `task.cancel()` once the grace period elapses actually stops it.

4. **Cooldown was checked after preemption.** A higher-priority-than-current
   candidate that itself would be dropped by the speech cooldown was tearing
   down the in-flight turn *before* the cooldown check ran, so the current
   turn was killed and nothing replaced it. Fixed: cooldown is now checked
   before the busy/preemption check, so a candidate that's going to be
   dropped never costs the current turn.

5. **Cooldown exempting voice/manual — confirmed as designed.** Never gate
   the human; a cooldown that ignores you while you're actually talking to
   it would read as broken, not alive.

6. **Injected `turn_handler` — confirmed as designed.** Keeps the bus
   importable and testable with a fake handler, zero dependency on
   phase-1-and-later modules. `__main__.py` (not yet written) wires up the
   real handler.

7. **Turn-triggering events weren't reaching subscribers — fixed.**
   `publish()` previously routed `input.chat`/`voice`/`manual` *only* into
   the arbitration queue, so the dashboard, SQLite writer, and JSONL log
   never saw the raw input — only the `decision.*` events derived from it.
   That broke invariant 4 ("one stream, three consumers") and would have
   broken prompt re-run / the latency waterfall, both of which need the
   triggering input on the record. Fixed: turn-triggering events are now
   always fanned out (in addition to being queued, when they're not
   background-tier chat), with `turn_id` minted at arrival so drops and
   selections both correlate back to a visible input.

8. **Kill switch left a stale backlog for `revive()` — fixed.** `kill()` now
   drains `_queue` in addition to cancelling the in-flight turn, so a
   backlog that arrived while killed can't flood through the instant
   `revive()` runs.

## Still open

None blocking. `docs/rigging_check_list.md` tracks the one open hardware
item (ball position parameter) unrelated to this contract.
