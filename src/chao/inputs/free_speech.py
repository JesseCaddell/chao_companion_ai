"""Free speech (session 11): lets chao initiate a spoken turn without being
talked to. See design doc §4.1 (a new trigger category, not one of the
existing tiers) and bus.py's module docstring for the arbitration side.

`FreeSpeech` is a pure interval timer, deliberately the simplest mechanism
that satisfies the user's own framing -- "frequent, same interval
regardless of backend, adjustable live" -- not tied to mood/boredom the
way `Fly`'s launch paths are. `enabled`/`interval_s` are plain mutable
fields, meant to be the same object the dashboard's command channel
mutates and `__main__.py`'s polling task reads; no event/callback wiring
needed for the setting itself, only for the resulting `input.ambient`
publish. Same clock-injectable dataclass shape as `Fly`/`IdleDrift` so the
interval logic is unit-testable without real sleeps.

Disabled by default and start-of-session only -- `enabled` is never
persisted to `config/emotes.yaml` or `config/chao.yaml`, by explicit user
decision: an autonomous-speech feature that survives a restart and starts
talking unprompted on its own would be a surprise, not a convenience.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

# Floor enforced by the dashboard command handler (dashboard/server.py),
# not here -- this class is a mechanism, the floor is policy that belongs
# at the boundary accepting untrusted-ish client input. Documented here
# too so a future reader of just this file isn't surprised by it existing
# elsewhere.
MIN_INTERVAL_S = 15.0
DEFAULT_INTERVAL_S = 90.0

# The "current event" text for a free-speech turn -- there's no real
# message to respond to, so the prompt says so explicitly rather than
# leaving the model to guess why a turn started with nothing to react to.
# Deliberately open-ended per the user's own framing ("both, depending on
# context"): react to something notable if there's something notable,
# otherwise just say what's on its mind.
PROMPT_TEXT = (
    "Nothing prompted this -- you're speaking on your own initiative, "
    "unprompted. React to anything notable in the recent conversation if "
    "there is something worth reacting to, or just share what's on your "
    "mind."
)


@dataclass
class FreeSpeech:
    enabled: bool = False
    interval_s: float = DEFAULT_INTERVAL_S
    clock: Callable[[], float] = time.monotonic

    _last_trigger: float = field(init=False)

    def __post_init__(self) -> None:
        self._last_trigger = self.clock()

    def tick(self) -> bool:
        """Call periodically (e.g. every 1s). Returns True exactly when a
        spontaneous turn should fire.

        While disabled, the trigger clock is kept pinned to "now" rather
        than left to accumulate -- otherwise re-enabling after a long
        disabled stretch would fire immediately, since the elapsed time
        while off would already exceed interval_s. Enabling should always
        mean "wait a fresh interval_s from now," not "cash in banked time."
        """
        now = self.clock()
        if not self.enabled:
            self._last_trigger = now
            return False
        if now - self._last_trigger >= self.interval_s:
            self._last_trigger = now
            return True
        return False
