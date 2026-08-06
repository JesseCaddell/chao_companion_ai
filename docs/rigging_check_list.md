# Rigging check list

Asks for the model's artist, discovered while building against the live rig.
Nothing here blocks progress unless marked a hard stop — items get scheduled
against the phase that actually needs them (design doc §17).

## Open

### 1. Ball position parameter

**Needed for:** §8.2 ball spring (damped lag behind head motion), Phase 2.

**Finding:** The current rig has no parameter that moves the ball/bubble's
on-screen position. Confirmed via VTS's Live2D parameter test panel — every
position-shaped candidate (`xp`, `xp2`, `xp3`, `yp3`, `yp4`) produces no visible
motion, and a live head-drag/webcam test showed no lag on the ball or its bubble
on its own. The existing `bubble` value drift is idle flame-flicker on the aura
sprite, not positional physics.

**Ask:** A parameter (or two, X/Y) that translates the ball+bubble group, driven
externally, so it can be sprung to lag the head per §8.2. Doesn't need a large
range — the spring overshoots slightly and settles in ~1/3 second, so a small
travel range is enough.

**Status:** Not a hard stop. Deferred; should land before Phase 2 starts if we
still want this feature. Confirmed *not* buildable in code alone regardless.

## Resolved

(none yet)
