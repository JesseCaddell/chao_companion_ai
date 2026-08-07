# Session state

Working notes for picking up where the last session left off. This is a progress
log, not a spec — see `chao-companion-design-v0.2.md` for design and `CLAUDE.md`
for standing conventions. Update this at the end of each session.

## Where we are

**Phase 0 is complete. Phase 1 is started** (brain + director + dashboard —
typed text → LLM → tags → emote fires, dashboard live, anticipation nudge).
Build order is design doc §17: 0 VTS spike → 1 brain + director + dashboard →
2 TTS + motion → 3 aliveness → 4 Twitch → 5 voice → 6 memory → 7 reflection.

`src/chao/events.py` and `src/chao/bus.py` are implemented — `Event`
dataclass, `Kind` string constants, and a `Bus` with two lanes: arbitrated
turns (`input.chat`/`input.voice`/`input.manual`, single in-flight, priority
preemption via cancel token, 15s speech cooldown) and un-arbitrated fan-out
for everything else (including `input.ambient`, deliberately, so aliveness
reactions stay unblocked). 9 tests passing, ruff clean. Added `pytest`,
`pytest-asyncio`, `ruff` as dev dependencies (weren't in `pyproject.toml`
before this session).

**The deferred Opus review of the event/bus design happened at the start of
this session** and found three real bugs beyond the five judgment calls
flagged for review, plus confirmed one of those five was itself a bug:

- Turn-triggering events (`input.chat`/`voice`/`manual`) were never fanned
  out to subscribers — only their derived `decision.*` events were, breaking
  invariant 4's "one stream, three consumers" and losing the raw input a
  prompt re-run would need. Fixed: `publish()` now fans out turn-triggering
  events too, minting `turn_id` at arrival (not selection) so drops
  correlate back to the input that produced them.
- §4.1 chat tiers 4-5 (`CHAT_BACKGROUND`) could still win an empty turn slot
  and produce a full spoken response, contradicting the "no full response"
  spec for those tiers. Fixed: `input.chat` scored `CHAT_BACKGROUND` now
  bypasses arbitration entirely, routed like `input.ambient`.
- Preemption teardown had a 2s grace period but never actually stopped a
  turn that ignored its cancel token past that point — it kept running
  concurrently with the new turn. Fixed: `_await_current_teardown` now
  hard-cancels via `task.cancel()` once the grace period elapses, using
  `asyncio.wait` (not `wait_for`) so the timeout can't be confused with
  cancelling the task itself.
- Cooldown was checked *after* preemption, so a candidate that would itself
  be dropped by cooldown could still tear down the currently running turn
  first. Fixed: cooldown check now runs before the busy/preemption check.
- Kill switch didn't drain the queue, so a backlog could flood through on
  `revive()`. Fixed.

Full outcomes (including the two confirmed-as-designed judgment calls —
ambient bypassing arbitration, cooldown exempting voice/manual) are written
up in `docs/event_bus_design.md`, which no longer carries "pending review"
framing. 13 tests passing (up from 9), ruff clean.

**Environment note for next session:** `uv run` (and pipes like `| tail` or
`| tee`) were extremely slow/unresponsive this session — turned out to be a
stale `.venv/.lock` left by an earlier force-killed `uv` process, not an
actual code hang, and it cost significant time to isolate (thank the
`advisor` catch for it). If `uv run pytest` seems to hang, check for stray
`python`/`uv` processes and a stale `.venv/.lock` before assuming the code
is broken — invoke pytest directly via `.venv/Scripts/python.exe -u -m
pytest` (no pipes) to get a fast, honest signal.

## What Phase 0 proved

Built `src/chao/outputs/vts.py` (thin VTS websocket client: connect, request,
token-cached auth) and `src/chao/tools/vts_probe.py` (standalone diagnostic,
`uv run python -m chao.tools.vts_probe`). Ran it live against the user's VTS
instance and confirmed both output channels end-to-end:

