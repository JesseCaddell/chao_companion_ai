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

**The required Opus review of this design has not happened yet** — the
`advisor` tool was overloaded every time it was tried this session (4
attempts across the session). Per CLAUDE.md's working-style rule, the
`Event`/bus contract is supposed to get that review before other code
builds on it. User explicitly chose "draft now, review later" when asked.
Full writeup of the judgment calls that need checking is in
`docs/event_bus_design.md` — **read that file first next session** and try
`advisor()` again before writing `brain/`, `director/`, or wiring
`__main__.py` to a real `turn_handler`.

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

**Design doc §19 open question 1 resolved: the ball has no independent
position parameter.** Every position-shaped candidate parameter (`xp`, `xp2`,
`xp3`, `yp3`, `yp4`) was slider-tested directly in VTS with no visible motion,
and a live head-drag/webcam test showed no inherent lag on the ball or its
aura either — the rig doesn't spring it on its own. This means §8.2's damped
ball spring has no parameter to drive; it's a rigging gap, not a code gap.
Logged as the first (only) entry in `docs/rigging_check_list.md`, targeted
for before Phase 2, **not a hard stop** — the user has artist access to get
a position parameter added when it's needed.

The rig's confirmed parameter groups, for reference:
- Eyes/mood: `happy`, `sad`, `angry`, `confused`
- Ball icon content: `heart`, `questionm`, `surprise`, `swirl`, `emote`
- Ball aura visibility: `bubble`, `bubble2`, `hidebub`
- Untested and likely irrelevant: `Param`–`Param5` (auto-generated names,
  never slider-tested — low priority, flagged by advisor as a minor gap in
  full enumeration but not expected to change the conclusion)

## Standing decisions made this session

- Git remote confirmed on `jesse-github` SSH alias (see CLAUDE.md's GitHub
  account check).
- Commit hygiene: batch related changes, one commit per stopping point (now
  in CLAUDE.md).
- Ball spring is real, deliberately designed (§8.2: subtle lag behind head
  motion, ~1/3s settle, not the ball detaching or roaming) — not being
  dropped, just deferred pending a rigging fix.

## Next actions

1. Get the deferred Opus review of `docs/event_bus_design.md` (retry
   `advisor()`), specifically the 5 flagged judgment calls — ambient
   bypassing arbitration, the collapsed 3-tier chat priority, the 2s
   preemption teardown timeout, cooldown exempting voice, and the injected
   `turn_handler` shape.
2. Continue Phase 1: `brain/` (LLM backend protocol + prompt assembly),
   `director/tags.py` (tolerant tag parser) and `director/director.py`
   (wires tag stream → mood/emote decisions), then `__main__.py` to wire a
   real `turn_handler` into `Bus.run()`, then the dashboard skeleton
   (FastAPI + websocket subscriber) so turns are visible live. Anticipation
   nudge (§10.5) needs `brain.request` to fire the question-mark ball pop
   before any audio exists — director-level, not bus-level.
3. Optionally close the minor gap: slider-test `Param`–`Param5` in VTS (low
   priority, quick, not expected to change any conclusion).
