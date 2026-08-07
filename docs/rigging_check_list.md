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
