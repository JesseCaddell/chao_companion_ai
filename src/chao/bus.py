"""Priority queue, turn arbiter, cancellation. See design doc §4.2, §13.

Two lanes, deliberately unequal:

- **Arbitrated turns.** `input.chat`, `input.voice`, `input.manual` compete
  for a single in-flight brain call. A strictly higher-priority arrival
  preempts the current turn by setting its cancel token; anything else is
  dropped with a `decision.dropped` event and a reason. This is the
  exclusive, cancellable, cooldown-gated path (CLAUDE.md invariant 5).
- **Everything else** — `input.ambient`, `director.*`, `output.*`,
  `vts.param`, `state.fly`, and the `decision.*`/`brain.*` events a turn
  itself produces — is fanned out to subscribers immediately, unblocked.
  Ambient events are deliberately *not* arbitrated: design doc §10.4 wants
  roughly 10 non-verbal reactions per spoken response, running continuously
  regardless of whatever the brain is doing. If a future ambient trigger
  needs to escalate to a full spoken turn, that's a phase 3+ decision, not
  one to speculatively build now.

The bus does not import brain/director/outputs. `run()` takes a
`turn_handler` callback so it stays testable with fakes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum

from chao.events import Event, Kind, new_id

logger = logging.getLogger(__name__)


class Priority(IntEnum):
    """Higher wins. Chat sub-priorities mirror design doc §4.1's 1-5
    trigger table, inverted (table's priority 1 is most urgent, so it
    maps to the top of this scale).
    """

    CHAT_BACKGROUND = 1  # §4.1 priority 4-5: velocity spike / background
    CHAT_ENGAGEMENT = 2  # §4.1 priority 3: high-affinity viewer
    CHAT_DIRECT = 3  # §4.1 priority 1-2: mention or question
    VOICE = 4  # streamer always outranks chat
    MANUAL = 5  # dashboard/typed override, test harness


# input.chat carries its selection-policy score (1-5, per §4.1) in
# payload["priority"]; missing/unknown values fall back to the floor
# rather than raising, per director/tags.py's "tolerant parser" precedent.
_CHAT_PRIORITY_MAP: dict[int, Priority] = {
    1: Priority.CHAT_DIRECT,
    2: Priority.CHAT_DIRECT,
    3: Priority.CHAT_ENGAGEMENT,
    4: Priority.CHAT_BACKGROUND,
    5: Priority.CHAT_BACKGROUND,
}

# Only these kinds compete for the single in-flight turn.
TURN_TRIGGERING_KINDS = frozenset({Kind.INPUT_CHAT, Kind.INPUT_VOICE, Kind.INPUT_MANUAL})

TurnHandler = Callable[[Event, asyncio.Event, str], Awaitable[None]]


@dataclass
class _CurrentTurn:
    turn_id: str
    priority: Priority
    cancel: asyncio.Event
    task: asyncio.Task


class Bus:
    def __init__(self, *, speech_cooldown_s: float = 15.0) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._current: _CurrentTurn | None = None
        self._speech_cooldown_s = speech_cooldown_s
        self._last_speech_end: float | None = None
        self._killed = asyncio.Event()

    def subscribe(self) -> asyncio.Queue[Event]:
        """Dashboard, SQLite writer, JSONL log each call this once."""
        q: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def publish(self, event: Event) -> None:
        """Entry point for every producer (inputs/, director/, outputs/, vts.py)."""
        if event.kind in TURN_TRIGGERING_KINDS:
            self._queue.put_nowait(event)
        else:
            self._fan_out(event)

    def _fan_out(self, event: Event) -> None:
        for q in self._subscribers:
            q.put_nowait(event)

    def priority_of(self, event: Event) -> Priority:
        if event.kind == Kind.INPUT_VOICE:
            return Priority.VOICE
        if event.kind == Kind.INPUT_MANUAL:
            return Priority.MANUAL
        if event.kind == Kind.INPUT_CHAT:
            scored = event.payload.get("priority", 5)
            return _CHAT_PRIORITY_MAP.get(scored, Priority.CHAT_BACKGROUND)
        return Priority.CHAT_BACKGROUND

    def mark_speech_end(self, ts: float) -> None:
        self._last_speech_end = ts

    def kill(self) -> None:
        """Physical kill switch (design doc §15): cancel in-flight turn now,
        block new turns until `revive()`.
        """
        self._killed.set()
        if self._current is not None:
            self._current.cancel.set()

    def revive(self) -> None:
        self._killed.clear()

    async def run(self, turn_handler: TurnHandler) -> None:
        while True:
            event = await self._queue.get()
            await self._arbitrate(event, turn_handler)

    async def _arbitrate(self, event: Event, turn_handler: TurnHandler) -> None:
        priority = self.priority_of(event)

        if self._killed.is_set():
            self.publish(
                Event(
                    kind=Kind.DECISION_DROPPED, payload={"reason": "killed"}, turn_id=event.turn_id
                )
            )
            return

        if self._current is not None:
            if priority > self._current.priority:
                self._current.cancel.set()
                await self._await_current_teardown()
            else:
                self.publish(
                    Event(
                        kind=Kind.DECISION_DROPPED,
                        payload={"reason": "busy", "priority": int(priority)},
                        turn_id=event.turn_id,
                    )
                )
                return

        if self._last_speech_end is not None and priority < Priority.VOICE:
            remaining = self._speech_cooldown_s - (event.ts - self._last_speech_end)
            if remaining > 0:
                self.publish(
                    Event(
                        kind=Kind.DECISION_DROPPED,
                        payload={"reason": "cooldown", "cooldown_remaining": remaining},
                        turn_id=event.turn_id,
                    )
                )
                return

        turn_id = event.turn_id or new_id()
        cancel = asyncio.Event()
        self.publish(
            Event(kind=Kind.DECISION_SELECTED, payload={"priority": int(priority)}, turn_id=turn_id)
        )
        task = asyncio.create_task(self._run_turn(event, cancel, turn_id, turn_handler))
        self._current = _CurrentTurn(turn_id=turn_id, priority=priority, cancel=cancel, task=task)

    async def _run_turn(
        self, event: Event, cancel: asyncio.Event, turn_id: str, turn_handler: TurnHandler
    ) -> None:
        try:
            await turn_handler(event, cancel, turn_id)
        except Exception:
            logger.exception("turn %s raised", turn_id)
            self.publish(
                Event(
                    kind=Kind.ERROR,
                    payload={"component": "bus", "message": "turn crashed"},
                    turn_id=turn_id,
                )
            )
        finally:
            if self._current is not None and self._current.turn_id == turn_id:
                self._current = None

    async def _await_current_teardown(self) -> None:
        if self._current is None:
            return
        try:
            await asyncio.wait_for(self._current.task, timeout=2.0)
        except TimeoutError:
            logger.warning("turn %s did not honor cancellation within 2s", self._current.turn_id)
        except asyncio.CancelledError:
            pass
