"""Piper TTS backend (CPU-only, zero-VRAM per CLAUDE.md invariant 1). See
design doc §8, §18.

`voice` is injectable so tests can fake it without loading a real Piper
model — `PiperVoice.load` is real disk I/O plus onnxruntime session setup,
too slow and too heavy to pay in unit tests.

Synthesis is blocking, CPU-bound work (onnxruntime inference), so
`synthesize` runs it in a thread via `run_in_executor` rather than awaiting
it directly — everything else on the bus (VTS, dashboard, aliveness) needs
to keep running while a sentence renders.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

# Piper doesn't decide pronunciation -- text is phonemized by espeak-ng
# before the model ever sees it, so mispronunciations have to be fixed in
# the text, not the model. TTS path only: callers must keep the original
# spelling for history/memory/the dashboard, or the fix leaks into retrieved
# context and becomes self-reinforcing. See docs/tts_pronunciation_overrides.md.
_PRONUNCIATION = {
    "chao": "chow",
    "chao's": "chow's",
}

# Letters only, with an apostrophe allowed *between* letters (contractions/
# possessives like "chao's") -- not at the edges, so a quoted 'chao' doesn't
# sweep the closing quote into the match and silently miss the dict lookup.
_WORD_PATTERN = re.compile(r"\b[A-Za-z]+(?:'[A-Za-z]+)?\b")


def _fix_pronunciation(text: str) -> str:
    """Rewrite words espeak-ng mispronounces. Call this only on text about to
    be spoken -- never on anything that gets stored or fed back to the LLM.
    """

    def replace(match: re.Match[str]) -> str:
        word = match.group(0)
        fixed = _PRONUNCIATION.get(word.lower())
        if fixed is None:
            return word
        return fixed.capitalize() if word[0].isupper() else fixed

    return _WORD_PATTERN.sub(replace, text)


@dataclass(frozen=True)
class AudioChunk:
    """One sentence's worth of synthesized audio. `samples` is mono float32
    PCM in [-1, 1] — the shape motion.py's §8.1 envelope extraction (RMS @
    60Hz) will read directly, no decoding step in between.
    """

    samples: np.ndarray
    sample_rate: int


class _PiperChunk(Protocol):
    audio_float_array: np.ndarray
    sample_rate: int


class _Voice(Protocol):
    def synthesize(self, text: str, syn_config: object = None) -> Iterable[_PiperChunk]: ...


class PiperBackend:
    def __init__(
        self, model_path: Path, *, voice: _Voice | None = None, length_scale: float | None = None
    ) -> None:
        self._model_path = model_path
        self._voice = voice
        # None -- not 1.0 -- so this falls through to the voice's own
        # trained default rather than silently overriding it. See
        # config/chao.yaml's tts.length_scale comment for why this
        # exists (session 10 part 13: trailing-consonant clipping).
        self._length_scale = length_scale

    def _load_voice(self) -> _Voice:
        if self._voice is None:
            # Deferred import: real model load, module import-time cost a
            # test using a fake `voice` shouldn't have to pay.
            from piper import PiperVoice

            self._voice = PiperVoice.load(self._model_path)
        return self._voice

    async def synthesize(self, text: str) -> AsyncIterator[AudioChunk]:
        loop = asyncio.get_running_loop()
        chunks = await loop.run_in_executor(None, self._synthesize_sync, _fix_pronunciation(text))
        for chunk in chunks:
            yield AudioChunk(samples=chunk.audio_float_array, sample_rate=chunk.sample_rate)

    def _synthesize_sync(self, text: str) -> list[_PiperChunk]:
        from piper.config import SynthesisConfig

        syn_config = SynthesisConfig(length_scale=self._length_scale)
        return list(self._load_voice().synthesize(text, syn_config=syn_config))
