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
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np


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
    def synthesize(self, text: str) -> Iterable[_PiperChunk]: ...


class PiperBackend:
    def __init__(self, model_path: Path, *, voice: _Voice | None = None) -> None:
        self._model_path = model_path
        self._voice = voice

    def _load_voice(self) -> _Voice:
        if self._voice is None:
            # Deferred import: real model load, module import-time cost a
            # test using a fake `voice` shouldn't have to pay.
            from piper import PiperVoice

            self._voice = PiperVoice.load(self._model_path)
        return self._voice

    async def synthesize(self, text: str) -> AsyncIterator[AudioChunk]:
        loop = asyncio.get_running_loop()
        chunks = await loop.run_in_executor(None, self._synthesize_sync, text)
        for chunk in chunks:
            yield AudioChunk(samples=chunk.audio_float_array, sample_rate=chunk.sample_rate)

    def _synthesize_sync(self, text: str) -> list[_PiperChunk]:
        return list(self._load_voice().synthesize(text))
