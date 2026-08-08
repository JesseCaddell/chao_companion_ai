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

Not built yet, deliberately: the micro/meso/macro idle-drift timescales
(§10.1-10.3, §10.6) and attention model (§10.3). All three need continuous
parameter injection, which per CLAUDE.md's phase-0 finding requires new
custom VTS parameters bound by hand in the VTS UI before code can drive
them — a real dependency on work only the user can do (see SESSION_STATE.md's
next actions), not something this module is blocked on writing but
couldn't be verified working without. Building the noise generators now,
with no bound parameter to watch them drive, would repeat the "UI over
nothing" mistake the dashboard's other panels deliberately avoided.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field

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
