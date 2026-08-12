"""Aliveness layer: behaviour that runs independently of the LLM. See
design doc §10.

Scope for this pass, deliberately narrow: just the anticipation nudge
(§10.5) — "build it in phase 1" per the design doc, and the only piece of
§10 buildable end-to-end right now. `Aliveness.handle` reacts to
`brain.request` by popping the ball's question-mark expression before any
audio exists, covering the 0.5-1.5s latency window with in-character
behaviour instead of dead air. The head-tilt half of §10.5 is not built —
same reason as the rest of §10, see below.

Deliberately does **not** share Director's cooldown/hotkey-alternation
state for the `curious` pool. The anticipation nudge fires at turn start;
a `[curious]`/`[thinking]` tag in the reply itself typically lands well
inside `curious`'s 3s cooldown window. Sharing state would mean the
anticipation nudge systematically suppresses the LLM's own semantic
signal — silently, since nothing would look broken. Firing independently
risks the opposite, benign case instead: two activations of the same
expression file close together, which `VTSEmoteSubscriber` already handles
correctly (cancels the pending deactivate and extends the hold, rather
than a stale deactivate cutting the new one short).

Also built this pass: `Fly`, §6.4's fly state machine, plus horizontal
screen positioning (session 9). This does NOT share the rest of §10's
hand-binding blocker — `MoveModelRequest` is a direct VTS API call, not a
custom Live2D parameter, so it needs no manual binding step. A live probe
(SESSION_STATE.md session 9) confirmed it interpolates smoothly over
`timeInSeconds` rather than snapping, so there's no tween loop to write:
picking a destination and publishing it is the whole job.

Also built this pass: a single ~1Hz `Mood.tick()` call, wired into
`__main__.py`'s `_run_mood` (mood.py owns the tick method and the timing
config; this module's `Fly` is the actual consumer — it's what lets a
flying chao land on its own without waiting on a fresh tag). This is
deliberately NOT §10.1's full micro/meso/macro three-tier structure — just
the one rate that has a real consumer today. See mood.py's docstring for
the tick/publish design, and `Fly._on_mood`'s docstring for why
repositioning specifically ignores tick-sourced mood events.

Still not built, deliberately: idle-drift noise (§10.2) and the attention
model (§10.3), and therefore the 60Hz/meso tiers of §10.1 — those
genuinely need continuous parameter injection, which per CLAUDE.md's
phase-0 finding requires new custom VTS parameters bound by hand in the
VTS UI first, a real dependency on work only the user can do (see
SESSION_STATE.md's next actions). Building the noise generators now, with
no bound parameter to watch them drive, would repeat the "UI over nothing"
mistake the dashboard's other panels deliberately avoided.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from chao.director.director import EmoteConfig
from chao.events import Event, Kind

_DEFAULT_POOL = "curious"


@dataclass
class Aliveness:
    emote_config: EmoteConfig
    publish: Callable[[Event], None]
    rng: random.Random = field(default_factory=random.Random)

    def handle(self, event: Event) -> None:
        if event.kind != Kind.BRAIN_REQUEST:
            return

        pool_name = self.emote_config.reactions.get("anticipation", _DEFAULT_POOL)
        pool = self.emote_config.pools.get(pool_name)
        if pool is None or not pool.hotkeys:
            return

        self.publish(
            Event(
                kind=Kind.DIRECTOR_EMOTE,
                turn_id=event.turn_id,
                payload={
                    "pool": pool_name,
                    "hotkey_id": self.rng.choice(pool.hotkeys),
                    "reason": "anticipation",
                },
            )
        )


@dataclass(frozen=True, slots=True)
class FlyConfig:
    hotkey: str = "fly.exp3.json"
    on_above: float = 0.70
    off_below: float = 0.40
    min_dwell_s: float = 5.0
    x_min: float = -0.5
    x_max: float = 0.5
    move_duration_s: float = 2.0
    land_duration_s: float = 1.5
    min_reposition_s: float = 8.0


def load_fly_config(path: Path) -> FlyConfig:
    """I/O lives here, not on Fly — same load/assemble split as
    load_mood_config. Reads the `fly:` block out of the same emotes.yaml
    file `mood:`/`heart_gate:` are already parked in.
    """
    data = yaml.safe_load(path.read_text()) or {}
    raw = data.get("fly") or {}
    defaults = FlyConfig()
    x_min, x_max = raw.get("x_range", [defaults.x_min, defaults.x_max])
    return FlyConfig(
        hotkey=str(raw.get("hotkey", defaults.hotkey)),
        on_above=float(raw.get("on_above", defaults.on_above)),
        off_below=float(raw.get("off_below", defaults.off_below)),
        min_dwell_s=float(raw.get("min_dwell_s", defaults.min_dwell_s)),
        x_min=float(x_min),
        x_max=float(x_max),
        move_duration_s=float(raw.get("move_duration_s", defaults.move_duration_s)),
        land_duration_s=float(raw.get("land_duration_s", defaults.land_duration_s)),
        min_reposition_s=float(raw.get("min_reposition_s", defaults.min_reposition_s)),
    )


@dataclass
class Fly:
    """§6.4's fly state machine: on/off hysteresis over arousal, with a
    minimum dwell time in either state so it doesn't flutter at the
    boundary, plus the two hard rules — land on `[sad]` regardless of
    arousal, and (§10.6, not built yet) land during the low-energy macro
    state.

    Driven by two event kinds:

    - `director.mood` supplies arousal for the hysteresis check —
      launching and landing both react to *any* `director.mood` arrival,
      tag-sourced or tick-sourced (session 9's `Mood.tick`), which is what
      lets a flying chao land on its own during a quiet stretch instead of
      needing a fresh tag. Repositioning is different: it's gated on
      `event.payload["source"] == "tag"` specifically. A tick is a
      background poll, not activity — letting it also trigger
      repositioning would make a flying chao pace to a new spot every
      `min_reposition_s` forever with zero activity, exactly what §10.2's
      "noise, not sine waves" warns against. Tag-sourced events are real
      activity (a tag the LLM actually emitted), so those are what
      "motivated repositioning" means here.
    - `director.tag` is only checked for an unconditional "sad" tag, which
      lands immediately — bypassing both the arousal threshold and
      `min_dwell_s`. A chao that just turned sad shouldn't stay aloft for
      up to `min_dwell_s` more seconds because the tag landed right after
      a launch.

    Landing during a quiet stretch works as of session 9's `Mood.tick` +
    `__main__.py`'s `_run_mood` timeout loop — arousal now actually
    re-decays and re-publishes on a timer, not just when a tag arrives.

    Publishes `state.fly` (§13's existing kind, extended payload):
    `{flying, trigger, target_x}`. `target_x` is the new horizontal
    destination when `flying` is True, and `None` when landing — a
    subscriber reads `None` as "glide back to the resting position", not
    "stay put".
    """

    config: FlyConfig
    publish: Callable[[Event], None]
    clock: Callable[[], float] = time.monotonic
    rng: random.Random = field(default_factory=random.Random)

    flying: bool = field(default=False, init=False)
    target_x: float = field(default=0.0, init=False)
    _last_transition: float = field(init=False)
    _last_reposition: float = field(init=False)

    def __post_init__(self) -> None:
        now = self.clock()
        self._last_transition = now
        self._last_reposition = now

    def handle(self, event: Event) -> None:
        if event.kind == Kind.DIRECTOR_MOOD:
            self._on_mood(event)
        elif event.kind == Kind.DIRECTOR_TAG:
            self._on_tag(event)

    def _on_mood(self, event: Event) -> None:
        arousal = event.payload.get("arousal")
        if arousal is None:
            return

        now = self.clock()
        if not self.flying and arousal > self.config.on_above:
            self._transition(True, "arousal_high", now, event.turn_id)
        elif self.flying and arousal < self.config.off_below:
            if now - self._last_transition >= self.config.min_dwell_s:
                self._transition(False, "arousal_low", now, event.turn_id)
        elif self.flying and event.payload.get("source") == "tag":
            self._maybe_reposition(now, event.turn_id)

    def _on_tag(self, event: Event) -> None:
        if not self.flying or event.payload.get("tag") != "sad":
            return
        self._transition(False, "sad", self.clock(), event.turn_id)

    def _maybe_reposition(self, now: float, turn_id: str | None) -> None:
        if now - self._last_reposition < self.config.min_reposition_s:
            return
        self._last_reposition = now
        self.target_x = self.rng.uniform(self.config.x_min, self.config.x_max)
        self._publish(turn_id, "reposition")

    def _transition(self, flying: bool, trigger: str, now: float, turn_id: str | None) -> None:
        self.flying = flying
        self._last_transition = now
        self._last_reposition = now
        if flying:
            self.target_x = self.rng.uniform(self.config.x_min, self.config.x_max)
        self._publish(turn_id, trigger)

    def _publish(self, turn_id: str | None, trigger: str) -> None:
        self.publish(
            Event(
                kind=Kind.STATE_FLY,
                turn_id=turn_id,
                payload={
                    "flying": self.flying,
                    "trigger": trigger,
                    "target_x": self.target_x if self.flying else None,
                },
            )
        )
