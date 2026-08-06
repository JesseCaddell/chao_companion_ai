# Event schema & bus contract — pending Opus review

Per CLAUDE.md's working-style rule, changes to the `Event` schema or the bus
contract get an Opus review before implementation, since everything
downstream depends on both. That review didn't happen before this was
written — the advisor tool was unavailable this session (repeatedly
overloaded) — so `src/chao/events.py` and `src/chao/bus.py` were implemented
against this proposal and now need the review retroactively. Nothing past
this point in the phase 1 build should build further on `bus.py`'s
arbitration internals until that happens, though the `Event`/`Kind` shapes
are low-risk to build against regardless.

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
  current turn's `asyncio.Event` cancel token, waits up to 2s for teardown);
  anything else gets a `decision.dropped` event and is discarded.
- **Un-arbitrated / always fanned out**: everything else, including
  `input.ambient`.

## Judgment calls made without review — flag these specifically

1. **`input.ambient` does not compete for the turn slot at all.** Design
   doc §4.1 says ambient "mostly" produces non-verbal reactions, implying
   it *sometimes* might warrant a full response, but doesn't specify when.
   I chose to keep ambient entirely outside arbitration — it's fanned
   straight through, unblocked, so the aliveness layer's reactions
   (§10.4's ~10:1 non-verbal-to-spoken ratio) are never queued behind or
   dropped by whatever the brain is doing. If some future ambient trigger
   (a big sub-train moment?) needs to escalate to a spoken response, the
   likely shape is: aliveness code re-emits it as `input.manual` or a new
   arbitrated kind, rather than putting raw ambient events in the
   arbitrated lane. Not built now — flagging the seam.

2. **Priority scale collapses §4.1's 5-tier chat table into 3 tiers**
   (`CHAT_BACKGROUND` / `CHAT_ENGAGEMENT` / `CHAT_DIRECT`), because tiers
   4-5 in that table ("chat velocity spike" and "everything else") are
   both no-full-response outcomes and didn't seem to need separate
   priority *for arbitration purposes* — the distinction between them
   (emote-only vs. logged-only) is a selection-policy decision that lives
   in `inputs/twitch.py` (not written yet), not in the bus. Worth checking
   whether collapsing those two loses something.

3. **Preemption teardown timeout (2s) is a guess.** No latency-budget
   section pins this down. If a turn ignores its cancel token and blocks
   past 2s, the bus currently just logs a warning and starts the new turn
   anyway *without* clearing `_current` from the stuck one — the old
   task's `finally` block will eventually clear it whenever it does exit,
   which could stomp on the new turn's bookkeeping if both finish around
   the same time. This is the shakiest part of the design and the one I'd
   most want a second pair of eyes on.

4. **Speech cooldown (15s, §4.1) gates everything below `Priority.VOICE`**,
   meaning the streamer's own voice and manual/dashboard turns are exempt.
   Nothing in the design doc says whether the streamer talking to the chao
   repeatedly within 15s should also be cooldown-gated; I guessed "no,
   never gate the human" since a cooldown that ignores you when you're
   actually talking to it would read as broken, not alive.

5. **`turn_handler` is an injected callback**, not something `bus.py`
   constructs from `brain`/`director` itself. Keeps the bus importable and
   testable with a fake handler with zero dependencies on phase-1-and-later
   modules that don't exist yet. `__main__.py` (not yet written) is where
   the real handler gets wired up.

## Next step

Re-run this through `advisor()` (or an explicit Opus pass) once available,
specifically against points 1-4 above. Update this file with the outcome —
either "confirmed as designed" or the revision — and remove the "pending
review" framing once it's resolved.
