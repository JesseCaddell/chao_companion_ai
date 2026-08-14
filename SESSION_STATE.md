# Session state

Working notes for picking up where the last session left off. This is a progress
log, not a spec — see `chao-companion-design-v0.2.md` for design and `CLAUDE.md`
for standing conventions. Update this at the end of each session.

## Stopping point (session 10 part 5, 2026-08-13, live-verified)

§8.1 envelope-driven motion, picked back up after the previous session was
cut short mid-task by context budget. Followed that session's own
next-steps list in order: re-ran the suite and ruff cold before touching
anything (both clean — 181 passed, confirming the unfinished code didn't
break anything, just wasn't covered yet); wrote the missing tests; called
`advisor` before going live, which caught a real bug the tests as first
written couldn't see; fixed it; live-verified twice against real VTS and
the real trained voice; tuned by ear per user feedback. Now committed-ready
(see below for what's still open).

**Real bug caught by `advisor` before any live run, not found live:**
`VTSMotionPlayer.play` was sleeping a full `frame_dt` *after* each
injection call instead of only the remainder of that frame's deadline. A
real `InjectParameterDataRequest` costs ~17ms against a ~33ms budget at
30fps, so the clip would have run ~1.5x longer than the audio it's
supposed to track, drifting further out of sync as a sentence goes on —
and since `Speaker._play_with_motion` gathers both tasks,
`output.speech_end` (and therefore the next queued sentence, via
`_drain_speech`) would have waited on the overrun too. **Fixed:** paced to
a fixed deadline schedule (`start + (i+1)*frame_dt`, `remaining = deadline
- clock()`, sleep only if positive) instead of a per-frame fixed sleep.
`clock` is now an injectable field on `VTSMotionPlayer` (defaults to
`time.monotonic`, same as `sleep`'s existing shape) so tests can pace
deterministically. Confirmed the fix matters, not just theoretically: ran
the old logic standalone against the same inputs used in the new
discriminating test — it overran to 0.218s where the paced version lands
on 0.150s, for a 4-frame/17ms-cost clip. The test that would have caught
the original bug and didn't (`sleep.durations == [1/30, 1/30]`, satisfied
by exactly the code that overruns live) was replaced with one that pins
the deadline behavior using a `FakeClock`/`ClockAdvancingSleep` pair, the
same fake-clock shape already established in `mood.py`'s and
`aliveness.py`'s tests.

**Second, smaller fix from the same advisor pass:** `__main__.py`'s
`_attach_motion` had `except VTSAPIError: pass`, meant to treat "parameter
already exists" as expected — but a bare `except` there swallows *any*
`VTSAPIError`, including a genuine failure that would leave the code
injecting into a parameter nothing is bound to (the exact silent-failure
mode the function's own docstring already warned about, just not yet
guarded against). Now prints `error_id`/`message` instead of discarding
them; not narrowed to the specific duplicate-name error ID yet since that
number hadn't actually been observed until this session's live run gave
it.

**21 new tests** (11 `tests/test_director_motion.py`, 6 added to
`tests/test_outputs_vts.py` for `VTSMotionPlayer`, 4 added to
`tests/test_outputs_speech.py` for `Speaker`'s concurrent-motion path),
**203 total passing**, ruff clean, `ruff format` clean.

**Also found and fixed, unrelated to the motion code itself:** the
user's trained voice's config file was on disk as `chao_voice.json`, but
Piper's own loader (`PiperVoice.load`) hard-requires
`<model_stem>.onnx.json` next to the `.onnx` weights — confirmed by a
direct load attempt raising `FileNotFoundError` for
`chao_voice.onnx.json` specifically. Not a code bug; the training
pipeline (a separate, python-3.10 environment per the user, mentioned
in-conversation — see the environment note below) just doesn't name its
output the way Piper's runtime expects. Fixed by copying (not moving)
`chao_voice.json` → `chao_voice.onnx.json` in `data/voices/`, so the
original stays in place in case the training tooling itself expects that
name. Both files are gitignored, same as every other `data/voices/*`
asset — nothing to commit for this.

**Live-verified twice against real VTS and the real `chao_voice.onnx`
voice**, via a scratch script (not committed, same throwaway spirit as
prior sessions' probes) that imports and calls the actual production
functions (`chao.__main__._build_speaker`, `_attach_motion`, the real
`Speaker`/`PiperBackend`/`VTSClient`) rather than reimplementing the
wiring:

- **First run** (`amplitude: 15`, the untested placeholder inherited from
  the original design): user confirmed **synced well, smooth** (not the
  "very jerky" raw two-point snap from session 10 part 4's test), but
  **not maxed out the whole time** yet also read as visually tame overall.
- **User's call: amplitude should be exaggerated, not conservative** —
  "for an animated character, exaggerated motion is a plus, not a
  negative." Bumped `motion.amplitude` **15 → 25** (of the parameter's
  bound ±30 range) in both `config/chao.yaml` and `MotionConfig`'s
  dataclass default, with the reasoning recorded inline in both places so
  a future reader doesn't have to rediscover why 25 and not, say, the full
  30 (left a little headroom under the hard limit rather than pinning
  exactly at it).
- **Second run at amplitude 25:** user confirmed **much better** — bigger,
  more expressive range (values now spanning roughly single digits up to
  ~25 on emphasized syllables, vs. mostly pinned near 14-15/15 before) —
  **with one real, not-yet-chased finding: some small jerks mid-sentence.**
  Not investigated further this session (same "record it, don't chase
  every finding immediately" discipline as session 10 part 4's own jerky-
  snap note) — candidate causes for whoever picks this up: per-frame RMS
  quantization becoming more visible at higher amplitude, occasional real
  injection round trips running long enough to compress a frame's
  `remaining` sleep to ~0, or something else entirely. Not reproduced
  against a controlled signal, so no root cause claimed yet.
- **Real tuning data point, not a guess:** actual synthesized-clip RMS
  measured at ~0.10-0.14 across the two runs (the exact sentence, real
  voice) — close to the `reference_rms: 0.1` placeholder, so that value
  wasn't far off blind. Left unchanged this session; flagged as the next
  knob to consider if the mid-sentence jerks turn out to be a
  clamping/saturation artifact rather than a timing one (peak hit 1.0 on
  both runs, meaning louder syllables are already saturating the
  normalization).
- **User asked for a note, not a build:** a live/dashboard control for
  `motion.amplitude` (and likely other `motion:` tuning fields) so this
  doesn't require an edit-yaml-and-rerun-a-script loop every time it needs
  adjusting by ear — exactly the "changed by feel, at runtime, often" case
  CLAUDE.md's config conventions name explicitly, but with no runtime path
  today. Added to design doc §11.2 as a session-10 note against the
  existing override-panel item (item 8), not built — the dashboard has
  only the event-feed panel so far (§11.2 item 3), and this needs that
  override panel to exist first.

**Committed** (`08215e4`) once the suite/lint/live-check/tuning above were
all done, per this project's established per-part-commit discipline
(visible in recent git log — `71d5854`, `30df346`, `d6be08c` are each one
part of this same session). `data/voices/` stayed untracked/uncommitted,
same as every session that's touched it — gitignored voice model assets,
not source.

### Session 10, part 6: `uv run` trampoline fix

Machine-wide since sometime this session: every documented CLAUDE.md
command that goes through a generated console-script shim
(`uv run pytest`, `uv run ruff ...`, and the raw `.venv\Scripts\*.exe`
files themselves) failed with `error: uv trampoline failed to canonicalize
script path`. Worked around all of the above sessions by calling
`.venv\Scripts\python.exe -m pytest` / `-m ruff` directly (which always
worked fine — `.venv`'s own interpreter is real and healthy, Python
3.14.7, matching `pyproject.toml`'s `requires-python = ">=3.11"`). User
flagged wanting this tidied up, with one hard constraint: whatever the fix
was, it must not touch the separate Piper voice-training environment/
Python install elsewhere on the machine.

**Root cause narrowed, not fully proven:** `uv run python --version`
worked the whole time — so `uv run` itself and the venv's real interpreter
were never broken, only the small (46080-byte) trampoline stub `.exe`s uv
generates per console-script entry point. Isolated further: trampolines
from the *original* `uv sync` (8/7 batch — `pytest.exe`, `dotenv.exe`,
`py.test.exe`, etc.) were **all** broken; trampolines generated later, when
`piper-tts` was added on 8/11 (`piper.exe`, `onnxruntime_test.exe`), **all**
worked fine, tested directly. Ruled out a `uv` version change as the cause
— only one `uv` version (0.12.2) has ever been installed via scoop on this
machine, and the interpreter directory the venv's `pyvenv.cfg` points at
(`cpython-3.14.7-windows-x86_64-none`) hasn't been touched since the
initial 8/7 sync either. So the breakage isn't "uv itself changed" or "the
interpreter moved" — something invalidated that first batch of shim files
specifically, sometime after they were created, while later-created ones
were unaffected. Best guess, unconfirmed: AV/backup software rewriting
those particular files post-creation is the usual culprit for this class
of Windows-specific uv issue. Not chased further since the fix below is
cheap, verified, and doesn't depend on knowing the exact trigger.

**Fix:** `uv sync --reinstall`, run from this project's directory only.
Forces uv to reinstall all 49 packages into **this project's own `.venv`**,
regenerating every trampoline from scratch. Verified this satisfies the
user's constraint before running anything: it only writes inside
`D:\PycharmProjects\chao_companion_ai\.venv`, touches neither global PATH
nor scoop's shared uv-managed Python installs, and `git status` after the
reinstall showed zero tracked-file changes (only `data/voices/`
untracked, as always) — nothing here can reach the Piper training
environment, which lives in a separate directory entirely.

**Verified fixed:** `uv run pytest -q` → 203 passed; `uv run ruff check` →
all checks passed; `uv run ruff format --check src tests` → all formatted.
Matches exactly what the `.venv\Scripts\python.exe -m` workaround had been
reporting all session, confirming the workaround was never masking a real
code problem — it was purely the trampolines.

**Also investigated, no action needed:** the user separately asked whether
moving a Piper-training environment down to Python 3.10 was a global PATH
change. Found two Python 3.10 installs on PATH — a Microsoft Store 3.10.11
via an App Execution Alias (very early in PATH) and a standalone
python.org 3.10.2 install (much later in PATH, plausibly the one set up
for Piper training). The Store alias was already winning bare `python`
resolution before either install mattered, and the standalone 3.10 entry
never actually takes priority for that command. **No PATH change was made
or needed** — this project never relies on bare `python` on PATH, only
`uv run` / `.venv\Scripts\python.exe`, both of which correctly resolve to
this project's own 3.14.7 interpreter regardless of what `python` alone
points to system-wide.

### Session 10, part 7: head/body dual-output parameter expansion

User's own direction, not a code task: "finish adding the manual
parameters" — `ChaoHeadBob`/`ParamAngleY` (item 4) was the only bound
axis; face left/right and tilt were still unbound, and the body-angle
question from item 4 ("no separate body-only channel needed") hadn't
accounted for the rigger's actual original design.

**User's own finding, from VTS's tracking-parameter mapping, reframed the
plan mid-stream:** the rigger's design has each `FaceAngle*` tracking
input fan out to *two* simultaneous outputs at once (e.g. `FaceAngleX` →
both `ParamAngleX` and `ParamBodyAngleX`, same input driving both). First
pass (mine, before this was raised) created 5 separate parameters — one
per head axis, one per body axis — assuming code would need to inject
matching values into two params per axis. User's clarifying question
established VTS actually lets *one* plugin-created custom parameter bind
to two output curves at once, same as a native tracking input can —
collapsing the design to 3 parameters, each with two outputs, correctly
matching the rigger's fan-out instead of approximating it in code. The 3
now-redundant body-only parameters (`ChaoBodyBob`/`ChaoBodyTurn`/
`ChaoBodyTilt`) were deleted before anything got bound to them.

**Final state, all live-verified:**

| Custom parameter | Output 1 | Output 2 |
|---|---|---|
| `ChaoHeadBob` (existing from part 4, second output added) | `ParamAngleY` | `ParamBodyAngleY` |
| `ChaoHeadTurn` (new) | `ParamAngleX` | `ParamBodyAngleX` |
| `ChaoHeadTilt` (new) | `ParamAngleZ` | `ParamBodyAngleZ` |

Verified via a scratch hold-test script (same resend-every-0.3s pattern
as `vts_probe.py`'s `hold_parameter`, since VTS drops an injected
parameter if it isn't resent within ~1s): all three parameters injected
at both +25 and -25 of their ±30 range, six holds total. User confirmed
all six — "resounding success." Full writeup, including the superseded
reasoning from item 4, in `docs/rigging_check_list.md` item 5.

**Not built this pass, deliberately:** nothing drives `ChaoHeadTurn` or
`ChaoHeadTilt` yet — this was VTS-side setup only, same "left bound,
ready to use" treatment `ChaoHeadBob` got in part 4 before §8.1's code
existed. `ChaoHeadBob`'s existing driver (`VTSMotionPlayer`) needed no
code change: it already injects one named parameter, and picks up the
new second output for free. The natural first consumer for the two new
axes is the idle-drift work flagged as the likely next step before this
detour (design doc §10.1/§10.2) — previously blocked on exactly this,
now unblocked.

### Session 10, part 8: idle drift (§10.1/§10.2) — built, rejected live, redesigned, locked in

User picked idle drift as the next step (the two new head axes from part
7 were built specifically to unblock it). Before writing code, called
`advisor` with a design sketch; its four points all got addressed:
X/Z-only (never `ChaoHeadBob`, sidesteps the "is the chao speaking"
coordination problem entirely — confirmed the right call), a rate floor
high enough to keep per-step deltas sub-perceptual, sharing the one
lock-serialized VTS connection rather than opening a second, and a
ramp-to-rest on every exit path.

**`attack_s` fix, landed first, live-confirmed:** before idle drift
itself, `advisor` diagnosed the "small jerks mid-sentence" finding
flagged back in part 5's amplitude tuning: `InjectParameterDataRequest`
doesn't interpolate, so `motion.py`'s old `attack_s: 0.03` closed ~63% of
a silence-to-loud onset's gap in a single frame — a real snap, not
smoothing. Raised to `0.10` (both `config/chao.yaml` and
`MotionConfig`'s dataclass default). **User confirmed live, unprompted:**
"the chao speak looks so good! Feels alive!" — signed off, not revisited
further this session.

**First idle drift version: a continuous Ornstein-Uhlenbeck noise
process** (`director/idle_drift.py`'s `ou_step`/`clamp`, a
`VTSIdleDriftPlayer.run` injecting both `ChaoHeadTurn`/`ChaoHeadTilt`
together in one call per frame at 10Hz, `_attach_idle_drift` mirroring
`_attach_motion`'s recreate-on-startup pattern). 17 new tests, full suite
green, live-verified twice (idle alone, then idle + speech together) with
zero errors on either run.

**User's verdict, watching it live, was a real rejection, not a tuning
note:** "the idle drift needs some work. Its micro jerks are noticeable
and it doesn't look good." Root cause, in hindsight obvious:
`InjectParameterDataRequest` doesn't interpolate (the same fact that
drove the `attack_s` fix above), so *every* OU step, however small, was a
real visible snap — continuous noise was fundamentally the wrong
mechanism for a non-interpolating parameter, not a mistunable one.
User's own replacement design: **"slow movement from point A to point B,
not micro nudges... the drift can be extreme... the more animated the
more fun this character becomes... it's a drawing and masterfully done
art."**

**Redesigned around that, `advisor`-reviewed again before writing code**
(confirmed the read was right, then added specifics): `director/idle_drift.py`
rewritten around a stateful `IdleDrift` class (same clock/rng-injection
shape as `Fly`, deliberately — `tick()` reads `self.clock()` itself, no
`now` parameter) implementing a two-phase state machine — hold at a
target (genuinely static, the property the user actually asked for), ease
to a freshly-drawn target via a hand-computed `smoothstep` curve (VTS
won't interpolate this parameter, so the code does), hold again.
`VTSIdleDriftPlayer` simplified to a thin shell calling `idle_drift.tick()`
each frame, same split as `extract_envelope`/`VTSMotionPlayer`. `ou_step`/
`clamp` and their tests deleted outright, not left around as dead code.

Four specifics from the second advisor pass, all incorporated:
- **Split `x_amplitude`/`z_amplitude`** (22/12), not one shared bound —
  tilt reads stronger per degree than turn on most rigs; an unconstrained
  random 2D target could land at extreme-tilt-plus-extreme-turn and read
  as broken rather than expressive.
- **`min_move_distance` (8.0) redraw** — without it, an occasional
  near-adjacent target draw produces an ease indistinguishable from an
  extra hold, a dead 10+ second stretch. Same latent issue as
  `Fly._maybe_reposition`'s, worse here since point-to-point motion is
  the primary behavior, not a rare event.
- **Exact-target snap on late ticks** — a tick arriving well past the
  ease's deadline (real jitter, or a test jumping the clock in one leap)
  must land exactly on target, not extrapolate past it. Pinned by a
  dedicated test that jumps the clock 50x past the ease duration in one
  call and asserts the value is still exactly the target.
- **`amplitude` is now a target-selection *bound*, not a clamp on a
  continuous process** — different semantics from the field it replaced,
  called out explicitly in both the code comment and `config/chao.yaml`
  so a future reader doesn't assume otherwise.

New tests replaced the old ones one-for-one in kind, not just in count:
determinism checked across *several* phase transitions (target and
duration draws interleave, so one transition wouldn't catch an ordering
bug), hold-is-static checked via bit-identical repeated `tick()` values
(the actual property being tested for), min-move-distance and amplitude
bounds checked across 30 simulated transitions. One real test bug found
and fixed along the way: repeatedly advancing a `FakeClock` by exactly
`0.1` accumulated floating-point rounding error, occasionally landing
`elapsed` just under the stored `phase_duration` and producing a spurious
duplicate — fixed by advancing with clear margin (`0.15`) past each
boundary instead of exactly at it. Full suite: 222 passed, ruff clean,
format clean.

**Live-verified, first pass at `ease_s=[1.5,4.0]`/`hold_s=[3.0,8.0]`,
`x_amplitude=22`/`z_amplitude=12`:** ran idle alone for 45s (advisor:
pacing failures only show up across multiple cycles, watch 3-4), then
idle + speech together, then idle alone again post-speech — all three
legs clean, no errors. **User: "wow. That is the best it has looked so
far. Love it."**

**Immediate follow-up ask, still live-tuning:** "the idle movements are
significantly slower than the movements while chao is talking... build
an allowance for increase of speed." Not a redesign — shortened
`ease_s`/`hold_s` ranges roughly 2.5x (`[0.6,1.5]`/`[2.0,5.0]`), same
amplitude/min-move-distance, in both `config/chao.yaml` and
`IdleDriftConfig`'s dataclass defaults. Re-verified live (user asked for
a second run after missing the first one mid-speech). **User: "BOOM!
Looks great! lock it in!"** — final, no further tuning pending.

**Second ask, logged not built:** the user wants the LLM (once wired to
chat/voice, phase 4/5) to be able to direct where the chao looks "when it
wants," not have idle drift run as a fully unattended script forever.
This is the same arbitration question already flagged in part 3
(`[look:chat]`/`[look:you]` tags vs. an aliveness-owned focus state), now
sharpened by having a real `IdleDrift` state machine to arbitrate
against. Logged as a design doc §10.3 note with three candidate
arbitration shapes, no decision made — nothing real exists yet to drive
it with (no live chat/voice input), so there's nothing to test against
this session. See design doc §10.3.

Committed (`f788a67`), same per-part-commit discipline as the rest of this
session.

*(§8.1's implementation details — `director/motion.py`, `VTSMotionPlayer`,
`Speaker`'s concurrent-motion path — are fully described in the "session
10 part 5" stopping point above, which supersedes an earlier, more
tentative draft of this same writeup that used to live here. That draft
predated this session's testing/advisor pass/live-verification/tuning and
is removed rather than kept as stale duplicate detail.)*

### Session 10, part 9: physical kill switch (§15)

User picked Twitch chat as the next build. Before writing any chat code,
orientation surfaced that the selection-policy arbiter and kill switch
*mechanism* already exist fully built and tested in `bus.py` (`Bus.kill()`/
`revive()`) — what's actually missing for "Twitch chat" is narrower than it
sounded: the real IRC connection, priority scoring, and the kill switch's
*physical trigger*. Consulted `advisor` before scoping the plan; its
guidance reshaped two things and this part covers the first:

- **`twitchio` dropped in favor of raw IRC over the existing `websockets`
  dependency** — 2.x and 3.x use incompatible auth flows and there was no
  way to know which `uv add twitchio` would pull; raw IRC needs no new
  dependency and matches this project's established "read the raw protocol"
  precedent (VTS). Not yet implemented — this part is kill switch only.
- **Build the kill switch first, not alongside Twitch** — smaller,
  self-contained, needs no Twitch dependency at all, and per design doc §15
  it has to exist *before* adversarial chat input goes live, not after.

**New file `src/chao/inputs/killswitch.py`**: `KillSwitch` binds two global
hotkeys via `pynput.keyboard.GlobalHotKeys` (`kill_hotkey`/`revive_hotkey`,
configurable, default `<ctrl>+<alt>+k`/`<ctrl>+<alt>+r`) to `bus.kill()`/
`bus.revive()`. `bus.kill()` already did the real work — no `bus.py` changes
needed — it sets the in-flight turn's cancel token, which is the *same*
token already threaded through `AudioPlayer.play`'s 20ms poll loop and
`Speaker.speak`'s own cancel check. Confirmed that chain by reading
`brain/turn.py`/`outputs/speech.py`/`outputs/audio.py` before writing any
code, per advisor's explicit push-back on an unverified assumption in the
first draft plan.

**Thread safety, per advisor**: pynput's listener fires callbacks on its own
OS thread, not the asyncio loop — the same class of exception CLAUDE.md's
no-threads rule already carves out for `sounddevice`'s audio callback.
`KillSwitch.start()` captures `asyncio.get_running_loop()`; the hotkey
callbacks marshal onto it via `call_soon_threadsafe`, catching `RuntimeError`
for the shutdown-race case (hotkey pressed while `main()`'s teardown already
closed the loop) — the press is just dropped, nothing left to kill.

**New event kind, additive only**: `Kind.STATE_KILLED` (`{killed: bool}`),
same shape as `STATE_FLY`. Checked before adding it that this doesn't touch
the Event schema/bus contract the way CLAUDE.md's escalation trigger means —
`bus.publish` fans out non-turn-triggering kinds immediately regardless of
`_killed`, so `state.killed` reaches subscribers (dashboard, logs) even
while the kill is in effect.

**Config**: new `kill_switch:` block in `config/chao.yaml`, `KillSwitchConfig`/
`load_kill_switch_config` following the same load/assemble split as every
other config in this project.

**Wired into `__main__.py`**: started in `main()` immediately after
`orchestrator.speaker` is set, before any other task — live before any input
source can trigger a turn. Stopped in the shutdown `finally`, ahead of the
existing quit-time `bus.kill()` call.

**Library choice verified live, not assumed**: advisor flagged this as the
one thing that had to be checked empirically — `pynput` vs `keyboard` differ
on whether Windows global hooks need admin. A standalone hotkey-only script
was inconclusive (user couldn't tell if it fired); rather than debug that in
isolation, went straight to the real end-to-end live check below, which
settles the same question unambiguously since it uses the actual `pynput`
listener.

**12 new tests** (`tests/test_inputs_killswitch.py`): hotkey binding, kill
blocking a subsequent *arbitrated* turn (via a real `bus.run()` task, not
just publish-and-inspect — an earlier draft of this test was a false
negative for exactly that reason, since nothing dequeues without `bus.run()`
running, caught before committing), `STATE_KILLED` payload shape both
directions, revive genuinely unblocking (asserts the handler actually ran,
not just "no drop event"), `stop()`-before-`start()` safety, the closed-loop
`RuntimeError` swallow, and `load_kill_switch_config`'s three cases (block
present / file missing / block absent). Full suite: **233 passed**, ruff
clean, format clean.

**Live-verified end-to-end** — real `pynput` listener, real Piper synthesis,
real audio playback (VTS not needed for this check, audio/bus wiring only):
spoke a long sentence, killed it with `Ctrl+Alt+K` mid-sentence. **User:
"It killed it immediately. split second response."** Confirmed via the
script's own event log: `speech_start` at 5.99s, `STATE_KILLED killed=True`
at 7.48s (roughly 1.5s into the sentence, well before it would have
finished) — matching what was heard. A subsequent turn attempt was dropped
(`dropped: killed`) while still killed. `Ctrl+Alt+R` published
`STATE_KILLED killed=False`, and the very next turn actually ran and spoke
to completion (`speech_end`, not dropped) — proving `revive()` genuinely
restores normal operation, not just that `kill()` alone works.

**No admin privileges required** — confirmed empirically by the live check
succeeding from a normal terminal, settling the open question from
advisor's review without needing a separate isolated script.

`uv add pynput` (pulls `six` as a transitive dep).

Kill switch complete before Twitch chat work begins, per advisor's explicit
ordering call and design doc §15. Next: raw IRC chat client.

### Session 10, part 10: raw IRC Twitch chat client (§4.1)

Second and final piece of "Twitch chat." Consulted `advisor` again before
writing code, with a concrete design already sketched from orientation.
Its review caught two things that would have been silent live-run failures
and confirmed the rest of the design was sound:

- **Handshake ordering.** Twitch can silently ignore a `JOIN` sent before
  the server finishes its connection-registration sequence — `CAP REQ`
  first, then `PASS`/`NICK`, then wait for `001` (`RPL_WELCOME`), *then*
  `JOIN`. Not something review alone could settle — advisor's explicit
  instruction was to verify it against a real channel before writing tests
  or the real client, not assume. Did exactly that (see below).
- **Mention detection false-positives.** The first-draft plan used
  substring matching (`"chao" in text`), which fires on "chaos", "chaotic",
  etc. — an immediate problem given `chao` is the actual configured name.
  Fixed with a word-boundary regex (`\bchao\b`) before any code was
  written, not discovered live.
- **`chat_spike` needs its own cooldown, confirmed by re-reading
  `aliveness.py`'s own docstring:** `Aliveness` deliberately doesn't share
  `Director`'s cooldown state, and unlike the anticipation nudge (paced by
  turn cadence), `chat_spike` fires once per qualifying chat message —
  several times a second during a real spike with no gating otherwise.
  Real, not theoretical: the live check below hit 223 spike-tier messages
  in 15 seconds.

**Verified against real Twitch traffic before writing the regex or tests**
(advisor: "answerable by connecting to a real channel and printing raw
lines, not by review") — a throwaway probe script (not committed)
connected anonymously to a busy public channel and printed ~30s of raw
IRC output. Confirmed the handshake sequence live (`CAP * ACK` → `001`...
`376` welcome block → `JOIN` → `353`/`366` NAMES → real `PRIVMSG` lines)
and that the tag+PRIVMSG regex matches real captured lines, not just
hand-written ones.

**New file `src/chao/inputs/twitch.py`**:
- `parse_tags`/`parse_privmsg` — pure functions, regex verified against
  real traffic (see above).
- `score_priority(text, chao_names)` — §4.1 tiers 1/2/5 from message text
  alone. Tier 3 (high-affinity viewer) needs the phase-6 affinity system
  to score at all; folded into tier 5 with a documented gap rather than
  guessed at.
- `VelocityTracker` — tier 4, a rolling-window message-rate counter
  (`window_s`/`threshold`, clock-injectable like `Fly`/`IdleDrift`).
  Config's own comment originally called this "untestable against real
  traffic on a quiet channel" — turned out very testable against a *busy*
  one instead (see live check below).
- `TwitchChatClient` — raw IRC over the existing `websockets` dependency,
  not `twitchio` (dropped last part: 2.x/3.x have incompatible auth flows
  with no way to pin which `uv add twitchio` would resolve to). Anonymous
  read (`justinfanNNNNN`, no password) by default — this build is chat
  *input* only, no send-back, so **no bot account or OAuth token is
  required** for what got built this pass. `TWITCH_OAUTH_TOKEN`/
  `TWITCH_BOT_USERNAME` in the environment switch to an authenticated
  connection if send-back is ever built later — the user's own bot-account
  setup from earlier in this session isn't wasted, just not yet needed.
  `connect` is injectable (same shape as `PiperBackend`/`SoundDeviceSink`)
  so tests don't open a real socket. No reconnect-on-drop logic —
  degrades the same way `_run_vts_subscriber` does on VTS-unavailable:
  the task ends, chat input goes quiet until restart. Deliberately not
  built this pass, same "no real consumer yet" discipline as earlier
  sessions' cuts.
- `TwitchConfig`/`load_twitch_config` — same load/assemble split as every
  other config in this project. New `twitch:` block in `config/chao.yaml`
  (`channel: ""` disables chat input entirely, same degrade-not-crash
  shape as `tts.voice_path` unset).

**`director/aliveness.py`: `chat_spike` finally has a consumer.**
`reactions.chat_spike: surprise` had sat unused in `emotes.yaml` since
session 8. `Aliveness.handle` now also reacts to `input.chat` events with
`priority == 4`, refactored into `_fire` (anticipation, unchanged —
deliberately ungated, confirmed by not breaking the existing
`test_does_not_share_cooldown_with_tag_triggered_curious_emotes`) and a
new `_fire_with_cooldown` (chat_spike only) sharing an emote-publish
helper. A real regression was caught and fixed before committing: an
earlier draft applied the same cooldown check to *both* reactions,
which broke that exact existing test — anticipation's whole design point
is firing on every single turn regardless of the `curious` pool's own
cooldown, so the fix scoped cooldown tracking to chat_spike specifically,
not generically to `_fire`.

**Wired into `__main__.py`**: `_run_twitch(bus, twitch_config)`, same
degrade-gracefully shape as `_run_vts_subscriber` (missing channel config
→ print and return; connection failure → catch `OSError`/`RuntimeError`,
print, return). Added to the task list and shutdown cancel/gather in
`main()`, no new dependency (`websockets` already there).

**19 new tests** (`tests/test_inputs_twitch.py`): tag/PRIVMSG parsing
(including the non-message-line rejection cases), `score_priority`'s
word-boundary fix specifically (`"chaos"` must not match `"chao"`),
`VelocityTracker`'s spike/no-spike/window-expiry cases, `load_twitch_config`'s
three cases, and `TwitchChatClient` end-to-end against a `FakeWebSocket`
(handshake order both anonymous and authenticated, PING/PONG, per-message
event shape, spike-tier upgrade applying to background chat but *not* to
an existing mention). **6 new tests** in `test_director_aliveness.py` for
the `chat_spike` wiring and the anticipation/chat_spike cooldown
independence. Full suite: **264 passed**, ruff clean, format clean.

**Live-verified end-to-end against real Twitch, real busy channel, no
credentials** — the actual production `TwitchChatClient`/`TwitchConfig`,
not a reimplementation: connected anonymously, watched for 15s, printed
every published `input.chat` event's tier. Real results: **`{1: 0, 2: 5,
4: 223, 5: 7}`** — zero mentions (none said "chao" in this window,
expected, not a failure), 5 real questions correctly tiered, and the
velocity tracker correctly upgraded 223 of 235 messages to spike tier
once the rolling window filled past threshold on a genuinely busy
channel — the exact case the config comment had called "untestable"
before this check. Handshake, PING/PONG, and PRIVMSG parsing all
confirmed working against real traffic, not just the fake-websocket
tests. (One console-only snag, not a code bug: the throwaway live-check
script's own `print()` crashed twice on Windows' cp1252 stdout hitting
emoji in real chat text/display names — fixed in the script with an
ascii-safe preview encode; the actual pipeline never prints raw chat text
to console, so this doesn't apply to production code.)

**Design doc §4.1's Twitch-chat phase is now functionally complete** for
what this pass scoped: real connection, real parsing, real priority
scoring feeding the already-built arbiter (`bus.py`), and a real non-verbal
consequence (`chat_spike`) for the one tier that previously had none.
Deliberately not built this pass: chat output filtering/username
sanitization beyond `display_name`-preferred (already existed in
`brain/turn.py`), a global rate-limit backstop, and per-prompt/response
session logging beyond what `Kind.BRAIN_COMPLETE`/`OUTPUT_SPEECH_*`
already give the dashboard/JSONL log — all still open per design doc §15,
not yet scoped with the user this pass.

Not yet committed at time of writing.

Picked up with a custom Piper voice (`.onnx`, user-trained) ready for
testing, plus a review of the not-yet-implemented
`docs/tts_pronunciation_overrides.md` design note.

**Pronunciation fix implemented and wired in.** `outputs/tts.py` now has
`_fix_pronunciation`/`_PRONUNCIATION` (currently just `chao`→`chow`,
`chao's`→`chow's`), applied inside `PiperBackend.synthesize` right before
the text hits Piper. Caught a real bug in the design doc's own draft regex
(`r"\b[\w']+\b"`) before implementing: it swept a trailing quote-apostrophe
into the match (`'chao'` → token `chao'`, dict miss, fix silently didn't
fire). Shipped `r"\b[A-Za-z]+(?:'[A-Za-z]+)?\b"` instead — apostrophe only
*between* letters. Doc updated to "Implemented," code sample corrected, bug
noted inline so it doesn't get silently re-copied later. 6 new tests.

**TTS wired into the live pipeline** — the bigger, deliberately-deferred
piece from session 9. Consulted `advisor` before designing, per this
project's established pattern for anything touching turn.py's
cancellation-critical path (invariant 5). Its guidance shaped the whole
design; two points mattered most:

- **TTS input source:** `Speaker` (new, `outputs/speech.py`) is fed cleaned
  sentences directly by `TurnOrchestrator` as `Director.process_chunk`
  produces them — **not** a bus subscriber. Nothing on the bus carries
  cleaned/tag-stripped sentence text (`brain.token` is deliberately raw;
  `brain.complete.full_text` is also raw and arrives too late for §12's
  sentence-streaming budget). Adding a `director.sentence` event kind to
  carry it would have been an Event schema change — an explicit
  CLAUDE.md Opus-escalation trigger — for something a direct method call
  does with no schema change at all, since `OUTPUT_SPEECH_START/END`
  already existed in `events.py` with their payload shape already
  specified in design doc §13.
- **Turn lifetime honesty:** `TurnOrchestrator.__call__` pushes each
  cleaned sentence onto a per-turn `asyncio.Queue`, drained by a separate
  task (`_drain_speech`) so synthesis/playback never stalls token
  streaming or emote firing — but `__call__` still *awaits that task*
  before returning (in a `finally`, sentinel-terminated, so it can't hang
  and always runs even on the cancel-branch early return). Reasoning:
  `bus.py`'s arbiter treats a returned handler as "turn over" — returning
  while audio is still playing would let a new turn start speaking over
  the old one and would start the 15s speech cooldown from the wrong
  moment.

**New files:**
- `outputs/audio.py` — `AudioPlayer` (cancellable playback: starts via
  `sd.play`, polls `cancel` on a 20ms interval calling `sd.stop()` the
  instant it fires, rather than the obvious-but-wrong
  `run_in_executor(None, sd.wait)`, which would just move the
  uninterruptible wait onto a thread instead of removing it — invariant 5
  again). `resolve_output_device(name_substring)` finds VB-Audio Virtual
  Cable (or any device) by case-insensitive name match, `None` (system
  default) if unset/not found — same graceful-degrade shape as
  `_run_vts_subscriber`'s "VTS unavailable" handling. Sink is injectable
  (`SoundDeviceSink` real impl, fakes in tests) — no PortAudio needed to
  run the suite.
- `outputs/speech.py` — `Speaker.speak(sentence, *, turn_id, cancel)`:
  synthesizes via `PiperBackend`, publishes `output.speech_start` (payload:
  **original** spelling + `duration_ms`, never the pronunciation-fixed
  text — confirmed by test, since that event feeds the dashboard/session
  log/eventual episodes table and a leaked "chow" would become
  self-reinforcing per the pronunciation doc's own invariants), plays via
  `AudioPlayer`, publishes `output.speech_end` — skipped if playback was
  cut short by cancellation rather than completing, so a `speech_end` never
  implies "this was actually heard." No-ops on blank text (a tag-only
  sentence cleans to `""`) or an already-cancelled turn. Also holds
  `TTSConfig`/`load_tts_config` (`voice_path`, `output_device`, read from
  `config/chao.yaml`'s new `tts:` block) — voice/device are config per
  CLAUDE.md, and this is specifically what makes the custom-voice swap
  (see below) a config-only change, per session 8's plan.

**Changed files:**
- `brain/turn.py` — `TurnOrchestrator` gets a `speaker: Speaker | None =
  None` field (`None` = not configured, same graceful-degrade shape as
  everything else optional in this pipeline); `__call__` wired per the
  queue/drain design above.
- `__main__.py` — `_build_speaker(tts_config, publish)` constructs a real
  `Speaker` from `config/chao.yaml`, or returns `None` with a printed
  warning if `voice_path` is unset or the file doesn't exist (mirrors
  `_run_vts_subscriber`'s degrade-not-crash handling). Wired via
  `orchestrator.speaker = _build_speaker(...)` after `build_pipeline()`
  returns, since building a real `Speaker` needs `bus.publish`, which
  doesn't exist until `build_pipeline()`'s internal `Bus()` does.
- `config/chao.yaml` — was completely empty; now holds the `tts:` block
  (`voice_path: data/voices/en_US-amy-medium.onnx`, `output_device: null`
  i.e. system default). This is the file the custom voice gets pointed at.
- `uv add sounddevice` — PortAudio bindings, CPU-only, no GPU (invariant
  1). Its playback is the "audio callback" case CLAUDE.md's no-threads
  rule already carves out.

**Deliberately not built this pass**, same "only what has a real consumer"
discipline as session 9's timescale loop: §8.1 envelope extraction (RMS@60Hz
→ body bob/head nod) — no bound VTS parameters exist for bob/nod yet, same
phase-0 manual-binding blocker as everything else in that category;
`AudioChunk.samples` is already the right seam, untouched. Also not
touched: rescheduling `director.py`'s emote firing onto the audio playback
clock (`_maybe_fire_emote`'s long-documented seam) — now *possible* since
`speech_start` is real, but changes emote timing that's already visually
confirmed working, so left as a separate, deliberate future change.

**Verified without playing real audio through the user's speakers
unprompted:** imported the real wiring path end-to-end against the actual
`config/chao.yaml` and the existing `en_US-amy-medium.onnx` — `_build_speaker`
constructs a real `Speaker` successfully; confirmed graceful `None` +
warning for both an unset `voice_path` and a `voice_path` pointing at a
nonexistent file.

**Live-verified with real audio, user-confirmed, after the two fixes
below:** `config/chao.yaml`'s `voice_path` swapped to the user's own
trained voice (`data/voices/chao_voice.onnx`, gitignored same as
amy-medium). Ran a scratch script exercising `Speaker.speak()` directly —
normal playback plus a cancel-mid-sentence case — three times total as the
user iterated on the voice model itself (first pass used voice checkpoint
v2, final pass v4; the `.onnx.json` config wasn't reissued between
checkpoints, which worked fine — that pairing is normally stable across
weight-only retraining). All three runs: `output.speech_start` →
`output.speech_end` with matching `duration_ms` on the completed sentence,
and **no** `speech_end` published for the sentence cancelled mid-playback
(confirming `Speaker.speak`'s completed-vs-cut-short distinction holds for
real, not just in the fake-sink tests). User confirmed by ear on the final
run: correct custom voice, "chao" and "chao's" both pronounced correctly
("chow"/"chow's"), and the long sentence audibly cut off partway rather
than playing to completion. **User signed off — TTS pipeline wiring is
fully live-verified, not just unit-tested.**

**Second advisor pass, after the design above landed, caught two real gaps
and one worth a comment:**
- **Fixed:** a `speaker.speak()` failure (bad voice file, PortAudio device
  error — the single most likely failure mode right now, given the next
  step is pointing `voice_path` at a brand-new custom onnx) would have
  propagated out of `__call__`'s `finally` *after* `brain.complete` already
  published successfully, making a fully-successful text turn report as a
  crashed one. `_drain_speech` now catches, publishes a `Kind.ERROR` with
  `turn_id` (visible in `_print_errors`/the dashboard), and stops trying
  for the rest of that turn — the next turn gets a fresh attempt.
- **Fixed:** `resolve_output_device` returning `None` on a *requested but
  not found* device name was silent — unlike `_run_vts_subscriber`'s "VTS
  unavailable" precedent it claimed to follow. Live impact if it ever
  fires: the chao's voice comes out the user's speakers instead of into
  OBS via the virtual cable (and back into the mic path if monitored),
  with nothing anywhere explaining why. Now prints a warning before
  falling back.
- **Noted, not changed:** `AudioPlayer`/`SoundDeviceSink` ride sounddevice's
  one module-level stream, so `stop()` in `play()`'s `finally` stops
  *whatever* is currently playing, not specifically that call's clip. Fine
  today (one `Speaker`, strictly sequential playback); flagged in
  `audio.py`'s docstring so a future second concurrent caller doesn't
  assume isolation it doesn't have.

25 new tests (6 pronunciation, 5 `outputs/audio.py`, 8 `outputs/speech.py`,
6 `brain/turn.py` speaker-wiring), full suite at 179 passed, ruff clean.
Not yet committed — pronunciation fix and TTS wiring are two logically
separate units; plan is to land them as two commits, not one, per CLAUDE.md's
commit-hygiene note (batch *related* changes, not unrelated ones together).
`pyproject.toml`/`uv.lock`'s `sounddevice` addition belongs with the wiring
commit, not the pronunciation one.

### Session 10, part 2: fly `x_range` eyeball check, plus a Y-axis and size scan

Closed out session 9's other flagged loose end: `fly.x_range`'s exact
bounds hadn't been confirmed by eye against the real OBS-capture framing,
only proven not to error via a raw API probe. Used scratch scripts (not
committed — one-off diagnostics in the same spirit as `tools/vts_probe.py`
but not generalized into a permanent tool) issuing the same
`MoveModelRequest` shape `VTSFlySubscriber` actually uses, so what got
eyeballed is exactly what a real flight looks like.

- **`x_range` widened and locked: `[-0.4, 0.4]` → `[-0.6, 0.6]`.** Started
  at the session-9 placeholder (confirmed comfortable, ~2in margin each
  side), tested `[-0.6, 0.6]` next — user confirmed fully on screen with a
  "perfect" glide both ways — and stopped there rather than pushing
  further untested. `config/emotes.yaml` updated with the new bounds and a
  session-10 comment explaining the eyeball process, since the session-9
  comment's "conservative placeholder pending visual check" framing was
  now stale.
- **New finding, not previously known: `size` is not a stable baseline
  value.** While chasing a `positionY`-vs-frame-edge question (see below),
  `CurrentModelRequest` samples of `size` disagreed across a few minutes
  (-81.4 twice back-to-back, then -87.1 later) even though `positionY`/
  `rotation` stayed bit-identical across the same samples. Reads as VTS's
  own idle animation subtly breathing the zoom, not this project's code.
  Corrected `docs/rigging_check_list.md` item 2, which an earlier draft
  this same session had written up as "fixed" based only on the first two,
  coincidentally-close samples — flagged explicitly so the wrong version
  doesn't get treated as settled. **Real implication for later:** a future
  "chao flies closer to camera" feature can't snapshot `size` once and
  treat it as *the* default; needs a live re-sample or a relative delta.
- **Vertical range explored and recorded, not wired into code.**
  `Fly`/`VTSFlySubscriber` still only move `positionX` — this was pure
  reconnaissance for a possible future fly-Y feature, at the user's
  request. First pass tested ±0.3 from the model's natural resting
  baseline: comfortable margin on top, bottom ran close to the frame edge.
  User asked to re-test from a **centered** `(0, 0)` position instead of
  that natural baseline; ±0.3 from center under-used the room, ±0.5 gave
  good margin both directions and was confirmed ("perfect... consider it
  locked"). Written up as `docs/rigging_check_list.md` item 3, explicit
  that it's a recorded reference value only — no `y_range` config key was
  added to `emotes.yaml`, since nothing reads one yet and an unused config
  field would misleadingly imply the feature exists. Also flagged the
  asymmetry for whoever builds fly-Y later: `x_range` is relative to the
  model's *natural resting position*, `y_range`'s reference point here was
  a *centered* one — those aren't the same baseline, and a real
  implementation needs to pick one deliberately.

Committed (`71d5854`).

### Session 10, part 3: attention model attempted, redirected to the subtitle overlay instead

User asked to build §10.3's attention model next. Before writing code,
checked it against what actually exists live and found the same "no real
consumer" problem that shaped every cut earlier this session, just
bigger: all four triggers lack a live source (streamer-speaks needs
`input.voice`, phase 5; chat velocity/name-mention need real `input.chat`,
phase 4 — neither built), and the output side has no bound VTS
look-direction parameter either (confirmed rig groups are eyes/mood, ball
icon/aura, ball position — nothing look-shaped), so per the phase-0
finding it'd need a manual create-and-bind in VTS before any code could
drive it regardless. Raised this with the user rather than building a
state machine with no live inputs and nowhere to send its output.
Consulted `advisor` first to pressure-test that read before raising it —
confirmed, and flagged one more thing worth settling in design whenever
this does get built: `[look:chat]`/`[look:you]` already exist as
LLM-emitted tags producing `director.tag` telemetry, and an
aliveness-owned focus state would want to drive the same look direction —
a real arbitration question, not just an implementation detail.

User's call: step back to design doc §17's actual phase order rather than
jumping ahead. Re-checked phase 2 (design doc says phase 3/aliveness
should follow a *finished* phase 2) against what's actually built:
TTS/audio routing done this session, ball spring resolved earlier, but
§8.1 envelope-driven motion (RMS→body bob/head nod) is unbuilt and has the
identical VTS-binding blocker as the attention model's look parameter, and
the subtitle overlay was flagged `TBD` in the design doc and never built.
Personality/voice test segment was arguably already satisfied by the
existing typed-input loop now that TTS is wired, just not confirmed as
such. Offered the three options; user picked the subtitle overlay as the
one with zero blockers.

**Built:** `src/chao/dashboard/subtitles.html` — a plain static page
(no build step, no framework, deliberately not part of the Vite dashboard
app) meant as an OBS Browser Source URL, stream-facing rather than
streamer-only. Connects to the same `/ws/events` the dashboard already
relays (a second independent subscriber, publishes nothing) and reacts to
two already-real event kinds: `output.speech_start` shows
`payload.sentence` (the *original* spelling — the same TTS-only
pronunciation fix invariant from earlier this session applies here too,
transitively, since `Speaker` never publishes the rewritten text) with a
hide-timer set to `payload.duration_ms`; `brain.request` (a new turn
starting) force-clears whatever's showing immediately. That second rule
exists specifically for the case a sentence gets cancelled mid-playback
and never receives its own `speech_end` (this session's earlier TTS-wiring
design, deliberately) — without it, that sentence's subtitle would sit on
screen until its own hide-timer eventually expired, up to its full
`duration_ms` later, regardless of what (if anything) was actually
playing. `dashboard/server.py` serves it at `/subtitles` (registered
before the dashboard's catch-all static mount, same ordering rationale as
the existing `/ws/events` route). 2 new tests (route serves the page;
route and `/ws/events` share one bus). Full suite at 181 passed, ruff
clean.

**Live-verified in a real browser**, not just unit-tested — a scratch
FastAPI+Bus process publishing a timed event sequence, watched via
Chrome automation (`claude-in-chrome`, which needed a reconnect mid-session
— an environment hiccup, not a code issue). Confirmed by screenshot: text
renders correctly (transparent page background, white text with a black
outline, bottom-centered, legible), replaces cleanly across sentences, and
the cancelled-sentence case actually works — a sentence published with a
20s `duration_ms` was fully cleared well before that timeout, immediately
after a simulated new-turn `brain.request`. No console errors. Timing
across the two attempts drifted noticeably from the script's nominal
delays (background-process startup lag, tool round-trip overhead) — noted
in case a future live check needs generous timing margins again, not
worth chasing further since the behavior itself was still caught cleanly
on-screen.

Committed (`30df346`).

### Session 10, part 4: §8.1's motion parameter — confirmed live, and a real design collapse

User asked to bind the body-bob VTS parameter next, continuing straight
into phase 2's last unbuilt piece (§8.1 envelope-driven motion) now that
the subtitle overlay closed out the item before it. Same create → bind →
inject procedure as `vts_probe.py`'s existing `happy` test, run live
against the real rig, not a new permanent tool.

- **`Live2DParameterListRequest` dump** surfaced real candidates:
  `ParamBodyAngleX/Y/Z` (standard Live2D body-rotation params, range
  ±10) — distinct from the ball's `xp4`/`yp5`/`yp6` group and
  `ParamBreath` (VTS's own native idle breathing, already confirmed not
  ours to drive, session 9).
- **First test, `ParamBodyAngleY`: created a custom parameter, user bound
  it, injected ±10.** Read back via `Live2DParameterListRequest` — the
  raw value updated correctly both times. **User reported zero visible
  motion**, confirmed twice (once with `faceFound: false`, once
  `faceFound: true`, ruling out a tracking-confidence-gating
  explanation). A real negative finding, not a test bug — same category
  as session 3's original dead ball-position candidates.
- **User's own diagnostic broke the case open:** checked VTS's tracking-
  parameter mapping directly and found `FaceAngleY` (one tracking input)
  fans out to *two* outputs at once — `ParamAngleY` ("Face up/down
  rotation", head) and `ParamBodyAngleY` ("Body Rotation Y"). Live face
  tracking moves both simultaneously, which reads as "the whole character
  bobbing" but is actually `ParamAngleY` doing the visible work — exactly
  consistent with the first test's finding that `ParamBodyAngleY` alone
  has no visual effect.
- **Second test, `ParamAngleY` alone: created a second custom parameter
  (`ChaoHeadBob`), user bound it, injected ±30 (full range).** User
  confirmed real visible motion — "right on the money" — of **both head
  and body together**, from one signal.
- **Real design implication, not just a rigging note:** §8.1 as originally
  spec'd routes two independent outputs (body bob, head nod) from one
  envelope. On this rig that collapses to **one** — this character's
  proportions mean the body already visually follows the head in the art,
  with no separate body-only channel to drive (`ParamBodyAngleX/Y/Z` need
  no binding, no code attention, unless the rigger adds real body
  deformation to them later). Design doc §8.1 updated with a session-10
  note; `docs/rigging_check_list.md` gets a new item 4 with the full
  writeup, including the two-step reasoning chain so a future reader
  doesn't have to rediscover it.
- **`ChaoHeadBob` deliberately left bound in VTS, not cleaned up** —
  unlike every other throwaway test parameter this session (created,
  tested, deleted), this one is the real, confirmed-working setup §8.1's
  actual implementation can inject into directly. No further VTS-side
  setup should be needed before that code gets written.
- **Noted, not chased further:** injections in both tests snapped directly
  between two discrete values ("very jerky," per the user) — expected,
  since the test used `mode: "set"` two-point steps, not §8.1's actual
  planned continuous ~60Hz envelope with attack/release smoothing already
  baked in before injection. Attributed to the test method, not the
  parameter/binding, but flagged as an assumption a real implementation
  should confirm rather than inherit unverified.

Not yet committed (`docs/rigging_check_list.md` item 4, the §8.1 design
doc update, this note).

## Stopping point (end of session 9, 2026-08-11)

Picked up session 8's TTS thread: Piper is now actually installed, not just
chosen.

- **`piper-tts` added as a project dependency** (`uv add piper-tts`) — pulls
  in `onnxruntime` CPU-only (no CUDA extras), consistent with invariant 1.
- **Voice: `en_US-amy-medium`**, the user's explicit placeholder pick.
  Downloaded via `python -m piper.download_voices` into `data/voices/`
  (`.onnx` + `.onnx.json`, ~63MB). Gitignored (`data/voices/*.onnx*`) —
  same treatment as `chao.db`/`vts_token.txt`: large, regenerable, not
  source. User is separately compiling/preparing a custom childlike voice
  in parallel (referenced a `shift_voice.py` tool outside this repo); that
  slots in later as a config-only swap, per session 8's plan.
- **`src/chao/outputs/tts.py` (new)** — `PiperBackend`, following the same
  injectable-client shape as `AnthropicBackend`/`OllamaBackend` so tests
  don't need a real model loaded. `synthesize(text)` is an async generator
  yielding one `AudioChunk` (mono float32 PCM + sample rate) per sentence —
  Piper's own `PiperVoice.synthesize` already sentence-splits, so no
  splitting logic needed here. The actual `onnxruntime` inference call is
  blocking/CPU-bound, so it's run via `loop.run_in_executor`, not awaited
  directly — keeps the rest of the bus (VTS, dashboard, aliveness) live
  while a sentence renders. 4 new tests (fake voice, no real Piper
  load), all passing; full suite at 126 passed, ruff clean.
- **Verified for real, not just unit-tested:** ran `PiperBackend.synthesize`
  against the actual downloaded `en_US-amy-medium` model end-to-end (a
  three-sentence string), got back three real audio chunks at 22050Hz —
  confirms the async/executor wrapping and the `AudioChunk` shape both work
  against genuine Piper output, not just the fake.
- **Deliberately not done yet:** `tts.py` is not wired into `turn.py` or
  `__main__.py`. Nothing calls `PiperBackend` from the live pipeline, no
  `output.speech_start`/`end` events are published, and there's no
  `outputs/audio.py` yet for virtual-cable playback. That's real pipeline
  wiring (turn.py's cancellation-critical path, per invariant 5) and a
  bigger, separate step — this session only unblocks it by giving
  `motion.py`'s §8.1 envelope extraction a real, concrete `AudioChunk`
  shape to consume, which was the actual point of doing Piper now rather
  than later.

### Session 9, part 2: the fly state machine + horizontal screen positioning

User redirected `motion.py` scope before it was built: breathing is VTS-
native (confirmed by the user), idle sway is low priority, and the real
ask was **the chao deciding to fly to different horizontal positions on
screen**. Consulted the `advisor` tool before designing (per CLAUDE.md's
"changing the Event schema" escalation trigger) — its guidance shaped
everything below, in particular "probe `MoveModelRequest` before designing
around it," which resolved the single biggest open question.

- **Live probe result (the architectural fork):** `MoveModelRequest`
  genuinely **interpolates** over `timeInSeconds` — does not snap. Raw
  `CurrentModelRequest` samples mid-move showed positionX moving from
  baseline (~-0.026) through -0.24 (t=0.5s) and -0.36 (t=1.0s) to -0.40
  (t=2.2s, settled), for a request targeting -0.4 over 2.0s. This means
  "fly to a new spot" is **one API call per destination** — no 60Hz tween
  loop, no `motion.py` needed for this feature at all.
- **`aliveness.py`: `Fly` (new)** — §6.4's on/off hysteresis state machine,
  pure and `FakeClock`-testable like `mood.py`. Driven by `director.mood`
  (arousal, for on_above/off_below) and `director.tag` (an unconditional
  "land on `[sad]`" hard rule that bypasses both the threshold and
  `min_dwell_s`). While already flying, a `director.mood` arrival that
  doesn't trigger landing is also the (rate-limited via `min_reposition_s`)
  trigger to consider a new horizontal target — reuses an existing,
  activity-tied signal instead of a periodic timer, per §10.2's "noise not
  pacing" spirit. Publishes `state.fly` (§13's existing kind, no schema
  change — just an added `target_x` field: the new x when `flying`, `None`
  when landing, read by the VTS side as "return to rest").
  - **Known, documented limitation:** no periodic tick, same as `Mood`.
    `director.mood` only fires when a tag nudges mood, so in a genuinely
    quiet stretch a flying chao won't re-evaluate arousal and won't land
    on its own. Needs §10.1's timescale loop to fully close — a separate,
    larger piece of work, not attempted this session.
- **`outputs/vts.py`: `VTSFlySubscriber` (new)** — turns `state.fly` into
  a `MoveModelRequest` (positionX from the event, Y/rotation/size held at
  a baseline captured once via `CurrentModelRequest` on first use) plus an
  `ExpressionActivationRequest` toggle for the `fly` visual itself. Unlike
  `VTSEmoteSubscriber`'s pool emotes, the fly expression is a **sustained**
  toggle with no auto-deactivate timer — §6.1 lists `fly` as its own
  `state` channel, not a transient eyes/ball emote, so CLAUDE.md's "never
  sustain an authored emote beyond ~4s" doesn't apply to it. Shares the
  existing VTS websocket connection with `VTSEmoteSubscriber` rather than
  opening a second one — both now live inside `_run_vts_subscriber`.
- **`config/emotes.yaml`'s `fly:` block extended:** `x_range: [-0.4, 0.4]`
  (conservative placeholder — -0.4 confirmed reachable and glided
  correctly, but the exact visible window edges haven't been checked by
  eye against the real OBS-capture framing), `move_duration_s: 2.0`,
  `land_duration_s: 1.5`, `min_reposition_s: 8.0`.
- **Verified live, twice:** the `MoveModelRequest` probe itself (raw API
  numbers only), then a second, separate live check running the actual
  `Fly`/`VTSFlySubscriber` classes end-to-end (synthetic `director.mood`
  events standing in for the LLM emitting real tags) against the running
  VTS instance. User confirmed: **glided smoothly both ways** (no
  snapping), **the ball trailed during the move** — the rig's §8.2
  physics group reacting to root model movement exactly like it already
  does to head movement, with no extra code — and **the fly expression
  visibly switched the chao from sitting to flying pose** while it moved
  across the screen. All three channels (position, ball lag, expression
  toggle) confirmed working together, not just individually.
- 21 new tests (`Fly`/`FlyConfig`/`load_fly_config` in
  `test_director_aliveness.py`, `VTSFlySubscriber` in `test_outputs_vts.py`),
  full suite at 147 passed, ruff clean.
- **Design doc updated:** §6.4 gets a session-9 update block documenting
  the interpolation finding and the known landing-in-quiet-stretches gap.
  §10.1's micro-timescale row also corrected — breathing and the ball
  spring are both native to VTS/the rig, not this layer's job; blink
  timing is left as still-owned pending a check.
- **Not done / still open:** exact `x_range` bounds vs. the real visible
  window (needs a by-eye check, not just "didn't error"); whether VTS
  persists a moved position across a restart if the process dies mid-
  flight instead of landing cleanly first (low risk, not tested).

### Session 9, part 3: the timescale loop (minimal — one rate, not three)

User asked for "the timescale loop" next. Consulted `advisor` before
building it, since §10.1 nominally specifies three tiers (60Hz/~1Hz/~1min)
and the same "build only what has a real consumer" lesson from part 2
applied again: no bound VTS parameters exist (60Hz has nothing to drive),
the dashboard's mood plot doesn't exist (nothing renders a continuous
mood stream), and §10.6's macro accumulators aren't built. The one real
consumer today is `Fly` landing during silence — `director.mood` only
fired when a tag nudged it, so a flying chao could never re-evaluate
arousal and land on its own. Built exactly that, nothing more.

- **`mood.py`: `Mood.tick(now)` (new)** — decay-only path (no tag delta),
  sharing `_decay_to` with `handle()` so a tick immediately before a tag
  provably lands on the same point a tag alone would at the same wall-clock
  time (exponential decay is step-invariant if and only if both paths run
  the same code — verified with an explicit equivalence test, not just
  asserted). Publishes `director.mood` with `turn_id=None` and
  `source="tick"`, but only when the point moved more than
  `_TICK_PUBLISH_EPSILON` since the last tick — near baseline, decay
  asymptotes and this naturally goes quiet instead of putting one event on
  the bus every second forever. `handle()` now also tags its payload
  `source="tag"` (existing exact-payload tests updated).
- **`MoodConfig.tick_interval_s`** (new field, default 1.0s) — read from
  `emotes.yaml`'s `mood:` block, same as the other mood tuning values.
- **`__main__.py`: `_run_mood` reworked**, not a new task — wraps
  `sub.get()` in `asyncio.wait_for(..., timeout=tick_interval_s)`; a
  `TimeoutError` calls `mood.tick()` instead of a second task sharing the
  same `Mood` instance. Simpler, and safe regardless (`Mood`'s methods are
  synchronous, no internal awaits, so there's no interleaving hazard even
  with two callers) — one task, one loop won out on simplicity alone.
- **`aliveness.py`: `Fly._on_mood` gated** — advisor flagged this before
  it became a live bug: with ticks now flowing through the same
  `director.mood` path repositioning already listens to, an unguarded
  `Fly` would pace to a new x every `min_reposition_s` with zero real
  activity, exactly the caged-pacing failure §10.2 warns against. Fixed by
  checking `event.payload["source"] == "tag"` before repositioning — the
  on/above and off/below hysteresis checks stay unconditional (both
  sources should be able to launch/land), only repositioning is
  tag-gated. Existing `Fly` tests already defaulted their `mood_event()`
  helper to `source="tag"`, so they kept passing unchanged; new tests
  added specifically for tick-sourced landing (works) and tick-sourced
  repositioning (correctly suppressed).
- **Found and fixed a real test bug while writing the `_run_mood`
  real-bus test:** publishing an event immediately after
  `asyncio.create_task(_run_mood(...))` raced the task's own
  `bus.subscribe()` call — same class of bug as session 8's VTS subscriber
  fix, just in test code this time, not production. Fixed with
  `await asyncio.sleep(0)` between task creation and publish to let
  `_run_mood` reach its subscribe call first.
- 8 new tests (5 for `Mood.tick()`, 2 for `Fly`'s source-gating, 1 for the
  real-bus `_run_mood` wiring), full suite at 155 passed, ruff clean.
- **Verified live, twice more:** first, the existing `Fly`/`VTSFlySubscriber`
  live-check pattern reused to confirm the tick-driven landing path calls
  the same, already-visually-confirmed `MoveModelRequest`/
  `ExpressionActivationRequest` sequence. Second, a dedicated live run:
  launched with a single `[angry]` tag, then genuinely no further
  events — landed on its own after ~14s (half-life shortened to 5s for a
  faster demo; thresholds/tick rate left as configured) purely from
  `Mood.tick()` re-decaying arousal below `off_below`. Not watched live
  this particular run (user wasn't at the screen), so this is confirmed at
  the API/log level — real `Fly.flying` transition, real VTS calls issued —
  but not re-confirmed by eye this time; the underlying VTS calls
  themselves were already visually confirmed working in part 2's check.

**End of day 2026-08-11.** Three commits landed and pushed
(`584771a` Piper install + `PiperBackend`, `60c75e3`/`e7fb8bf` the fly
state machine with horizontal positioning, `1796383` the minimal
timescale loop): Piper is installed with a placeholder voice and a
tested, live-verified synthesis interface; §6.4's fly state machine now
flies the chao to different horizontal screen positions and back, with
the ball trailing and the fly expression toggling correctly, all
confirmed live; and a flying chao now lands on its own during silence via
`Mood.tick()`, without needing a fresh tag. Working tree clean, full
suite green (155 passed), pushed to `origin/master`. Next session's
natural entry points: the attention model (§10.3), or eyeballing the fly
`x_range` bounds against the real stream layout (see Next actions below).

## Stopping point (end of session 8, 2026-08-08)

Picked the TTS engine (**Piper**, confirming the design doc's existing
placeholder — it's the only option that satisfies invariant 1 at the
existing 200ms first-chunk latency budget; GPU-oriented alternatives were
ruled out on that basis, not on quality). Voice selection is explicitly
deferred: user wants a childlike voice, is willing to pay for one, but
wants to start with any free default Piper voice so pipeline work (audio
routing, envelope extraction) isn't blocked on finding the "right" voice
first. Swapping the voice file later is a config change, not a pipeline
change, so nothing built against a placeholder voice needs rework.

**Verified `ANTHROPIC_API_KEY` is valid** — session 6's "primary backend
failed before first token" finding was real but wasn't the key: a live
call through the actual `AnthropicBackend` class (not just the raw SDK)
succeeded cleanly. Whatever caused the two session-6 failures is still
unexplained, but it isn't an expired/invalid key. Not chased further since
it hasn't recurred.

**Found and fixed a real bug, not a phantom one: the §10.5 anticipation
nudge was silently invisible on every fresh app start.** With VTS actually
open (first time this project has had a live VTS instance available
during a session), the ball never showed the question mark on two
consecutive live runs — no error anywhere, clean shutdown both times. A
direct, isolated `ExpressionActivationRequest` against `question.exp3.json`
(bypassing the whole app) worked immediately and visibly, which ruled out
VTS/the expression file itself and pointed at the app's own wiring.

Root cause: `__main__.py`'s `_run_vts_subscriber` called `bus.subscribe()`
**after** `await client.connect()` / `await client.authenticate()` — both
real network round trips to VTS. `Bus.subscribe()` (see `bus.py`) has no
event replay for late subscribers; a fast first turn (input arriving
essentially at process start, as it does with piped/scripted input, and
plausibly with fast manual typing too) could fire `brain.request` →
`Aliveness`'s anticipation nudge → `director.emote` before the VTS
subscriber's queue even existed, silently dropping the event forever with
nothing anywhere to indicate it happened — no exception, no `Kind.ERROR`,
no "VTS unavailable" message, because nothing actually failed. **Fix:**
move `sub = bus.subscribe()` to before the connect/authenticate calls, so
anything published during that handshake queues up normally instead of
vanishing. Verified live, twice, after the fix: both the anticipation
nudge (`question`) and a tag-triggered `happy` emote from the actual reply
fired and were visually confirmed by the user. `docs/rigging_check_list.md`
item 2 (visual confirmation of the anticipation nudge) is now genuinely
closed, not just deterministically tested.

No new unit test added for this fix — it's a startup-ordering bug in live
process wiring (`__main__.py`), not testable the way `Mood`/`Aliveness`'s
pure logic is; the existing bus-level integration test
(`test_aliveness_fires_anticipation_emote_on_a_real_bus`) already proves
the event ordering is correct once a subscriber exists, which is the part
that's actually unit-testable. The bug was specifically in *when* the real
app subscribes, which only a live run against real VTS could surface.

122 tests still passing (no new ones — see above), ruff clean.

**Design doc review, same session, closing it out:** user asked for a
staleness pass over `chao-companion-design-v0.2.md` before ending for the
day, on the correct instinct that several sessions' worth of real
decisions hadn't been reconciled back into it. Read the full document
end to end against current code/CLAUDE.md/this file. Found and fixed:

- **§5.2 / §18, real discrepancy, not just wording:** the doc still
  described the local LLM backend as a raw `llama.cpp` server (OpenAI-
  compatible endpoint, `--threads 8`, specific RAM/tok-s figures). Actual
  phase-1 implementation (`src/chao/brain/local.py`, built session 4-ish)
  is `OllamaBackend` against Ollama's own `/api/chat` endpoint —
  `qwen3:8b`, zero-VRAM via `OLLAMA_NUM_GPU=0`, model storage relocated
  via `OLLAMA_MODELS`. Same intent (CPU-only quantized 8B inference),
  different mechanism, and the doc named the wrong one. Rewrote §5.2 and
  §18's LLM (local) row to describe Ollama; deliberately did not restate
  specific RAM/cold-load numbers in the design doc itself (session notes
  disagree with each other across sessions on the exact cold-load figure
  — ~57s in one place, ~2min in another, likely different measurement
  conditions) — pointed at `SESSION_STATE.md` instead of asserting a
  number the doc can't keep current.
- **§19 open question 2, stale status claim:** said "Neutral is blocking
  for phase 1." It wasn't, in practice — phase 1 shipped (dashboard live,
  anticipation nudge built and now visually confirmed) with `neutral`
  unmapped and shelved, exactly as CLAUDE.md and this file's next-actions
  list have said for several sessions. Updated to reflect that.
- **§13 event schema, minor:** `director.mood`'s payload column said
  `valence, arousal, baseline` (singular) — actual implemented payload
  from `mood.py` is `{valence, arousal, baseline_valence,
  baseline_arousal}` (two dims, not one combined value). Corrected the
  table.
- **§18 TTS row, minor:** updated "re-evaluate options at phase 2" to
  reflect that the re-evaluation happened this session (Piper confirmed,
  see above) rather than leaving it perpetually phrased as pending.

**Not changed, checked and found still accurate:** §7's plugin-parameter
binding explanation, the file tree, the phase table's shape (it's a
target-state document, not a progress tracker — SESSION_STATE.md is
correctly the place completion status lives, not the design doc), and
the event schema's other rows.

**End of day 2026-08-08.** This closes out sessions 6-8: aliveness's
anticipation nudge, mood.py, the TTS engine decision, the ANTHROPIC_API_KEY
verification, the VTS subscriber race fix (with live visual confirmation),
and this design doc reconciliation. See "Next actions" below for what's
still open going into the next session — `motion.py` is next in line,
gated on either the attention model (buildable now, not live-verifiable
without Twitch/voice input) or picking a TTS model's actual voice
(now that Piper is confirmed) to give `motion.py`'s envelope extraction a
real interface to build against.

## Stopping point (end of session 7, 2026-08-08)

Built `director/mood.py` — the valence/arousal half of the aliveness layer
(design doc §6, §10), user-requested next ("mood.py first, since it drives
eye expression") after correcting a wrong claim from session 6's writeup:
**hand-binding VTS parameters was never a prerequisite for writing or
testing mood/motion logic, only for the final step of driving VTS with
real injected values.** Per CLAUDE.md's own testing section, mood decay is
named explicitly as a pure function to test directly, no I/O — same
category as `director.py`'s cooldown logic, which was built and fully
tested long before any live VTS check. Corrected in-conversation before
any code was written; flagging here so the wrong version doesn't get
re-read as true later.

`Mood` (new, `src/chao/director/mood.py`) is a plain bus subscriber
(`handle(event)`) that reacts to `director.tag` only: on a recognized tag,
lazily decays the current valence/arousal point toward baseline based on
elapsed time since the last update (`decay()`, a pure exponential
half-life function — tested directly: `elapsed=0` is identity, one
half-life lands exactly halfway, monotone convergence, zero-half-life is a
no-op), then applies that tag's §6.2 delta on top, clamped to [-1, 1], and
publishes `director.mood` (new bus contract — payload
`{valence, arousal, baseline_valence, baseline_arousal}`; nothing consumes
it yet besides the dashboard's generic event-feed relay). Wired into
`__main__.py` as a fifth background task, `_run_mood`, same shape as
`_run_aliveness`.

**Real design decision, caught by advisor before writing code:** the delta
table (`_TAG_DELTAS`) is keyed on the **tag**, never the emote pool.
`director.py`'s `_TAG_TO_POOL` maps both `happy` and `affection` to the
same `"happy"` pool, but §6.2 gives them different valence/arousal deltas
(+0.7/+0.6 vs +0.9/+0.3) — keying on pool would silently give `affection`
happy's numbers. `thinking` is deliberately **absent** from the table
(§6.2 marks it "transient only"), not present with zero deltas — an absent
key correctly no-ops (no decay side-effect, no `director.mood` publish),
which matters because `Director` already fires `thinking`'s emote via the
`curious` pool, so a mood entry would have double-counted a signal that
design doc's table says shouldn't move mood at all. `pause` and
`look:chat`/`look:you` fall through the same lookup miss, no special-casing
needed, since `_tag_key()` renders them as strings the table just doesn't
contain.

**Config:** added a `mood:` block to `config/emotes.yaml` (`baseline_valence:
0.2`, `baseline_arousal: 0.3`, `half_life_s: 20.0`) rather than starting
the empty `config/chao.yaml` from scratch — `emotes.yaml`'s own header
comment already parked `fly:`/`heart_gate:` there as belonging to
aliveness.py/mood.py, the strongest existing signal for where this
belongs. **The two baseline numbers are explicit tuning placeholders, not
settled values** — flagged in the YAML comment that `baseline_arousal`
specifically will later set how often the chao rests near flying-eligible
once `aliveness.py` reads it for §6.4's fly hysteresis (`on_above: 0.70`).

**Deliberately not built this pass** (mirrors aliveness.py's own scoping
precedent): no periodic tick — decay is lazy, computed only when a tag
arrives, since nothing in the codebase runs a timer loop yet; `aliveness.py`
owns the micro/meso/macro timescales (§10.1) and will call a future
`Mood.tick(now)` once that loop exists. Fly's hysteresis (§6.4) and heart
gating (§6.3, needs per-viewer affinity from memory, phase 6) are
untouched — both already have config parked in `emotes.yaml` and stay
aliveness.py's/phase-6's job respectively. No VTS wiring at all yet:
`director.mood` isn't consumed by any subscriber besides the dashboard's
generic relay, so the actual hand-binding step (pick target Live2D
parameters, `ParameterCreationRequest`, bind in VTS UI, verify via
`vts_probe.py`'s proven procedure) is still real future work — just
correctly scoped now to "wire mood's output into VTS," not "everything
mood-related."

18 new tests (`tests/test_director_mood.py`), 122 total passing (up from
104), ruff clean.

## Stopping point (end of session 6, 2026-08-08)

Built the first piece of `director/aliveness.py`: the §10.5 anticipation
nudge (pop the ball's question-mark expression the instant `brain.request`
fires, before any audio/text exists — covers the 0.5-1.5s latency window
in-character instead of as dead air). Scope was deliberately narrow, same
discipline as the dashboard skeleton: the rest of §10 (micro/meso/macro
idle drift, the attention model) needs continuous parameter injection,
which per the phase-0 finding requires new custom VTS parameters **bound
by hand in the VTS UI** before code can drive them — a real user-side
dependency, not something blocking the code but something blocking
*verifying* it live. Not started; see next actions below.

`Aliveness` (new, `src/chao/director/aliveness.py`) is a plain bus
subscriber (`handle(event)`, no async/I/O of its own) — reads
`emote_config.reactions["anticipation"]` (new `EmoteConfig.reactions:
dict[str, str]` field, parsed by `load_emote_config`; `config/emotes.yaml`'s
`reactions:` block already had this exact slot reserved, header comment
said "not built yet" — now it's built for this one entry) to pick a pool
(`curious`, i.e. `question.exp3.json`), and fires it via the same
`director.emote` event `VTSEmoteSubscriber` already knows how to render —
no new VTS-side plumbing needed. Wired into `__main__.py` as a fourth
background task, `_run_aliveness`, alongside the error/dashboard/VTS
tasks.

**Deliberately does not share Director's cooldown state for the `curious`
pool** — this was the one real design decision here, caught by advisor
review before writing code: `curious`'s cooldown is 3s, and the
anticipation nudge fires right as that window opens relative to a
`[curious]`/`[thinking]` tag in the reply itself, which typically lands
well inside 3s. Sharing cooldown state would mean the nudge *silently
suppresses* the LLM's own semantic signal on essentially every turn — no
error, nothing visibly broken, just a systematically muted tag. Firing
independently risks the opposite, benign failure mode instead: two
activations of `question.exp3.json` close together, which
`VTSEmoteSubscriber._activate` already handles correctly (cancels the
pending deactivate, extends the hold, rather than a stale deactivate
cutting the new one short). Documented at length in `aliveness.py`'s
docstring and pinned by
`test_does_not_share_cooldown_with_tag_triggered_curious_emotes`.

Two rounds of advisor review happened (before writing code, and after).
The second round caught a real gap: the first version of the integration
test only asserted the anticipation emote *eventually* appears on the
bus — it would have passed identically if the emote arrived after
`brain.complete`, which defeats the entire point of §10.5 (the nudge only
matters if it's *early*). Fixed with a `SuspendingFakeBackend` (genuinely
suspends via `asyncio.sleep(0)` before its first chunk, unlike the
existing `FakeBackend`, which never yields control to the event loop at
all — meaning a whole turn can run start-to-finish in one uninterrupted
task step, starving `aliveness_task` of a chance to run until *after*
`brain.complete` already fired) plus an explicit ordering assertion
(`director.emote` index < first `brain.token` index in the observed
sequence). Verified non-flaky across 5 repeated runs — the FIFO ordering
of task scheduling favors aliveness resuming before `_run_turn`
reschedules, so this isn't a timing coin-flip.

**A live end-to-end attempt (real Anthropic call + real dashboard
websocket + a scripted client, same pattern as session 5's dashboard
verification) was tried and abandoned, not because anything is broken:**
the scripted probe connected to the dashboard's websocket ~2s after the
server published its burst of turn-start events, consistently missing all
of them (Python + `websockets` cold-import + TCP/HTTP-upgrade overhead ate
the margin) — and separately, VTS isn't running on this machine right
now, so even a clean live run couldn't have shown the actual payoff
(someone seeing the question mark on the model). Advisor's read, which
matches the evidence: don't retry it, the deterministic integration test
above plus the already-passing `test_dashboard_server.py` websocket-relay
tests cover everything a live run would have proven. **Visual
confirmation in VTS is still outstanding** — added to next actions below,
not silently dropped.

**Unrelated finding, also not chased down:** both live attempts this
session logged `"primary backend failed before first token; falling back
to local"` — the cloud (Anthropic) call failed before any token arrived,
even with the sandbox disabled for the second attempt. Session 5 made
real Anthropic calls successfully from this same machine, so this is new,
not a standing issue. Possibly a rotated/expired `ANTHROPIC_API_KEY`, or
something else entirely — not investigated, out of scope for this
session's work (aliveness doesn't depend on the backend call succeeding;
`brain.request` — and therefore the anticipation nudge — fires
unconditionally before the backend is ever called). Flagging so it isn't
rediscovered as a fresh mystery: **if live turns are silently falling back
to Ollama, check the API key first.**

**Newly relevant, not a code change:** `curious`'s `duration_s: 2.0`
clears the question-mark expression 2s after it fires. If a turn falls
back to local (Ollama, ~2 *minutes* cold-load per SESSION_STATE's session
5 notes), the ball will have already returned to neutral long before the
reply actually starts streaming. This is correct per CLAUDE.md's "never
sustain an authored emote beyond ~4s" — flagging it only because item (2)
above makes local fallback newly likely to actually happen live, not
because anything needs to change.

104 tests passing (up from 95), ruff clean.

## Stopping point (end of session 5, 2026-08-07)

The dashboard (design doc §11) exists and is verified working end-to-end,
scoped deliberately to just the event feed panel (§11.2 item 3) — the
other eight panels render data nothing in the backend produces yet
(retrieval, mood.py, parameter injection), so building UI for them now
would be UI over nothing. `__main__.py`'s `_print_events` console stand-in
is gone, replaced by an in-process FastAPI/uvicorn server plus a Vite+React
frontend (`src/chao/dashboard/web/`), exactly per §11.4's stack. A real
turn (typed input → Anthropic → tags → emotes) was pushed through the
actual websocket and its full event sequence confirmed on the wire via a
scripted client. This part is committed (`99d667a`).

**Presentation layer changed after that commit, same session:** the user
asked, after seeing the plan, to present the dashboard in a native window
via `pywebview` instead of a Chrome tab — Chrome's baseline per-tab/process
overhead is unwelcome given CLAUDE.md invariant 1 (zero VRAM, the user
games on this machine; RAM overhead isn't the invariant's literal scope
but is in the same spirit). Design doc §11.4 and §18's tech table updated
first, then built: `chao.dashboard.server` now also serves the built
frontend (`web/dist/`) as static files, and a new `chao.dashboard.window`
entry point opens it in a native window via WebView2 (Windows' built-in
Chromium-based runtime — reuses what's already on the machine rather than
adding a separate browser instance). Committed (`20daf1e`).

**Visual verification finally closed out, same session, after both
commits above:** the Chrome extension failed to connect twice earlier in
the session (`tabs_context_mcp`: "Browser extension is not connected") —
turned out to be an environment/login-state issue, not anything
structurally wrong; it connected cleanly once the user confirmed it was
actually installed. Opened the dashboard in a real tab, pushed two real
turns through the running app, and visually confirmed the event feed
renders exactly as designed (dark theme, correct ordering, correct
latency/tag/emote data, streaming bubble folding cleanly into history).
See "Native window presentation" → its "Next actions" closure note for
detail. The gap flagged twice earlier this session is genuinely resolved,
not just re-flagged again.

Also this session, before any of the above: the C→D directory move (done
between sessions, outside a coding session) had silently broken the
venv's editable install, which would have made every test fail with a
confusing `ModuleNotFoundError` the moment anyone tried to run them —
caught and fixed. Ollama got installed and `qwen3:8b` pulled and wired in
as the local fallback, confirmed CPU-only and working, though slow (as
expected).

Nothing is broken or mid-edit; 95 tests passing, ruff clean. Both the
dashboard skeleton and the native-window presentation layer are
committed. The only loose end from before is `neutral`, still
deliberately shelved.

**Pick up here next session:** next actions list at the bottom, roughly in
priority order. `aliveness.py` is probably the most natural next step now
that the dashboard exists to watch it work, but phase 2 kickoff (TTS model
selection) is equally reasonable if that's the priority instead.

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

## brain/ — LLM backend protocol and prompt assembly (session 3)

Built against design doc §4.3/§5, in two commits:

**`brain/backend.py`** — the `LLMBackend` Protocol exactly as §5 specifies
(`stream(system, messages, cancel) -> AsyncIterator[str]`), plus
`CircuitBreakerBackend` (§5.3): races the primary's first token against a
timeout (default 2s); on timeout or any exception *before* the first
token, falls back to a second backend for the whole turn. Once the primary
has yielded anything, no fallback is possible — later errors just
propagate, since switching backends after text is already out would
duplicate it.

**`brain/prompt.py`** — `assemble_prompt` is a pure function (no file/DB
I/O — that's the caller's job) returning a `Prompt` with `stable`
(identity+personality) and `dynamic` (memory) kept as **separate fields**,
not pre-joined into one string. This was the one thing worth getting
exactly right: `stable` must stay byte-identical across turns that don't
change personality state, or cloud prompt caching and llama.cpp's KV slot
cache (CLAUDE.md: "keep the first two byte-stable") silently stop working.
Tested directly (`test_stable_block_is_byte_identical_regardless_of_memory_or_context`).
Chat-sourced input is wrapped in `<message source=... trust=...>` and
labelled `untrusted` regardless of what `config/identity.md` says
(invariant 6) — voice/manual/ambient get `trusted`. Token budgeting is
`len(text)//4` (approximate, named `approx_tokens` so it's never mistaken
for exact); memory is capped to a budget, recent context trims from the
middle when over budget, never the top (§5.2).

**`brain/cloud.py`** (`AnthropicBackend`) and **`brain/local.py`**
(`OllamaBackend`) — the two concrete backends, added in a second commit
once `anthropic`/`httpx` were added as dependencies. Both take an
injectable `client` so tests fake the SDK/HTTP shape instead of hitting
the network or spending credits. Both check the cancel token *between*
chunks, not mid-await — good enough for the common case; a backend that
stops yielding entirely relies on bus.py's hard-cancel escalation instead
of perfect cooperative cancellation.

**Decisions made with the user this session:** cloud backend is Anthropic
(`claude-haiku-4-5-20251001` default, overridable via constructor — not
yet wired to `config/chao.yaml`, which is still empty); local backend is
Ollama rather than raw `llama-server` (friendlier to run, still CPU-only,
still zero-VRAM). `config/identity.md` stays a stub for now — real
character/trait content is a separate pass with an Opus review when the
user's ready, per CLAUDE.md's prompt-design escalation rule.

**Not built yet, deliberately deferred:** the turn orchestrator that
actually satisfies `bus.py`'s `TurnHandler` shape (assembling a `Prompt`,
publishing `brain.request`/`brain.token`/`brain.complete`, calling a
backend). That belongs in `brain/__init__.py` or similar, once
`director/tags.py` exists to consume the token stream — building it
before there's anything to hand tokens to would be premature. Anthropic
prompt caching's `cache_control` breakpoints are also deferred — `Prompt`
keeps the `stable`/`dynamic` seam so that's addable later without a
refactor, just not implemented now.

30 tests passing (up from 13), ruff clean.

## director/tags.py — tolerant tag parser (session 3, cont'd)

`parse_tags(text) -> (cleaned_text, list[ParsedTag])`, pure and stateless
per §9. Two-stage matching: anything bracket-shaped (`[word]` or
`[word:word]`) is stripped from output regardless of recognition, so a
hallucinated `[proud]` or `[laughs]` never reaches TTS; only names/values
in `KNOWN_TAGS`/`KNOWN_LOOK_TARGETS` become a `ParsedTag`. Unclosed
brackets and multi-word bracketed asides are left as literal text —
deliberately not guessed at. Also exports `split_sentences`, reused by
whatever in director.py buffers to sentence boundaries.

Advisor review caught a real bug before commit: a trailing tag (nothing
after it, e.g. a sign-off `[happy]`) produced a `sentence_index` one past
the end of `split_sentences(cleaned)` — an `IndexError` waiting for
director.py, which will schedule emotes by indexing into the sentence
list. Fixed by clamping to the last real sentence index. Tests added for
both the trailing-tag case and tag-only input (empty cleaned text).

**Important precondition for director.py, documented in tags.py's
docstring but not enforced by it:** `parse_tags` must only be called on
text whose tags are already complete. `brain.token` arrives as chunks —
`[hap` / `py]` split across a chunk boundary won't match either half, and
both fragments would leak into TTS as literal text. Buffering partial
tokens until tags (and ideally sentence boundaries) are complete is
director.py's job. Whitespace is collapsed per call too, so don't rely on
leading/trailing whitespace surviving between chunks.

48 tests passing (up from 30), ruff clean.

## director/director.py — tag stream to emote decisions (session 3, cont'd)

`Director` buffers `brain.token` chunks to sentence boundaries
(`process_chunk`/`end_turn`), parses tags per completed sentence via
`tags.parse_tags`, maps recognized tags to an emote pool (§6.2 —
happy/affection→happy, curious/thinking→curious, surprise/confused/sad/angry
→ themselves; `pause`/`look:*` produce `director.tag` telemetry but never
an emote) and fires it via `director.emote`, gated by a per-pool cooldown
and "never the same hotkey twice consecutively" (only meaningful with 2+
hotkeys per pool — current config has 1 each, so it always repeats, which
is correct). Publishes events only — no VTS calls. `publish` is just
`bus.publish` (sync for non-turn-triggering kinds), so Director itself
needs no `async` at all.

Deliberately out of scope this pass: mood valence/arousal (`mood.py`,
still its own empty stub — no `director.mood` published), the `heart`
pool (§6.3, affinity-gated, needs memory/phase 6), fly (`aliveness.py`
owns `state.fly`). Firing is immediate — no TTS exists yet to schedule
against. The single `self.publish(...)` call in `_maybe_fire_emote` is the
seam where CLAUDE.md's "schedule against the audio playback clock, not
token arrival" gets wired in during phase 2.

`config/emotes.yaml` populated with the design doc's §14 example (pools +
fly + heart_gate + reactions) — **hotkey names (`chao.happy` etc.) are
provisional**, copied from the doc, not yet confirmed against this
model's actual VTS hotkey list. Confirm before outputs/vts.py wires
`director.emote` up to real `ExpressionActivationRequest` calls. Added
`pyyaml` to load it; only the `pools` section is read so far.

Advisor review caught two real bugs before commit:
- `end_turn()` cleared `_turn_id` *before* publishing the final flush's
  events, so the LLM's last sentence — the one most likely to lack
  trailing punctuation and therefore go through the flush path — always
  published with `turn_id=None`, breaking the latency-waterfall/prompt-
  re-run use of that field. Fixed by clearing after.
- The sentence-buffering "safe prefix" had an extra guard that held back
  the *entire remaining buffer* whenever the about-to-be-released prefix
  contained an unmatched `[`. Reachability analysis: a well-formed tag's
  contents can't contain `.!?` or whitespace, so a real tag can never
  straddle a sentence-terminator split point — the guard could only ever
  fire on a genuinely malformed stray `[` in prose, and in that case it
  silently stalled *all further sentence streaming for the rest of the
  turn* (nothing would release until `end_turn()`), quietly defeating
  §12's sentence-streaming latency budget for a case tags.py already
  tolerates fine on its own (leaves it as literal text). Removed. Sentence
  boundary regex de-duplicated: `tags.SENTENCE_BOUNDARY` is now public and
  imported by director.py instead of a second copy — the two had to stay
  identical for `sentence_index` to line up, so keeping one copy removes a
  whole class of drift bug.

68 tests passing (up from 48), ruff clean.

## brain/turn.py — the turn orchestrator (session 3, cont'd)

`TurnOrchestrator` is the piece that finally satisfies `bus.py`'s
`TurnHandler` — instances implement `__call__(event, cancel, turn_id)`
matching that signature directly, so `Bus.run(orchestrator)` just works
(proven with a real `Bus` in `test_works_as_a_real_bus_turn_handler`, not
just direct calls). Per turn: derive a `CurrentEvent` from the triggering
input (`input.chat`→source="chat"+speaker from display_name/login,
`input.voice`→"voice", else "manual"), `assemble_prompt`, publish
`brain.request` immediately after (as early as possible, for §10.5's
anticipation nudge once aliveness.py exists to consume it), stream the
backend publishing `brain.token` per **raw** chunk (tags included — that's
the faithful record) while feeding each chunk to `Director.process_chunk`,
then `Director.end_turn()` to flush the remainder, then `brain.complete`
with `full_text`/`latency_ms`/`usage` (`usage: None` always — not exposed
by the `LLMBackend` protocol yet, a documented gap not a bug).

Recent conversation history is a simple in-process bounded `deque[Turn]`
(default cap 40, must stay even — see comment on `history_limit`), not
persisted; cross-session memory is still phase 6. `personality`/`memory`
are static constructor strings for now, pending `mood.py` and
`memory/store.py`. History reuses `prompt.messages[-1].content` (the
already-wrapped/labelled current event) for the "user" turn rather than
re-wrapping raw text, so untrusted-chat labelling survives into future
turns' recent context instead of being lost.

Advisor review caught one real bug before commit, plus a real test gap:
- A reply that's empty after tag-stripping (backend yields nothing, or
  the model emits only a tag, e.g. `"[happy]"` → cleaned `""`) was still
  appended to history as an empty-content `Message`. The Anthropic API
  rejects empty message content with a 400; `CircuitBreakerBackend` reads
  that as a pre-first-token failure and falls back to local — for every
  turn from then on, since the poisoned empty Turn stays in history. The
  session doesn't recover without a restart. Fixed: the history append
  (not `brain.complete`, which still reports the real `full_text`,
  possibly `""`) is skipped when the cleaned reply is empty.
- Every test called the orchestrator directly; nothing proved the
  headline claim (works as a real `TurnHandler`). Added
  `test_works_as_a_real_bus_turn_handler` against an actual `Bus.run()`.

81 tests passing (up from 68), ruff clean. **Phase 1's core pipeline is
now wired end-to-end**: typed/chat/voice input → `Bus` arbitration →
`TurnOrchestrator` → backend → `Director` → tag/emote events. What's
missing before it's runnable live is `__main__.py` (wiring real backends,
loading `config/identity.md`/`emotes.yaml`, calling `Bus.run`) and a VTS
subscriber for `director.emote` — everything else in the chain is real
code, not a stub.

## `__main__.py` — live entry point (session 3, cont'd)

`build_pipeline(*, identity, emote_config, backend) -> (Bus, TurnOrchestrator)`
holds the dependency-injected wiring (tested with fakes in
`test_main.py`); `main()` owns the real I/O — reads
`config/identity.md`/`config/emotes.yaml`, constructs
`CircuitBreakerBackend(AnthropicBackend(), OllamaBackend(...))`, and runs
`Bus.run(orchestrator)` against typed stdin. Typed lines become
`input.manual` events; a `_print_events` subscriber stands in for the
not-yet-built dashboard/JSONL log, printing streamed tokens, latency,
emote firings, and drops. This is `uv run python -m chao` — the first
live-runnable slice of phase 1, proven by direct construction against
the real config files (not just fakes): imports, `AnthropicBackend()`,
`OllamaBackend(...)`, `CircuitBreakerBackend`, and
`load_emote_config(EMOTES_PATH)` all succeed, and the emote pools load
correctly from the real `config/emotes.yaml`.

Populated `config/identity.md` with an operational placeholder
(temperament + the tag vocabulary from `director/tags.py`, explicitly
labelled a stub pending the real Opus-reviewed character sheet) — an
advisor review flagged that with the file empty, the prompt gave the
model no reason to ever emit a tag, so the live smoke test would look
like a working chat loop with a silently dead emote pipeline. The two
other findings from that review were fixed before commit: `main()` now
fails fast with a clear message if `ANTHROPIC_API_KEY` isn't set
(previously the error surfaced three layers deep as a generic
circuit-breaker fallback failure, after the user had already typed);
shutdown now calls `bus.kill()` before cancelling the run/console tasks
so the in-flight turn's cancel token is set and it exits cleanly instead
of being hard-cancelled mid-await.

**Ollama is not installed on this machine yet** — the local fallback
path uses a placeholder model name (`llama3.1:8b-instruct-q4_K_M`,
override via `CHAO_OLLAMA_MODEL`) and is untested against a real server.
If cloud fails and it falls back, expect a connection error, not a
response. `config/chao.yaml` is still empty/unread — the Anthropic model
choice stays a `cloud.py` constant, not yet configurable.

83 tests passing (up from 81), ruff clean. Not yet run live against the
real Anthropic API in this session (needs `ANTHROPIC_API_KEY` set and
spends credits) — wiring is verified, live behavior isn't.

## `.env` support + first live run (session 3, cont'd)

Added `python-dotenv`; `main()` calls `load_dotenv()` before the API key
check, so a local `.env` (gitignored, template in `.env.example`) is
picked up automatically — no more setting the env var by hand per shell
session.

**Ran the full pipeline live for the first time this session** — typed
input → real Anthropic call → tags → Director → emotes, end to end.
First attempt crashed: `CircuitBreakerBackend`'s `first_token_timeout_s`
default (2.0s, from CLAUDE.md's "~2s" note) was tripping on a *working*
call — a cold Anthropic connection (fresh TLS handshake, no keep-alive)
measured ~2.25s to first token, just over the threshold, so it fell back
to Ollama, which isn't installed, and the turn crashed with a connection
error. Confirmed by calling `AnthropicBackend` directly outside the
breaker (worked fine, ~2.25s to first token). Bumped the default to
5.0s — verified against `test_brain_backend.py` (all tests pass explicit
values, unaffected) and a second live run, which completed cleanly in
~3.1s with both `[happy]` and `[curious]` firing real `director.emote`
events (`chao.happy`, `chao.question`).

This is the first real evidence the identity.md placeholder's tag
instructions work — the model used tags correctly, once per sentence,
at sentence start, unprompted for specifics beyond the placeholder's
example.

83 tests passing (unchanged — no new tests needed: `load_dotenv()` is
I/O, not unit-tested, and `test_brain_backend.py` already covers the
breaker's timeout behavior via explicit values), ruff clean.

## Standing decisions made this session

- Git remote confirmed on `jesse-github` SSH alias (see CLAUDE.md's GitHub
  account check).
- Commit hygiene: batch related changes, one commit per stopping point (now
  in CLAUDE.md).
- Ball spring is real, deliberately designed (§8.2: subtle lag behind head
  motion, ~1/3s settle, not the ball detaching or roaming) — not being
  dropped, just deferred pending a rigging fix.

## config/emotes.yaml confirmed against live VTS (session 3, cont'd)

Queried the running VTS instance directly (`ExpressionStateRequest` +
`HotkeysInCurrentModelRequest`) and replaced the placeholder `chao.*`
names with the real expression file names. All 7 tag-mapped pools plus
`fly` have a matching `<name>.exp3.json` file and a 1:1 hotkey:

- Confirmed: `happy`, `heart`, `question` (curious pool), `surprise`,
  `confused`, `sad`, `angry`, `fly` — all exist. `happy.exp3.json`
  visually verified firing live via `ExpressionActivationRequest`.
- **`neutral` genuinely does not exist in VTS** — not just unverified,
  actually missing. Nothing currently fires it (no tag maps to
  `"neutral"` in `director.py`'s `_TAG_TO_POOL`), so `hotkeys: []` is
  safe as a placeholder. **User still needs to author a default-state
  expression in VTS** before neutral can be wired to anything — that's
  on the user, not blocked on code.

Per CLAUDE.md's phase-0 finding, the config's `hotkeys:` key holds
expression FILE names (for `ExpressionActivationRequest`), not literal
VTS hotkey labels — a 1:1 hotkey exists per expression too, but firing
should go through the expression file, not `HotkeyTriggerRequest`.

**Follow-up (session 4):** user authored a `neutral` hotkey in VTS, but
it came in as VTS's built-in **`RemoveAllExpressions`** type, not
`ToggleExpression` — there's still no `neutral.exp3.json` file, and
`RemoveAllExpressions` has no file to activate at all. This wasn't
deliberate: the user tried to build a real neutral *expression* (a file
with explicit parameter values to reset everything to baseline) but
couldn't tell what values to set without knowing the model's full
parameter list/rest state — VTS doesn't offer an easy "capture current
pose as an expression" shortcut for this. `RemoveAllExpressions` is what
VTS gave by default instead, and works conceptually (clears whatever's
active) but needs a different code path (`HotkeyTriggerRequest` by
hotkey ID, not `ExpressionActivationRequest` by file) than every other
pool. Deliberately shelved — this needs either the exact rest-state
parameter values (a modeling task, not a VTS-UI task) or a code special
case for `RemoveAllExpressions`, and neither is a priority right now
since nothing maps to `neutral` yet (`_TAG_TO_POOL` doesn't reference
it). `config/emotes.yaml`'s `neutral: { hotkeys: [] }` stays correct as
a placeholder.

## outputs/vts.py — VTS emote subscriber (session 4)

`VTSEmoteSubscriber.handle(event)` turns `director.emote` into a real
`ExpressionActivationRequest(active=True)`, then schedules a background
task to send `active=False` after `duration_s` (pulled from
`emote_config`, since the event payload only has pool/hotkey_id/reason) —
enforces CLAUDE.md's "never sustain beyond ~4s" itself rather than
trusting the caller. Deactivation is a background task specifically so
`handle()` returns immediately and doesn't block the consumer loop for
the full hold of an unrelated expression. If the same expression fires
again before its hold expires, the pending deactivate is cancelled and
rescheduled (extends the hold) instead of a stale deactivate cutting the
new activation short — the one genuinely tricky case, covered by its own
test. VTS failures (either direction) are caught, logged, and published
as `Kind.ERROR` rather than crashing the subscriber or the app.

Wired into `__main__.py` as a third background task (alongside the
console printer and the bus runner). Connects/authenticates once at
startup; degrades gracefully — prints a message, keeps the chat loop
running — if VTS isn't up or auth is rejected, rather than crashing the
whole process over an optional output.

**Verified live, visually confirmed by the user** — but it took three
attempts: the first two runs (through the full `__main__.py` pipeline)
produced no visible reaction, which looked like a real bug. Root cause
turned out to be entirely mundane: the user didn't have the VTS window
in view at the moment the 2-2.5s expression fired. An isolated
diagnostic script with a 5s countdown and a 6s hold (long enough to
switch windows and still catch it) confirmed the subscriber works
correctly end-to-end. Worth remembering: a "nothing happened" report
after a fire-and-auto-clear action needs "were you looking at the right
moment" ruled out before assuming the code is broken.

90 tests passing (up from 83 — 7 new in `test_outputs_vts.py`), ruff
clean.

## Project plan addition: TTS, subtitles, personality test segment (session 4)

User request, not yet implemented — logged in design doc §17/§18/§19 so
it isn't lost before phase 2 starts:

- **TTS** was already phase 2's headline deliverable (`Piper (CPU) —
  re-evaluate options at phase 2`, §18) — still needs an actual model
  selected when phase 2 starts, not just "Piper" as a placeholder.
- **Subtitle overlay for the stream** is new — a viewer-facing surface,
  explicitly separate from the dashboard (which CLAUDE.md already
  establishes as streamer-only debug tooling). Likely a lightweight
  browser-source page driven by `brain.token`/`output.speech_start`/`end`,
  design TBD at phase 2. Added as its own row in §18's tech table and
  open question 7.
- **A dedicated personality/voice testing segment** — a way to iterate on
  the chao's character and TTS voice without a live audience. Likely an
  extension of `__main__.py`'s typed-input loop (already proven this
  session) once TTS exists, rather than a new tool — noted as open
  question 6, to actually decide at phase 2, not before.

## Project moved from C: to D:, Ollama installed, qwen3:8b pulled (session 5)

The project directory moved from C: to D: (now `D:\PycharmProjects\chao_companion_ai`,
no space — it had previously lived at a path with a space in it, `D:\Pycharm
Projects\...`, at least once along the way). **The move silently broke the venv's
editable install**: `.venv/Lib/site-packages/_editable_impl_chao.pth` still pointed at
the old `D:\Pycharm Projects\chao_companion_ai\src` path, so `import chao` failed and
all 11 test modules errored at collection with `ModuleNotFoundError: No module named
'chao'`. Fixed with `uv sync` (regenerates the `.pth` to the current path) — 90 tests
passing again, confirmed clean. **If tests ever fail to collect with that error again
after moving/renaming the project directory, this is why — run `uv sync` first before
assuming real code is broken.**

Ollama itself installed and confirmed working:
- System env vars `OLLAMA_MODELS=D:\.ollama\models` and `OLLAMA_NUM_GPU=0` were set by
  the user (Windows System Properties, machine scope) so models live on D: and inference
  stays CPU-only per invariant 1. Verified via registry
  (`[Environment]::GetEnvironmentVariable(..., "Machine")`) rather than trusting the
  already-open shell's env, since a shell started before the vars were created won't see
  them. The Ollama app/server processes were already running from before the vars were
  set, so they were restarted (`Stop-Process` + relaunch `ollama app.exe`) to pick the
  new env up.
- `ollama pull qwen3:8b` (5.2 GB) succeeded and landed correctly on
  `D:\.ollama\models\blobs` (confirmed via `ollama list` and directly inspecting the
  folder; the default `C:\Users\jesse\.ollama\models` stayed empty).
- **Live smoke test via `ollama run qwen3:8b`, CPU-only:** first call took ~2 minutes
  total — 57s cold load (disk → RAM, one-time per keep-alive window) + ~24s prompt eval
  + ~41s generation eval, at **0.89 tokens/s prompt eval / 2.89 tokens/s generation**.
  Slow, but this is the expected cost of invariant 1 (zero VRAM, CPU-only) on an 8B
  model, not a bug. Most of the generated tokens were Qwen3's default "thinking" mode
  reasoning through a one-sentence request before the real answer — `ollama run`'s CLI
  visibly separates `Thinking...` / `...done thinking.` from the actual reply.
  **Confirmed directly against the raw API this session** (`curl .../api/chat` with
  `stream: true`): each streamed chunk has separate `message.thinking` and
  `message.content` keys — `content` stays `""` while reasoning is in progress and only
  populates for the real reply. `brain/local.py`'s `OllamaBackend` already only reads
  `chunk["message"]["content"]`, so thinking tokens are correctly excluded from what
  reaches `Director`/TTS with no code change needed — verified, not just inferred from
  the CLI's rendering. Still worth deciding whether to pass `"think": false` in
  `OllamaBackend`'s payload to cut latency — chao's responses are short/tag-driven,
  unlikely to need multi-step reasoning, and thinking mode roughly doubles wall-clock
  time on top of an already-slow CPU path. Not changed this session — flagging as a
  decision, not making it unilaterally, since it trades latency for potential answer
  quality.
- `__main__.py`'s `DEFAULT_OLLAMA_MODEL` updated from the untested placeholder
  (`llama3.1:8b-instruct-q4_K_M`) to `qwen3:8b`; `.env.example`'s `CHAO_OLLAMA_MODEL`
  comment updated to match. The circuit breaker's local fallback path is now pointed at
  a real, working model, though its first-call latency (~2 min cold) is far past the
  5.0s `first_token_timeout_s` used for the *cloud* primary — that timeout only gates
  cloud's first token, not local's, so this doesn't break anything, but a fallback to
  local after a cloud failure will feel very slow to a live user. Not addressed this
  session (`OllamaBackend`'s `keep_alive` isn't configured — repeat calls within
  Ollama's default keep-alive window should skip the 57s reload).

Nothing else changed. Still not run: an actual end-to-end pipeline turn that falls
through to the local backend (item below).

## Dashboard skeleton (session 5, cont'd)

Built per design doc §11, scoped to the event feed panel only (§11.2 item
3) — see the top-of-file rationale for why the other eight panels wait.

**`src/chao/bus.py`** got one small addition first, needed before a
websocket-backed subscriber could exist safely: `Bus.subscribe()` had no
matching `unsubscribe()`, so every dashboard connection (a browser refresh
during frontend dev, a reconnect) would permanently leak a queue into
`_subscribers` — never removed, still paid for on every future
`_fan_out()`. Added `unsubscribe()` (a `list.remove`, one call site, no new
abstraction) and made `_fan_out` iterate `list(self._subscribers)` so a
subscriber unsubscribing itself mid-fan-out can't mutate the list being
iterated. Flagged to the user first since bus.py touches CLAUDE.md's
"escalate before changing the bus contract" rule — advisor input was that
this specific change is additive/mechanical (symmetric teardown for an
existing lifecycle method), not the kind of arbitration-semantics change
the rule is really guarding against, so it went ahead without a separate
Opus pass. New test: `test_unsubscribe_stops_further_fan_out`. 91 tests
passing at that point.

**`src/chao/dashboard/server.py`** — `create_app(bus) -> FastAPI`,
dependency-injected like `build_pipeline`. One route, `/ws/events`:
accepts the websocket, calls `bus.subscribe()`, relays every `Event` as
JSON (`dataclasses.asdict`) until disconnect, then `bus.unsubscribe()`s in
a `finally`. Checked what actually goes into `Event.payload` across
`turn.py`/`director.py`/`vts.py`/`bus.py` before trusting `send_json` on
it — all primitives (str/int/float/None/list[str]/dict[str,int]) today, so
no custom encoder needed; worth re-checking if a future payload field ever
holds something richer. CORS wide open (`allow_origins=["*"]`) since the
Vite dev server runs on a different port and this never leaves localhost.
New tests in `tests/test_dashboard_server.py`, using
`fastapi.testclient.TestClient`'s websocket support: one confirms a
published event round-trips over the wire, one confirms disconnect
actually calls `unsubscribe` (reaches into `bus._subscribers` directly —
acceptable for a whitebox test of exactly the leak this session set out to
fix). 93 tests passing, ruff clean.

**`src/chao/dashboard/web/`** — scaffolded fresh via `npm create
vite@latest . -- --template react-ts` (the directory only had a
`.gitkeep` before), per design doc §11.4's stack decision. Demo
boilerplate (counter, react/vite logos, marketing-page CSS) stripped
entirely. `App.tsx` is one component: opens a `WebSocket` to
`ws://127.0.0.1:8765/ws/events`, shows a connected/disconnected indicator,
accumulates `brain.token` chunks per `turn_id` into a live streaming
bubble (folded into a normal feed entry on that turn's `brain.complete`),
and renders everything else as a reverse-chronological list capped to 200
entries with kind-specific one-line summaries (latency for
`brain.complete`, pool→hotkey for `director.emote`, etc.). No router, no
state library, no env-var configuration for the WS URL — none of that is
earned yet for a one-screen skeleton. `npm run build` (tsc -b + vite
build) passes clean, confirming the TypeScript is sound even without a
browser to load it in.

**Wired into `__main__.py`:** `_print_events` (which printed everything —
tokens, latency, emotes, drops, errors) is gone. Replaced by two things:
`_run_dashboard_server`, which runs `uvicorn.Server(...).serve()` as an
`asyncio.Task` in the *same* process (not `uvicorn.run`, which would call
its own `asyncio.run` and fight the loop `main()` already owns — this
matters because the dashboard's websocket handler calls `bus.subscribe()`,
an in-process `asyncio.Queue`, not a network call); and `_print_errors`, a
~10-line stderr backstop that only surfaces `Kind.ERROR` — kept
deliberately, per advisor review, so a VTS auth failure or turn crash
isn't silently invisible on a run where nobody has the dashboard open
(first run of the day, uvicorn failed to bind, etc.). Startup now prints
the dashboard's websocket URL and the `npm run dev` command to launch the
frontend. Also fixed two stale bits of `__main__.py` while in there: the
`ANTHROPIC_API_KEY` error message still said Ollama "isn't installed
yet" (it is now), and `DEFAULT_OLLAMA_MODEL` still pointed at the
never-installed placeholder from session 3 (now `qwen3:8b`, matching the
Ollama work earlier this session).

**Verification, and its limits.** Ran the real app (`uv run python -m
chao`, via a named pipe so stdin could stay open across a background
task) and the real Vite dev server side by side, then connected a small
scripted Python client (`websockets.connect(...)`) directly to
`/ws/events` instead of a browser — the Chrome extension used for browser
automation wasn't connected in this environment (`tabs_context_mcp`
failed: "Browser extension is not connected"). Typed a real message into
the running app's stdin and confirmed the *exact* event sequence arrived
over the actual websocket: `input.manual` → `decision.selected` →
`brain.request` → `brain.token` (×2, real Anthropic streaming) →
`director.tag`/`director.emote` (×2, `happy` then `curious`, tags the
model chose unprompted) → `brain.complete` with real latency. This proves
the backend, the bus change, and the websocket wire format are all
correct — it's the same event stream `App.tsx` is built to parse. **What
it does not prove:** that the React component actually renders this
correctly in a real browser (CSS layout, the streaming-bubble/entry-list
split, the connected/disconnected indicator). That's a genuine gap, not
glossed over — see "Native window presentation" below for the follow-up
attempt with the (now-changed) production presentation layer.

## Native window presentation (session 5, cont'd)

After the dashboard skeleton above was committed, the user asked to swap
the presentation layer from a Chrome tab to a native OS window via
`pywebview`, specifically to avoid a full separate Chrome process's
baseline memory overhead — CLAUDE.md invariant 1 is literally about VRAM,
not RAM, but the underlying reason for it (this machine is also used for
gaming, background overhead is worth avoiding) applies in spirit. Design
doc §11.4 and §18's tech table updated to document the decision *before*
building it, per the user's explicit ask to update the plan first.

**What changed:**
- `chao.dashboard.server`: added `WEB_DIST = Path(__file__).parent / "web"
  / "dist"` and, after the websocket route, `app.mount("/", StaticFiles(...),
  ...)` when that directory exists — skipped gracefully if `npm run build`
  hasn't been run, so the `npm run dev` workflow is unaffected. Also moved
  `DASHBOARD_HOST`/`DASHBOARD_PORT` here from `__main__.py`, since they're
  properties of the dashboard server, not the brain — `__main__.py` now
  imports them rather than defining them, which also let the new
  `chao.dashboard.window` import them without an inverted dependency (a
  leaf module importing the composition root).
- `chao.dashboard.window` (new): `webview.create_window(...)` +
  `webview.start()`, pointed at the dashboard server's URL. Deliberately a
  separate process/entry point from `chao.__main__`, not a thread inside
  it — `webview.start()` blocks and needs the OS main thread for its GUI
  loop, which conflicts with `__main__.py`'s asyncio loop already owning
  that thread in the brain process. Run alongside `uv run python -m chao`,
  same relationship a browser tab would have.
- `pyproject.toml`: added `pywebview` (pulled in `pythonnet`/`clr-loader`
  for the Windows EdgeChromium/WebView2 backend).
- `CLAUDE.md`'s Commands section: added the window-launch command and
  `npm run build`; reworded the `npm run dev` line to make clear it's for
  frontend iteration, not normal use, now that the server self-serves the
  built frontend.
- New tests in `test_dashboard_server.py`: static files serve correctly
  when `WEB_DIST` (monkeypatched) points at a real directory with an
  `index.html`, and the mount is absent (404 on `/`) when it doesn't. 95
  tests passing, ruff clean.

**Verification, and its limits (round two).** Built the frontend for real
(`npm run build`), ran the actual app, and confirmed via `curl` that
`GET /` and `GET /assets/*.js` both return 200 with the correct built
HTML/JS — the static-serving code path is genuinely exercised, not just
unit-tested against a fake directory. Then ran
`python -m chao.dashboard.window` for real: no exception, no traceback,
and `msedgewebview2.exe` (six processes — WebView2's normal
browser/renderer/GPU-process split, the same architecture as Chrome)
stayed alive and running, consistent with the runtime having actually
loaded and rendered the page rather than failing silently. **Chrome
extension still wasn't available this session** (same limitation as the
dashboard skeleton work), so — same as before — nobody has visually
confirmed the rendered UI actually looks right inside the window. Process
health plus a confirmed-correct HTTP response is real evidence something
is working, but it is not the same as having seen it.

## Next actions

1. ~~Visually confirm the dashboard actually renders correctly~~ — **done,
   closed out.** The Chrome extension connected successfully once the
   user confirmed it was actually installed (the earlier failures this
   session were an environment/login-state issue, not a structural
   problem). Opened `http://127.0.0.1:8765/` in a real tab (proxy for the
   `pywebview` window, which renders the same HTML/CSS/JS via the same
   WebView2 engine) and pushed two real turns through the running app.
   Confirmed: dark theme renders correctly, "connected" status shows in
   accent color, both turns appeared in the feed in correct
   reverse-chronological order with correct latency/tags/emotes, and the
   streaming bubble folded cleanly into history on each `brain.complete`
   with no stuck or duplicate entries (both turns completed too fast,
   under ~1s, to catch a screenshot mid-stream — the fold-in logic is
   exercised by every successful turn regardless, so this isn't a real
   gap). The event feed panel is genuinely done, not just wired.
2. ~~Aliveness — the §10.5 anticipation nudge~~ — **done session 6**
   (`Aliveness` in `aliveness.py`, wired into `__main__.py` as
   `_run_aliveness`). ~~`mood.py` — valence/arousal state~~ — **done
   session 7** (`Mood`, tag-keyed deltas, lazy decay, wired as `_run_mood`;
   see that session's stopping-point writeup for the corrected claim about
   what hand-binding actually blocks). ~~§6.4's fly state machine +
   horizontal screen positioning~~ — **done session 9** (`Fly` in
   `aliveness.py`, `VTSFlySubscriber` in `outputs/vts.py`, wired as
   `_run_fly`; see session 9 part 2's writeup — live-verified, glides
   smoothly, ball trails correctly). ~~The timescale loop~~ — **done
   session 9 part 3, deliberately minimal: one ~1Hz `Mood.tick()`, not
   §10.1's full three-tier structure** (60Hz/meso still have no consumer —
   see part 3's writeup for why building only the one rate with a real
   consumer was the right call, per `advisor`). `Fly` now lands on its own
   during a quiet stretch; confirmed at the API/VTS-call level live, not
   re-watched by eye this particular run. What's left, in order:
   - The attention model (§10.3) — still the natural next consumer for a
     faster (meso, ~1Hz-ish) tick if it ends up needing one; don't assume
     it does until it's actually being built.
   - `motion.py` itself may not be needed at all: session 9's
     `MoveModelRequest` probe found VTS interpolates natively, so the
     "chao moves around" feature didn't need a code-side tween loop. If a
     future idle-drift need turns out to require continuous Live2D
     parameter injection (not a discrete API call like `MoveModelRequest`),
     motion.py becomes relevant then — not before.
   - **Hand-binding is still gated on a concrete target list:** it was
     NOT needed for horizontal flying (that uses `MoveModelRequest`, a
     direct API call, not a bound parameter) — that blocker only applies
     to whatever the timescale loop above ends up wanting to drive via
     continuous injection. Don't bind speculatively.
   - ~~Visually confirm the anticipation nudge in real VTS~~ — **done
     session 8.** Found a real bug in the process: `_run_vts_subscriber`
     subscribed to the bus *after* the VTS connect/authenticate round trip,
     so a fast first turn could fire the nudge before anything was
     listening — silently dropped, no error anywhere. Fixed by subscribing
     before that I/O. Confirmed live afterward: both the anticipation
     nudge and a tag-triggered `happy` emote fired and were visually seen.
3. `neutral` is shelved, not urgent — nothing fires it yet. When it
   resurfaces: either figure out the model's rest-state parameter values
   well enough to author a real `neutral.exp3.json` (may need the
   rigger's help, per the follow-up note above), or accept
   `RemoveAllExpressions` as the permanent design and give
   `VTSEmoteSubscriber` a special case for `HotkeyTriggerRequest`.
4. ~~If/when Ollama gets installed, pull a real model~~ — done this session
   (`qwen3:8b`, CPU-only, confirmed working, thinking-token exclusion
   verified against the raw API). Follow-ups still outstanding: (a) whether
   to pass `"think": false` to cut latency — **user decision: defer until
   chao is rigged and live, so the effect on real interactions can be felt
   rather than guessed at**, not a blocker for anything else; (b) run one
   real end-to-end turn through the local fallback path (not just a
   standalone `ollama run`) to see the whole chain behave, ideally with
   `keep_alive` considered so it's not paying the 57s cold-load tax every
   time.
5. Optionally close the minor gap: slider-test `Param`–`Param5` in VTS (low
   priority, quick, not expected to change any conclusion).
6. Phase 2 kickoff: ~~pick a TTS model~~ — **done session 8, Piper**
   (confirmed, not changed — the only CPU/zero-VRAM option at the doc's
   200ms latency budget). ~~Install Piper + pick a placeholder voice~~ —
   **done session 9** (`piper-tts` installed, `en_US-amy-medium`
   downloaded to `data/voices/`, `outputs/tts.py`'s `PiperBackend` built
   and verified against the real model). Still open: the childlike paid
   voice (user preparing one separately, config swap later), and wiring
   `tts.py` into the live turn pipeline (`turn.py`, `output.speech_*`
   events, a real `outputs/audio.py` for virtual-cable playback) — not
   started, deliberately deferred since `motion.py` only needed the
   `AudioChunk` interface to exist, not a live pipeline. Subtitle overlay +
   personality/voice test segment (§18) still not started.
7. Once the dashboard's event feed panel has been used for a bit and its
   rough edges are known, consider the next panel per §11.2's priority
   order (retrieval trace and mood plot both still need their underlying
   features built first, so realistically this is a phase-6+ concern).
8. ~~Check whether `ANTHROPIC_API_KEY` is still valid~~ — **done session
   8, key is valid.** A live call through the real `AnthropicBackend`
   class succeeded cleanly. Session 6's two "failed before first token"
   incidents are still unexplained, but ruled out as a key problem —
   hasn't recurred since, not chased further.
