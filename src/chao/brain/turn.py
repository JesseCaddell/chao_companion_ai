"""Turn orchestrator: wires a backend + Director together to satisfy
bus.py's `TurnHandler`. See design doc §4.3, §13.

`TurnOrchestrator` instances are directly usable as `bus.py`'s
`turn_handler` (they implement `__call__` with the matching signature).
Per turn: assemble a Prompt, publish `brain.request`, stream the backend's
response — publishing `brain.token` per chunk and feeding it to
`Director.process_chunk` — then publish `brain.complete` and remember the
exchange for the next turn's recent context.

Not yet real, deliberately: `personality` and `memory` are static strings
(no mood.py, no memory/store.py — both later phases). Recent conversation
history is a simple in-process bound list, not persisted — cross-session
memory is phase 6's job, this is just same-session continuity.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from chao.brain.backend import LLMBackend
from chao.brain.prompt import CurrentEvent, EventSource, Prompt, Turn, assemble_prompt
from chao.director.director import Director
from chao.events import Event, Kind
from chao.outputs.speech import Speaker

_SOURCE_BY_KIND: dict[str, EventSource] = {
    Kind.INPUT_CHAT: "chat",
    Kind.INPUT_VOICE: "voice",
    Kind.INPUT_MANUAL: "manual",
    Kind.INPUT_AMBIENT: "ambient",
}


@dataclass
class TurnOrchestrator:
    backend: LLMBackend
    director: Director
    publish: Callable[[Event], None]
    identity: str = ""
    personality: str = ""
    memory: str = ""
    # None means TTS isn't configured -- same graceful-degrade shape as a
    # VTS connection that never came up: the chat loop still works, it
    # just doesn't speak.
    speaker: Speaker | None = None
    # Turns, not exchanges (2 Turns/exchange, always appended as a pair) —
    # keep this even, or a popleft() can strip the pairing and leave
    # history starting with an assistant Turn, which the Anthropic API
    # rejects (first message must be "user").
    history_limit: int = 40
    # A pinned local backend (CHAO_LLM_BACKEND=local) has no
    # CircuitBreakerBackend watching it, so a slow-to-first-token turn (e.g.
    # Ollama under real CPU contention with VTS/OBS — see brain/local.py's
    # module docstring) and a genuinely dead backend look identical from
    # outside: total silence. This doesn't fall back or cancel anything —
    # it just stops the silence from being ambiguous. If no chunk arrives
    # within this many seconds, publish once and keep waiting.
    stall_warning_s: float = 8.0

    _history: deque[Turn] = field(default_factory=deque, init=False)

    async def __call__(self, event: Event, cancel: asyncio.Event, turn_id: str) -> None:
        current_event = _current_event_from(event)
        prompt = assemble_prompt(
            identity=self.identity,
            personality=self.personality,
            memory=self.memory,
            recent_context=list(self._history),
            current_event=current_event,
        )

        self.publish(
            Event(
                kind=Kind.BRAIN_REQUEST,
                turn_id=turn_id,
                payload={
                    "prompt_sections": _non_empty_sections(prompt),
                    "token_counts": prompt.token_counts,
                    # Whatever backend was configured — once that's a
                    # CircuitBreakerBackend this always reads
                    # "CircuitBreakerBackend", never revealing whether a
                    # given turn actually fell back to local. This fires
                    # before streaming starts, so it can't know yet either
                    # way; a real answer would need the breaker to surface
                    # it on brain.complete instead.
                    "backend": type(self.backend).__name__,
                },
            )
        )

        self.director.begin_turn(turn_id)
        start = time.monotonic()
        raw_chunks: list[str] = []
        cleaned_chunks: list[str] = []

        # Sentences are handed to the speaker as Director completes them,
        # drained by a separate task so synthesis/playback never stalls
        # token streaming (see Speaker's docstring for why this is a direct
        # handoff, not a bus event). __call__ still awaits the drain task
        # before returning -- bus.py's arbiter treats a returned handler as
        # "turn over," so returning while audio is still playing would let
        # a new turn start speaking over it and would start the 15s speech
        # cooldown from the wrong moment.
        speech_queue: asyncio.Queue[str | None] | None = None
        speech_task: asyncio.Task[None] | None = None
        if self.speaker is not None:
            speech_queue = asyncio.Queue()
            speech_task = asyncio.create_task(self._drain_speech(speech_queue, turn_id, cancel))

        stall_task = asyncio.create_task(self._warn_if_stalled(turn_id))
        try:
            async for chunk in self.backend.stream(prompt.system, prompt.messages, cancel):
                if not stall_task.done():
                    stall_task.cancel()
                raw_chunks.append(chunk)
                self.publish(Event(kind=Kind.BRAIN_TOKEN, turn_id=turn_id, payload={"text": chunk}))
                sentences = self.director.process_chunk(chunk)
                cleaned_chunks.extend(sentences)
                if speech_queue is not None:
                    for sentence in sentences:
                        speech_queue.put_nowait(sentence)

            if cancel.is_set():
                # Aborted mid-stream. Whatever already streamed was
                # legitimate (its director.tag/emote events already
                # published above) but the unflushed remainder is
                # abandoned, not completed — no brain.complete, no history
                # entry. The next turn's begin_turn() resets Director's
                # buffer, so nothing leaks.
                return

            final = self.director.end_turn()
            if final:
                cleaned_chunks.append(final)
                if speech_queue is not None:
                    speech_queue.put_nowait(final)

            full_text = "".join(raw_chunks)
            latency_ms = (time.monotonic() - start) * 1000

            self.publish(
                Event(
                    kind=Kind.BRAIN_COMPLETE,
                    turn_id=turn_id,
                    payload={
                        "full_text": full_text,
                        "latency_ms": latency_ms,
                        "usage": None,  # not exposed by LLMBackend yet
                    },
                )
            )

            # A reply that's empty after tag-stripping (backend yielded
            # nothing, or the model emitted only a tag) must not become a
            # Message with empty content — the Anthropic API rejects that
            # with a 400, which CircuitBreakerBackend would read as a
            # pre-first-token failure and fall back for every turn from
            # here on, using the same poisoned history. brain.complete
            # above still reports the real full_text (possibly ""); only
            # the history entry is skipped.
            reply = " ".join(c for c in cleaned_chunks if c).strip()
            if reply:
                # prompt.messages[-1] is the current event already
                # wrapped/labelled by assemble_prompt — reused here so
                # untrusted-chat labelling survives into history instead of
                # being lost on the next turn.
                self._history.append(Turn(role="user", text=prompt.messages[-1].content))
                self._history.append(Turn(role="assistant", text=reply))
                while len(self._history) > self.history_limit:
                    self._history.popleft()
        finally:
            # Runs on every exit path, including the cancel-branch return
            # above: the sentinel guarantees _drain_speech terminates
            # instead of blocking on queue.get() forever, and awaiting it
            # here is what makes turn lifetime honest (see comment above).
            # Already-queued sentences past a cancellation point still get
            # popped, but Speaker.speak() checks the same cancel token
            # first thing and no-ops instead of synthesizing/playing them.
            if speech_queue is not None and speech_task is not None:
                speech_queue.put_nowait(None)
                await speech_task
            stall_task.cancel()

    async def _warn_if_stalled(self, turn_id: str) -> None:
        await asyncio.sleep(self.stall_warning_s)
        self.publish(
            Event(
                kind=Kind.BRAIN_STALLED,
                turn_id=turn_id,
                payload={"waited_s": self.stall_warning_s},
            )
        )

    async def _drain_speech(
        self, queue: asyncio.Queue[str | None], turn_id: str, cancel: asyncio.Event
    ) -> None:
        """A `speaker.speak()` failure (bad voice file, PortAudio device
        error) must not surface as *this turn* raising: `__call__` awaits
        this task from inside its own `finally`, after `brain.complete` has
        already published successfully, so letting the exception propagate
        there would report a fully-successful text turn as a crash. Report
        it as its own error and stop trying for the rest of the turn
        instead -- a broken voice model won't fix itself mid-sentence, and
        the next turn gets a fresh attempt.
        """
        assert self.speaker is not None
        while True:
            sentence = await queue.get()
            if sentence is None:
                return
            try:
                await self.speaker.speak(sentence, turn_id=turn_id, cancel=cancel)
            except Exception as e:  # noqa: BLE001 - any speech failure reports and stops, never crashes the turn
                self.publish(
                    Event(
                        kind=Kind.ERROR,
                        turn_id=turn_id,
                        payload={"message": f"speech failed: {e}"},
                    )
                )
                return


def _current_event_from(event: Event) -> CurrentEvent:
    # Fails closed, not open: an input Kind added later and forgotten
    # here must default to untrusted, not trusted -- CLAUDE.md invariant
    # 6 only holds if a gap in this mapping can't silently promote
    # something to the system prompt's trust level.
    source = _SOURCE_BY_KIND.get(event.kind, "chat")
    speaker = None
    if source == "chat":
        speaker = event.payload.get("display_name") or event.payload.get("login")
    return CurrentEvent(text=event.payload.get("text", ""), source=source, speaker=speaker)


def _non_empty_sections(prompt: Prompt) -> list[str]:
    sections = []
    if prompt.stable:
        sections.append("stable")
    if prompt.dynamic:
        sections.append("dynamic")
    if prompt.messages:
        sections.append("messages")
    return sections
