"""Idle drift: non-periodic idle motion for the head-turn/tilt axes
(design doc §10.1 micro tier, §10.2). Stateful, clock/rng-injected, no I/O
-- testable directly like aliveness.py's `Fly`, not like motion.py's
`extract_envelope` (this needs internal state across ticks, a pure
step-in/value-out function wouldn't fit the same way).

§10.2 is explicit: idle drift must be non-periodic -- "humans detect a
loop within about thirty seconds." First version of this module used a
continuous Ornstein-Uhlenbeck noise process, re-injected every frame.
Live-verified and rejected by the user (session 10 part 8): the result
read as constant "micro jerks," not smooth drift -- because
`InjectParameterDataRequest` does not interpolate (unlike
`MoveModelRequest`, confirmed session 9), so *every* OU step, however
small, was a real visual snap, all the time.

Redesigned around the user's own description: "slow movement from point A
to point B, not micro nudges... the drift can be extreme." `IdleDrift` is
a point-to-point state machine (hold at a target, ease to a new target,
hold again) with the easing curve computed and injected by this code frame
by frame, since VTS won't do it for this parameter. This inverts the
earlier rate/amplitude tradeoff (advisor, second review): a multi-second
ease across a much larger range has *smaller* per-frame deltas than the
old noise process had at a much smaller range, so it can be simultaneously
bigger and smoother.

Deliberately drives ChaoHeadTurn (ParamAngleX) and ChaoHeadTilt
(ParamAngleZ) only, not ChaoHeadBob (ParamAngleY) -- see the module's
first version's docstring reasoning, unchanged by this redesign: leaving
bob untouched sidesteps the "is the chao currently speaking" coordination
problem entirely.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class IdleDriftConfig:
    x_parameter_name: str = "ChaoHeadTurn"
    z_parameter_name: str = "ChaoHeadTilt"
    # Injection rate for the eased move *and* the resend heartbeat during a
    # hold (VTS drops an un-resent tracking parameter after ~1s). Higher
    # than the old noise process's 10Hz on purpose -- advisor's second
    # review: a 2s ease across a wide range at 15Hz is still a smaller
    # per-frame delta than the old process had at a much smaller range.
    fps: float = 15.0
    # Target-selection bounds -- NOT a clamp on a continuous process like
    # the old `amplitude` field was. Each new target is drawn uniformly
    # from [-x_amplitude, x_amplitude] / [-z_amplitude, z_amplitude].
    # Deliberately extreme per the user's explicit direction ("the more
    # animated the more fun this character becomes... it's a drawing").
    # Split by axis, not shared, per advisor: tilt reads stronger per
    # degree than turn on most rigs, so z gets a smaller bound than x --
    # an untuned guess, not a measured one.
    x_amplitude: float = 22.0
    z_amplitude: float = 12.0
    # A newly-drawn target within this 2D distance of the current position
    # is rejected and redrawn (up to a bounded number of attempts). Without
    # this, an occasional near-adjacent draw produces an ease that's
    # visually indistinguishable from an extra hold -- a dead stretch of
    # 10+ seconds where nothing looks like it's happening (advisor flagged
    # this as `Fly._maybe_reposition`'s same latent issue, worse here
    # because point-to-point motion is the *primary* behavior, not a rare
    # event).
    min_move_distance: float = 8.0
    # Live-verified once at 1.5-4.0s/3.0-8.0s ("best it has looked so
    # far"), then sped up ~2.5x on the user's follow-up: idle read as
    # visibly slower than the speech-driven bob, wanted closer in pace,
    # not just a bigger range.
    ease_min_s: float = 0.6
    ease_max_s: float = 1.5
    hold_min_s: float = 2.0
    hold_max_s: float = 5.0
    # The VTS-side custom parameters' own range, used only when
    # (re)creating them via ParameterCreationRequest -- matches
    # ParamAngleX/Z's actual bound range (rigging_check_list.md item 5),
    # same role as MotionConfig.param_min/max for ChaoHeadBob.
    param_min: float = -30.0
    param_max: float = 30.0


def load_idle_drift_config(path: Path) -> IdleDriftConfig:
    """I/O lives here, not on IdleDrift -- same load/assemble split as
    motion.py's load_motion_config. Reads the `idle_drift:` block out of
    `config/chao.yaml`.
    """
    data = yaml.safe_load(path.read_text()) or {} if path.exists() else {}
    raw = data.get("idle_drift") or {}
    defaults = IdleDriftConfig()
    return IdleDriftConfig(
        x_parameter_name=str(raw.get("x_parameter_name", defaults.x_parameter_name)),
        z_parameter_name=str(raw.get("z_parameter_name", defaults.z_parameter_name)),
        fps=float(raw.get("fps", defaults.fps)),
        x_amplitude=float(raw.get("x_amplitude", defaults.x_amplitude)),
        z_amplitude=float(raw.get("z_amplitude", defaults.z_amplitude)),
        min_move_distance=float(raw.get("min_move_distance", defaults.min_move_distance)),
        ease_min_s=float(raw.get("ease_min_s", defaults.ease_min_s)),
        ease_max_s=float(raw.get("ease_max_s", defaults.ease_max_s)),
        hold_min_s=float(raw.get("hold_min_s", defaults.hold_min_s)),
        hold_max_s=float(raw.get("hold_max_s", defaults.hold_max_s)),
        param_min=float(raw.get("param_min", defaults.param_min)),
        param_max=float(raw.get("param_max", defaults.param_max)),
    )


def smoothstep(t: float) -> float:
    """Ease-in-out over [0, 1], clamped outside that range. `3t^2 - 2t^3`
    -- zero velocity at both endpoints, which is why a `MoveModelRequest`
    glide (session 9, VTS's own interpolation) and this manually-computed
    one both read as "gliding," not "coasting to a sudden stop."
    """
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


_MAX_TARGET_DRAW_ATTEMPTS = 20


@dataclass
class IdleDrift:
    """Point-to-point idle motion for the (x=turn, z=tilt) pair, moved
    together as one 2D target -- "point A to point B," per the user's own
    framing, not two independently-timed axes. Same clock/rng-injection
    shape as `Fly` (aliveness.py): `tick()` reads `self.clock()` itself
    rather than taking `now` as a parameter, and is deterministic given a
    seeded `rng`.

    Two phases, alternating: `"holding"` (steady at the current target,
    `tick()` returns the same value every call -- the actual property the
    user asked for) and `"easing"` (interpolating from the phase's start
    value to a freshly-drawn target over a randomly chosen duration, via
    `smoothstep`). A phase's duration is drawn once, when it starts, from
    `ease_min_s..ease_max_s` or `hold_min_s..hold_max_s`.
    """

    config: IdleDriftConfig
    clock: Callable[[], float] = time.monotonic
    rng: random.Random = field(default_factory=random.Random)

    x_value: float = field(default=0.0, init=False)
    z_value: float = field(default=0.0, init=False)
    _x_start: float = field(default=0.0, init=False)
    _z_start: float = field(default=0.0, init=False)
    _x_target: float = field(default=0.0, init=False)
    _z_target: float = field(default=0.0, init=False)
    _phase: str = field(default="holding", init=False)
    _phase_start: float = field(init=False)
    _phase_duration: float = field(init=False)

    def __post_init__(self) -> None:
        self._phase_start = self.clock()
        self._phase_duration = self.rng.uniform(self.config.hold_min_s, self.config.hold_max_s)

    def tick(self) -> tuple[float, float]:
        now = self.clock()
        elapsed = now - self._phase_start
        if elapsed >= self._phase_duration:
            self._advance_phase(now)
            elapsed = 0.0
        if self._phase == "easing":
            t = smoothstep(elapsed / self._phase_duration) if self._phase_duration > 0 else 1.0
            self.x_value = self._x_start + (self._x_target - self._x_start) * t
            self.z_value = self._z_start + (self._z_target - self._z_start) * t
        return self.x_value, self.z_value

    def _advance_phase(self, now: float) -> None:
        if self._phase == "holding":
            self._phase = "easing"
            self._x_start, self._z_start = self.x_value, self.z_value
            self._x_target, self._z_target = self._pick_target()
            self._phase_duration = self.rng.uniform(self.config.ease_min_s, self.config.ease_max_s)
        else:
            # Snap exactly to the target, not whatever the last interpolated
            # frame's t landed on -- a late tick() call (scheduling jitter,
            # or a test jumping the clock far past the deadline) must not
            # leave the value short of the intended destination.
            self._phase = "holding"
            self.x_value, self.z_value = self._x_target, self._z_target
            self._phase_duration = self.rng.uniform(self.config.hold_min_s, self.config.hold_max_s)
        self._phase_start = now

    def _pick_target(self) -> tuple[float, float]:
        x, z = self.x_value, self.z_value
        for _ in range(_MAX_TARGET_DRAW_ATTEMPTS):
            x = self.rng.uniform(-self.config.x_amplitude, self.config.x_amplitude)
            z = self.rng.uniform(-self.config.z_amplitude, self.config.z_amplitude)
            if math.hypot(x - self.x_value, z - self.z_value) >= self.config.min_move_distance:
                break
        return x, z
