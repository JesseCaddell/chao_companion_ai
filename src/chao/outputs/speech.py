"""Ties Piper synthesis (`tts.py`) and virtual-cable playback (`audio.py`)
together into "speak one cleaned sentence," publishing
`output.speech_start`/`output.speech_end` (design doc §13) around it.

Deliberately not a bus subscriber. Nothing on the bus carries cleaned,
tag-stripped sentence text: `brain.token` is the raw stream (director.py
strips tags per sentence, but that cleaned text never gets published --
turn.py's own docstring calls the raw token "the faithful record"), and
`brain.complete.full_text` is also raw and arrives too late for §12's
sentence-streaming budget anyway. Adding a new event kind to carry it would
be an Event schema change -- CLAUDE.md's explicit Opus-escalation trigger --
for something `TurnOrchestrator` can just hand `Speaker` directly as each
sentence completes. See `brain/turn.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

from chao.events import Event, Kind
from chao.outputs.audio import AudioPlayer
from chao.outputs.tts import PiperBackend


@dataclass(frozen=True, slots=True)
class TTSConfig:
    voice_path: str = ""
    output_device: str | None = None


def load_tts_config(path: Path) -> TTSConfig:
    """Reads the `tts:` block out of `config/chao.yaml`. Voice path and
    output device are config, not constants (CLAUDE.md: "never
    hardcoded") -- specifically, this is what makes swapping in a custom
    voice a config-only change, per session 8's plan, instead of a code
    change.
    """
    data = yaml.safe_load(path.read_text()) or {} if path.exists() else {}
    raw = data.get("tts") or {}
    return TTSConfig(
        voice_path=str(raw.get("voice_path", "")),
        output_device=raw.get("output_device"),
    )


class Speaker:
    def __init__(
        self,
        *,
        backend: PiperBackend,
        player: AudioPlayer,
        publish: Callable[[Event], None],
    ) -> None:
        self._backend = backend
        self._player = player
        self._publish = publish

    async def speak(self, sentence: str, *, turn_id: str, cancel: asyncio.Event) -> None:
        """Synthesizes and plays one sentence. No-ops on blank text (a
        tag-only sentence cleans to "") and on an already-cancelled turn,
        so a turn aborted before this sentence was reached doesn't
        needlessly hit Piper or PortAudio.

        `sentence` -- the real spelling, not the TTS-only pronunciation
        fix's rewritten text -- is what goes in the published event.
        `PiperBackend.synthesize` applies that fix internally and doesn't
        hand the rewritten text back out, so this can't leak "chow" into
        the dashboard/session log by accident even though the two
        concerns live in neighboring files.
        """
        text = sentence.strip()
        if not text or cancel.is_set():
            return

        chunks = [chunk async for chunk in self._backend.synthesize(text)]
        if not chunks or cancel.is_set():
            return

        samples = (
            chunks[0].samples if len(chunks) == 1 else np.concatenate([c.samples for c in chunks])
        )
        sample_rate = chunks[0].sample_rate
        duration_ms = len(samples) / sample_rate * 1000

        self._publish(
            Event(
                kind=Kind.OUTPUT_SPEECH_START,
                turn_id=turn_id,
                payload={"sentence": text, "duration_ms": duration_ms},
            )
        )
        completed = await self._player.play(samples, sample_rate, cancel=cancel)
        if completed:
            self._publish(
                Event(
                    kind=Kind.OUTPUT_SPEECH_END,
                    turn_id=turn_id,
                    payload={"sentence": text, "duration_ms": duration_ms},
                )
            )
