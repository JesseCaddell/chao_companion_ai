"""Aliveness layer: behaviour that runs independently of the LLM. See
design doc §10.

Scope for this pass, deliberately narrow: just the anticipation nudge
(§10.5) — "build it in phase 1" per the design doc, and the only piece of
§10 buildable end-to-end right now. `Aliveness.handle` reacts to
`brain.request` by popping the ball's question-mark expression before any
audio exists, covering the 0.5-1.5s latency window with in-character
behaviour instead of dead air. The head-tilt half of §10.5 is not built —
same reason as the rest of §10, see below.

Session 10 part 10 added a second reaction: `chat_spike` (design doc
§4.1 tier 4, "velocity spike"), fired by `TwitchChatClient` tagging a
background chat message `priority: 4` while its own rolling-window
velocity tracker is above threshold. `Aliveness` fires whenever a
qualifying `input.chat` event arrives, gated by its own cooldown tracker
(see `_last_fired` below) since nothing else rate-limits a reaction fired
off a per-message chat event the way turn cadence naturally rate-limits
the anticipation nudge.

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

Update (session 10 parts 7-8, correcting this docstring's earlier claim):
idle-drift noise (§10.2) IS built — `director/idle_drift.py`'s `IdleDrift`,
point-to-point head-turn/tilt motion, live-verified and explicitly locked
in by the user ("so good right now, I don't want that changed" — session
11). The "needs hand-bound VTS parameters first" blocker that used to sit
here is resolved (`ChaoHeadTurn`/`ChaoHeadTilt`, part 7) and doesn't apply
to this module at all — `IdleDrift` lives in its own file, not here.

The attention model (§10.3) was investigated (session 10 part 3) and
explicitly declined (session 11): the user's call was that there's no
meaningful "place" for the chao to look, `[look:chat]`/`[look:you]` stay
as harmless no-op telemetry, and `IdleDrift`'s current autonomous behavior
is frozen as-is. Not a blocked feature — a closed question. Don't reopen
it without the user raising it again.

Still genuinely not built: the 60Hz/meso tiers of §10.1 beyond what
`IdleDrift`/`Mood.tick()` already cover, and §10.6's boredom/macro-state
accumulators (session 11's `Fly` boredom path is a narrow, Fly-specific
version of the idea, not the general macro-state system §10.6 describes).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from chao.director.director import EmoteConfig, EmotePool
from chao.events import Event, Kind

_DEFAULT_POOL = "curious"

_FLY_TARGET_FRACTIONS = {"left": 0.0, "right": 1.0, "center": 0.5}
_FLY_DIRECTIONS = frozenset(f"fly:{d}" for d in _FLY_TARGET_FRACTIONS)


@dataclass
class Aliveness:
    emote_config: EmoteConfig
    publish: Callable[[Event], None]
    rng: random.Random = field(default_factory=random.Random)
    clock: Callable[[], float] = time.monotonic

    # Cooldown bookkeeping for chat_spike only -- see _fire_with_cooldown.
    # anticipation stays intentionally ungated (below): it fires on every
    # single brain.request by design (see the existing
    # test_does_not_share_cooldown_with_tag_triggered_curious_emotes),
    # since gating it would let the nudge silently suppress a
    # [curious]/[thinking] tag's own emote on essentially every turn.
    _last_fired: dict[str, float] = field(default_factory=dict, init=False)

    def handle(self, event: Event) -> None:
        if event.kind == Kind.BRAIN_REQUEST:
            self._fire("anticipation", event.turn_id)
        elif event.kind == Kind.INPUT_CHAT and event.payload.get("priority") == 4:
            self._fire_with_cooldown("chat_spike", event.turn_id)

    def _fire(self, reaction_key: str, turn_id: str | None) -> None:
        pool_name, pool = self._resolve_pool(reaction_key)
        if pool is None:
            return
        self._publish_emote(pool_name, pool, reaction_key, turn_id)

    def _fire_with_cooldown(self, reaction_key: str, turn_id: str | None) -> None:
        """Only chat_spike uses this: unlike the anticipation nudge (paced
        by turn cadence), chat_spike fires once per qualifying chat
        message while a velocity spike persists -- several times a second
        with no gating otherwise. Aliveness deliberately doesn't share
        Director's own cooldown state (see module docstring), so this is a
        second, independent cooldown tracker, not a reuse of Director's.
        """
        pool_name, pool = self._resolve_pool(reaction_key)
        if pool is None:
            return

        now = self.clock()
        last = self._last_fired.get(reaction_key)
        if last is not None and now - last < pool.cooldown_s:
            return
        self._last_fired[reaction_key] = now
        self._publish_emote(pool_name, pool, reaction_key, turn_id)

    def _resolve_pool(self, reaction_key: str) -> tuple[str, EmotePool | None]:
        pool_name = self.emote_config.reactions.get(reaction_key, _DEFAULT_POOL)
        pool = self.emote_config.pools.get(pool_name)
        if pool is None or not pool.hotkeys:
            return pool_name, None
        return pool_name, pool

    def _publish_emote(
        self, pool_name: str, pool: EmotePool, reaction_key: str, turn_id: str | None
    ) -> None:
        self.publish(
            Event(
                kind=Kind.DIRECTOR_EMOTE,
                turn_id=turn_id,
                payload={
                    "pool": pool_name,
                    "hotkey_id": self.rng.choice(pool.hotkeys),
                    "reason": reaction_key,
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
    # Session 11: "adventurous or bored or any feeling" redesign. Default
    # 1.0 (always launch on crossing) preserves every pre-existing test's
    # deterministic behavior untouched -- only config/chao.yaml's real
    # value makes crossing genuinely a coin flip rather than a guarantee.
    launch_probability: float = 1.0
    # Boredom launch path: a second, independent way to take off besides
    # high arousal, so flying isn't a one-note "excited" signal. Requires
    # arousal to sit continuously below `boredom_below` for at least
    # `boredom_dwell_s` before becoming eligible, then rolls
    # `boredom_probability` on every mood event while eligible (not
    # edge-triggered like the high-arousal path -- being bored is a
    # sustained state, not a one-shot crossing, so repeated rolls while it
    # persists is the intended behavior). Default probability 0.0 disables
    # this path entirely unless configured, same "omitted/zero disables"
    # shape as brain/local.py's num_thread/think.
    boredom_below: float = 0.25
    boredom_dwell_s: float = 45.0
    boredom_probability: float = 0.0


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
        launch_probability=float(raw.get("launch_probability", defaults.launch_probability)),
        boredom_below=float(raw.get("boredom_below", defaults.boredom_below)),
        boredom_dwell_s=float(raw.get("boredom_dwell_s", defaults.boredom_dwell_s)),
        boredom_probability=float(raw.get("boredom_probability", defaults.boredom_probability)),
    )


@dataclass
class Fly:
    """§6.4's fly state machine: on/off hysteresis over arousal, with a
    minimum dwell time in either state so it doesn't flutter at the
    boundary, plus the two hard rules — land on `[sad]` regardless of
    arousal, and (§10.6, not built yet) land during the low-energy macro
    state.

    Session 11: launching is no longer a guaranteed consequence of crossing
    a threshold. Two independent paths, both probabilistic:

    - **High arousal** ("excited/adventurous"): crossing `on_above` rolls
      `launch_probability` once (edge-triggered — re-arms only after
      arousal dips back below `on_above`), instead of always launching.
      This is what makes flying read as occasional again instead of firing
      on nearly every emotionally-tagged turn (`_TAG_DELTAS` in mood.py
      routinely pushes arousal from baseline straight past 0.70 on a
      single tag — the probability roll is what keeps that from being a
      guaranteed launch, not a retuning of the deltas themselves).
    - **Boredom** ("any feeling" besides excited): arousal sitting
      continuously below `boredom_below` for at least `boredom_dwell_s`
      makes the chao eligible, then every mood event rolls
      `boredom_probability` while eligible — not edge-triggered, since
      being bored is a sustained state, not a one-shot crossing. Disabled
      by default (`boredom_probability=0.0`); config/emotes.yaml carries
      the real tuned values.

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
    - `director.tag` is checked for an unconditional "sad" tag, which lands
      immediately — bypassing both the arousal threshold and
      `min_dwell_s`. A chao that just turned sad shouldn't stay aloft for
      up to `min_dwell_s` more seconds because the tag landed right after
      a launch.
    - `director.tag` is also checked for `fly:left` / `fly:right` /
      `fly:center` (session 10 part 16) — a directed tag from the LLM,
      used when the streamer explicitly asks chao to move somewhere.
      Bypasses the arousal threshold and dwell/reposition floors the same
      way `sad` bypasses them for landing: those floors exist to damp
      *autonomous* pacing, not to veto an explicit directive. Launches
      from grounded if needed. The resulting target **latches** —
      `_maybe_reposition`'s ambient, mood-tick-driven repositioning is
      skipped entirely while `_directed` is set, so a chao told to go left
      doesn't randomly wander off again a few seconds later. The latch
      clears on landing (any route), so the next autonomous launch goes
      back to picking a random spot.

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
    _directed: bool = field(default=False, init=False)
    # High-arousal launch is edge-triggered: one roll per crossing, not one
    # roll per mood event while sustained above on_above -- re-arms only
    # once arousal dips back below on_above. Without this, launch_probability
    # would barely reduce frequency at all: arousal typically stays above
    # threshold for several ticks (half_life_s=20s decay is slow), so
    # repeated per-tick rolls would still launch on nearly every crossing,
    # just with a random delay instead of immediately.
    _high_arousal_rolled: bool = field(default=False, init=False)
    _bored_since: float | None = field(default=None, init=False)

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
        if not self.flying:
            if arousal > self.config.on_above:
                if not self._high_arousal_rolled:
                    self._high_arousal_rolled = True
                    if self.rng.random() < self.config.launch_probability:
                        self._transition(True, "arousal_high", now, event.turn_id)
                        return
            else:
                self._high_arousal_rolled = False

            if arousal < self.config.boredom_below:
                if self._bored_since is None:
                    self._bored_since = now
                if (
                    now - self._bored_since >= self.config.boredom_dwell_s
                    and self.rng.random() < self.config.boredom_probability
                ):
                    self._transition(True, "boredom", now, event.turn_id)
                    return
            else:
                self._bored_since = None
        elif arousal < self.config.off_below:
            if now - self._last_transition >= self.config.min_dwell_s:
                self._transition(False, "arousal_low", now, event.turn_id)
        elif event.payload.get("source") == "tag":
            self._maybe_reposition(now, event.turn_id)

    def _on_tag(self, event: Event) -> None:
        tag = event.payload.get("tag")
        if tag == "sad":
            if self.flying:
                self._transition(False, "sad", self.clock(), event.turn_id)
        elif tag in _FLY_DIRECTIONS:
            self._on_directed_fly(tag, event.turn_id)

    def _on_directed_fly(self, tag: str, turn_id: str | None) -> None:
        direction = tag.split(":", 1)[1]
        fraction = _FLY_TARGET_FRACTIONS[direction]
        now = self.clock()
        self.flying = True
        self.target_x = self.config.x_min + fraction * (self.config.x_max - self.config.x_min)
        self._directed = True
        self._last_transition = now
        self._last_reposition = now
        self._publish(turn_id, "directed")

    def _maybe_reposition(self, now: float, turn_id: str | None) -> None:
        if self._directed:
            return
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
        else:
            self._directed = False
            self._high_arousal_rolled = False
            self._bored_since = None
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
