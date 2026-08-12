"""Mood: valence/arousal state. See design doc §6, §10.

Scope for this pass, deliberately narrow, mirroring aliveness.py's precedent:

- `Mood.handle` reacts to `director.tag`: each recognized tag applies its
  §6.2 valence/arousal delta, clamped to [-1, 1], on top of whatever the
  point has decayed to since the last update.
- `Mood.tick(now)` (session 9) is the periodic path promised by this
  docstring's earlier draft -- `__main__.py`'s `_run_mood` now calls it on
  a timeout when no event arrives within `tick_interval_s`, so decay
  actually progresses during quiet stretches instead of being frozen until
  the next tag. Both paths share `_decay_to`, the same `decay()` call --
  this matters: a tick immediately before a tag must land on the exact
  same point a tag alone would, decay being exponential (and therefore
  step-invariant) makes that true as long as the code path is shared, not
  reimplemented.
- `tick()` only publishes `director.mood` when the point actually moved by
  more than `_TICK_PUBLISH_EPSILON` since the last tick -- near baseline,
  decay asymptotes and successive ticks move it by less and less, so this
  naturally goes quiet rather than putting one event/tick on the bus
  forever. `handle()` always publishes; a tag is real activity, not a
  background poll.
- Publishes `director.mood` with payload `{valence, arousal,
  baseline_valence, baseline_arousal, source}`. `source` is `"tag"` or
  `"tick"` -- `aliveness.py`'s `Fly` reads it to tell real activity from a
  background poll, since only tag-sourced events should trigger picking a
  new horizontal fly target (see `Fly._on_mood`'s docstring: a tick
  triggering repositioning would make a flying chao pace on a timer with
  zero activity, exactly what §10.2 warns against).

Not in this pass, deliberately: heart gating (§6.3, needs per-viewer
affinity from memory, phase 6) -- `emotes.yaml`'s `heart_gate:` block is
still aspirational.
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


# How far valence/arousal must move in a single tick before it's worth a
# director.mood event. Not a YAML tuning knob like the deltas/half-life
# above -- this is a numerical noise floor for event-bus volume, not a
# "feel" parameter, and moving it doesn't change any observable behavior
# except how chatty the tick path is.
_TICK_PUBLISH_EPSILON = 1e-4


@dataclass(frozen=True, slots=True)
class MoodConfig:
    baseline_valence: float = 0.0
    baseline_arousal: float = 0.0
    half_life_s: float = 20.0
    tick_interval_s: float = 1.0


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
        tick_interval_s=float(raw.get("tick_interval_s", 1.0)),
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

        self._decay_to(self.clock())

        dv, da = delta
        self.valence = _clamp(self.valence + dv)
        self.arousal = _clamp(self.arousal + da)

        self._publish(event.turn_id, "tag")

    def tick(self, now: float | None = None) -> None:
        """Periodic path (§10.1's timescale loop) -- lets decay progress,
        and lets a downstream state machine like `Fly` re-evaluate arousal,
        without waiting on the next tag. See module docstring for the
        shared-decay-path and epsilon-gating rationale.
        """
        prev_valence, prev_arousal = self.valence, self.arousal
        self._decay_to(self.clock() if now is None else now)

        moved = (
            abs(self.valence - prev_valence) > _TICK_PUBLISH_EPSILON
            or abs(self.arousal - prev_arousal) > _TICK_PUBLISH_EPSILON
        )
        if moved:
            self._publish(None, "tick")

    def _decay_to(self, now: float) -> None:
        elapsed = now - self._last_update
        self.valence = decay(
            self.valence, self.config.baseline_valence, elapsed, self.config.half_life_s
        )
        self.arousal = decay(
            self.arousal, self.config.baseline_arousal, elapsed, self.config.half_life_s
        )
        self._last_update = now

    def _publish(self, turn_id: str | None, source: str) -> None:
        self.publish(
            Event(
                kind=Kind.DIRECTOR_MOOD,
                turn_id=turn_id,
                payload={
                    "valence": self.valence,
                    "arousal": self.arousal,
                    "baseline_valence": self.config.baseline_valence,
                    "baseline_arousal": self.config.baseline_arousal,
                    "source": source,
                },
            )
        )
