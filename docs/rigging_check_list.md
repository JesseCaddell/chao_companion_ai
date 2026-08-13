# Rigging check list

Asks for the model's artist, discovered while building against the live rig.
Nothing here blocks progress unless marked a hard stop — items get scheduled
against the phase that actually needs them (design doc §17).

## Open

(none currently)

## Resolved

### 1. Ball position parameter

**Needed for:** §8.2 ball spring (damped lag behind head motion), Phase 2.

**Finding:** The rigger added a dedicated physics group for the ball:
`xp4` (X), `yp5` (Y), and `yp6` (bubble scale, −1..1 shrink/grow — not
positional, part of the same new group). The group has its own native
lag baked in — §8.2's damped-spring behavior is now handled by the rig,
not code.

**Verification:** confirmed by the user directly — moving the model around
in VTS with face tracking off, the ball visibly lags behind. No plugin-side
binding or code-side spring is needed; the physics group reacts on its own
to model movement regardless of what's driving it.

**Status:** Resolved. No further action needed before Phase 2.

### 2. Default model position/size baseline

**Needed for:** any future feature that changes `size` (e.g. "chao flies
closer to camera") or `positionY` — need to know what "default"/"resting"
actually is to offset from it, and to know what to restore afterward.

**Finding:** `CurrentModelRequest`'s `modelPosition`, sampled three times
live in session 10 (a few minutes apart, always after the model had
settled): `positionX` ≈ -0.026 (small jitter — idle sway, not meaningful),
`positionY` = -0.10491454601287842 (identical all three samples),
`rotation` = 360.0 (identical all three). `size` was **not** stable:
-81.39791107177734 on the first two samples (taken back to back), then
-87.11779022216797 on a third sample taken a couple minutes later —
correcting an earlier draft of this entry that claimed `size` was fixed
based on only the first two, coincidentally-close samples. Reads as VTS's
own idle animation subtly breathing the zoom level, not something this
project's code is driving. **A future "fly closer to camera" feature
cannot just snapshot `size` once and treat it as "the" default** — it
needs either a live re-sample right before use, or to store a relative
delta rather than an absolute target.

**Status:** Partially resolved. `positionY`/`rotation` look like genuine
fixed baseline values (repeatable across samples); `size` does not and
needs more samples over a longer window to characterize its idle range
before anything depends on it.

### 3. Vertical range for a future fly-Y feature

**Needed for:** a possible future extension of §6.4's fly state machine to
move vertically as well as horizontally. **Not wired into any code yet** —
`Fly`/`VTSFlySubscriber` only move `positionX`; `positionY` is always held
at whatever baseline was captured on the first flight. This entry is a
recorded reference value for when/if that feature gets built, same as
item 2 above — not a claim that vertical flying exists.

**Finding:** Eyeballed live against the real OBS-capture framing, from a
**centered** position (`positionX: 0, positionY: 0`, not the model's
natural resting baseline — the user asked to re-center first after the
first pass, centered at the natural baseline (`positionY` ≈ -0.105),
showed comfortable margin on top but the bottom running close to the
frame edge). From true center: ±0.3 was comfortable but under-used the
available room; ±0.5 gave good margin on both top and bottom. **Confirmed:
`positionY` in `[-0.5, 0.5]` from a centered `(0, 0)` position stays fully
on screen.**

**Status:** Resolved as a recorded reference value for future use. Note
the asymmetry versus item 2's `x_range`, which is confirmed relative to
the model's *natural resting position*, not a centered one — a future
fly-Y implementation should decide deliberately which reference point it
uses rather than assuming they match.
