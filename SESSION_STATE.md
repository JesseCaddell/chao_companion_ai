# Session state

Working notes for picking up where the last session left off. This is a progress
log, not a spec — see `chao-companion-design-v0.2.md` for design and `CLAUDE.md`
for standing conventions. Update this at the end of each session.

## Where we are

**Phase 0 is complete.** Build order is design doc §17: 0 VTS spike → 1 brain +
director + dashboard → 2 TTS + motion → 3 aliveness → 4 Twitch → 5 voice →
6 memory → 7 reflection.

**Next up: Phase 1** (brain + director + dashboard — typed text → LLM → tags →
emote fires, dashboard live, anticipation nudge). Not started yet. Per
CLAUDE.md's working-style rule, this phase defines the `Event` schema and the
bus contract, which should get an Opus review before implementation starts —
that review has not happened yet.

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

1. Get an Opus review of the `Event` schema / bus contract shape before
   writing `bus.py` or `events.py` for real (CLAUDE.md working-style rule).
2. Scaffold Phase 1: typed text → LLM → tags → emote fires, dashboard live,
   anticipation nudge.
3. Optionally close the minor gap: slider-test `Param`–`Param5` in VTS (low
   priority, quick, not expected to change any conclusion).
