"""Mood: valence/arousal state. See design doc §6, §10.

Scope for this pass, deliberately narrow, mirroring aliveness.py's precedent:

- `Mood` subscribes to `director.tag` only. Each recognized tag applies its
  §6.2 valence/arousal delta, clamped to [-1, 1], on top of whatever the
  point has decayed to since the last update.
- Decay is lazy: computed from `now - last_update` whenever a tag arrives,
  not on a timer. There's no periodic tick yet because nothing in the
  codebase runs one -- aliveness.py owns the micro/meso/macro timescales
  (§10.1) and will call `Mood.tick(now)` once its own timer loop exists.
  Between tags the point is frozen for dashboard purposes; the math is
  still correct, it just isn't re-emitted until something moves it.
- Publishes `director.mood` (a new bus contract as of this module -- payload
  is `{valence, arousal, baseline_valence, baseline_arousal}`) so the
  dashboard's event feed and eventual mood plot (§11.2 item 4) can render
  it; nothing subscribes yet besides the generic event-feed relay.

Not in this pass, deliberately: `fly`'s hysteresis (§6.4, aliveness.py's
job -- it owns `state.fly`) and heart gating (§6.3, needs per-viewer
affinity from memory, phase 6). Both already have config parked in
emotes.yaml's `fly:`/`heart_gate:` blocks; this module doesn't touch them.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from chao.events import Event, Kind

# §6.2's tag -> (valence delta, arousal delta) table. Keyed on the tag
# itself, never the emote pool -- director.py's _TAG_TO_POOL maps both
# "happy" and "affection" to the same "happy" pool, but §6.2 gives them
# different deltas. "thinking" is deliberately absent: §6.2 marks it
# "transient only", i.e. no mood nudge, not a zero-valued one. "pause" and
# "look:*" (director.py's `_tag_key` renders these as "look:chat" etc.)
# aren't mood signals either and fall through the same lookup miss.
_TAG_DELTAS: dict[str, tuple[float, float]] = {
    "happy": (0.7, 0.6),
    "affection": (0.9, 0.3),
    "curious": (0.1, 0.4),
    "surprise": (0.0, 0.9),
    "confused": (-0.2, 0.4),
    "sad": (-0.7, -0.4),
    "angry": (-0.6, 0.8),
}


def decay(current: float, baseline: float, elapsed: float, half_life_s: float) -> float:
    """Exponential decay of `current` toward `baseline` over `elapsed`
    seconds, given a half-life. Pure function, tested directly per
    CLAUDE.md's testing conventions -- no sine waves, no stepwise
    approximation, just distance-from-baseline halving every `half_life_s`.
    """
    if elapsed <= 0 or half_life_s <= 0:
        return current
    factor = 0.5 ** (elapsed / half_life_s)
    return baseline + (current - baseline) * factor


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


@dataclass(frozen=True, slots=True)
class MoodConfig:
    baseline_valence: float = 0.0
    baseline_arousal: float = 0.0
    half_life_s: float = 20.0


def load_mood_config(path: Path) -> MoodConfig:
    """I/O lives here, not on Mood -- same load/assemble split as
    director.py's load_emote_config. Reads the `mood:` block out of the
    same emotes.yaml file `fly:`/`heart_gate:` are already parked in.
    """
    data = yaml.safe_load(path.read_text()) or {}
    raw = data.get("mood") or {}
    return MoodConfig(
        baseline_valence=float(raw.get("baseline_valence", 0.0)),
        baseline_arousal=float(raw.get("baseline_arousal", 0.0)),
        half_life_s=float(raw.get("half_life_s", 20.0)),
    )


@dataclass
class Mood:
    config: MoodConfig
    publish: Callable[[Event], None]
    clock: Callable[[], float] = time.monotonic

    valence: float = field(init=False)
    arousal: float = field(init=False)
    _last_update: float = field(init=False)

    def __post_init__(self) -> None:
        self.valence = self.config.baseline_valence
        self.arousal = self.config.baseline_arousal
        self._last_update = self.clock()

    def handle(self, event: Event) -> None:
        if event.kind != Kind.DIRECTOR_TAG:
            return
        delta = _TAG_DELTAS.get(event.payload.get("tag"))
        if delta is None:
            return

        now = self.clock()
        elapsed = now - self._last_update
        self.valence = decay(
            self.valence, self.config.baseline_valence, elapsed, self.config.half_life_s
        )
        self.arousal = decay(
            self.arousal, self.config.baseline_arousal, elapsed, self.config.half_life_s
        )
        self._last_update = now

        dv, da = delta
        self.valence = _clamp(self.valence + dv)
        self.arousal = _clamp(self.arousal + da)

        self.publish(
            Event(
                kind=Kind.DIRECTOR_MOOD,
                turn_id=event.turn_id,
                payload={
                    "valence": self.valence,
                    "arousal": self.arousal,
                    "baseline_valence": self.config.baseline_valence,
                    "baseline_arousal": self.config.baseline_arousal,
                },
            )
        )
