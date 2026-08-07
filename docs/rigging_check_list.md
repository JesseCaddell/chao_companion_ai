# Rigging check list

Asks for the model's artist, discovered while building against the live rig.
Nothing here blocks progress unless marked a hard stop — items get scheduled
against the phase that actually needs them (design doc §17).

## Open

### 1. Ball position parameter — drive-chain verification

**Needed for:** §8.2 ball spring (damped lag behind head motion), Phase 2.

**Update:** The rigger added a dedicated physics group for the ball:
`xp4` (X), `yp5` (Y), and `yp6` (bubble scale, −1..1 shrink/grow — not
positional, part of the same new group). Lag confirmed live in VTS by the
artist — the physics group now bakes in §8.2's damped-spring behavior
natively, so **no code-side spring is needed for ball position**. This
replaces the original finding below, which no longer applies now that the
parameters exist.

**What's still open:** the physics group needs *something* driving its
input to lag behind. With face tracking off (§7.3), the only thing that can
drive head motion is a plugin-injected parameter — and `InjectParameterDataRequest`
can only write custom "tracking" parameters (the phase 0 error-453 finding),
which then need a one-time manual bind in the VTS UI to a head Live2D
output. That bind hasn't been confirmed to exist yet. If nothing is bound
to head motion, `xp4`/`yp5` will sit still during an autonomous session even
though they move correctly under a manual slider test or webcam drag — the
same kind of thing phase 0 originally found for `happy`.

**Ask (verification, not the artist):** before phase 2 relies on this, bind
a custom tracking param to a head output (`ParamAngleX`/`Y`) if not already
done, inject into it, and confirm `xp4`/`yp5` visibly lag in response — the
same pattern already proven for `happy` in phase 0.

**Status:** Not a hard stop. The rigging half is done; a plugin-side
verification pass remains before Phase 2 starts.

## Resolved

(none yet — item 1's rigging half is done, but it stays open until the
drive-chain verification above is confirmed.)