- **Discrete channel** — `ExpressionActivationRequest` (explicit `active`
  state) activates/deactivates a named expression cleanly. Use this, not
  `HotkeyTriggerRequest`, which toggles blindly and left `angry` stuck on
  after an early run.
- **Continuous channel** — `InjectParameterDataRequest` cannot write Live2D
  model parameters directly (VTS error 453). It only writes plugin-created
  custom "tracking" parameters, which then have to be bound *by hand, inside
  the VTS UI*, to a specific Live2D output — per-model, one-time, not
  scriptable via the API. Confirmed working once bound: creating a custom
  param, binding it to `happy` in VTS, and injecting into it drove `happy`
  in isolation (not the ball) — which is correct, since eyes and ball are
  separate parameters, matching the two-channel expression model in
  CLAUDE.md/design doc §6.

**Design doc §19 open question 1, originally resolved "no" this phase, was
overturned by the rigger two sessions later — see below.** At the time:
every position-shaped candidate parameter (`xp`, `xp2`, `xp3`, `yp3`, `yp4`)
was slider-tested directly in VTS with no visible motion, and a live
head-drag/webcam test showed no inherent lag on the ball or its aura either.
Logged as the first entry in `docs/rigging_check_list.md`.

The rig's confirmed parameter groups, for reference:
- Eyes/mood: `happy`, `sad`, `angry`, `confused`
- Ball icon content: `heart`, `questionm`, `surprise`, `swirl`, `emote`
- Ball aura visibility: `bubble`, `bubble2`, `hidebub`
- Ball position (added later, see below): `xp4` (X), `yp5` (Y), `yp6`
  (bubble scale)
- Untested and likely irrelevant: `Param`–`Param5` (auto-generated names,
  never slider-tested — low priority, flagged by advisor as a minor gap in
  full enumeration but not expected to change the conclusion)

## Rigger added a ball physics group (session 3)

The artist added a dedicated physics group to the model: `xp4`/`yp5` (ball
X/Y) and `yp6` (bubble scale, −1..1 shrink/grow, unrelated to position).
User confirmed live in VTS that the group has **its own native lag** —
§8.2's damped spring is now baked into the rig, not something we drive in
code. Design doc §8.2/§8.3, `docs/rigging_check_list.md` item 1, and
CLAUDE.md's `director/motion.py` note are all updated to reflect this.

**Fully closed.** User verified directly: with face tracking off, moving
the model around in VTS makes the ball visibly lag on its own. The physics
group reacts to model movement regardless of what's driving it, so no
plugin-side binding is needed. `docs/rigging_check_list.md` item 1 is
resolved — nothing left to verify before phase 2 on this front.

§8.3's "faster/slower spring response by mood" is no longer achievable —
stiffness/damping are now baked into the model's `.physics3.json` at rig
time, not exposed by the VTS API. Amplitude and posture-offset modulation
are unaffected. Doc updated to say so rather than leave it stale.

## Standing decisions made this session

- Git remote confirmed on `jesse-github` SSH alias (see CLAUDE.md's GitHub
  account check).
- Commit hygiene: batch related changes, one commit per stopping point (now
  in CLAUDE.md).
- Ball spring is real, deliberately designed (§8.2: subtle lag behind head
  motion, ~1/3s settle, not the ball detaching or roaming) — not being
  dropped, just deferred pending a rigging fix.

## Next actions

1. Continue Phase 1: `brain/` (LLM backend protocol + prompt assembly),
   `director/tags.py` (tolerant tag parser) and `director/director.py`
   (wires tag stream → mood/emote decisions), then `__main__.py` to wire a
   real `turn_handler` into `Bus.run()`, then the dashboard skeleton
   (FastAPI + websocket subscriber) so turns are visible live. Anticipation
   nudge (§10.5) needs `brain.request` to fire the question-mark ball pop
   before any audio exists — director-level, not bus-level.
2. Optionally close the minor gap: slider-test `Param`–`Param5` in VTS (low
   priority, quick, not expected to change any conclusion).
