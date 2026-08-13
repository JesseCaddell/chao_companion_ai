"""Cancellable playback out to VB-Audio Virtual Cable (design doc §8, §18).

`sd.play()`/`sd.wait()` would be the obvious pairing, but `sd.wait()` blocks
the calling thread with no way in for the cancel token -- running it via
`run_in_executor` would just make the *thread* uninterruptible instead
(CLAUDE.md invariant 5: no uninterruptible await chain). Instead, `play()`
starts playback and polls `cancel` on a short interval, calling `sd.stop()`
the moment it fires, so cancellation lands mid-sentence rather than at the
next sentence boundary.

`sink` is injectable so tests don't need real audio hardware or PortAudio --
same shape as `PiperBackend(voice=...)`.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

import numpy as np

_POLL_INTERVAL_S = 0.02


class _Sink(Protocol):
    def play(self, samples: np.ndarray, sample_rate: int) -> None: ...
    def stop(self) -> None: ...
    def is_playing(self) -> bool: ...


class SoundDeviceSink:
    """Thin wrapper over `sounddevice`'s module-level playback state.
    Deferred import, same reasoning as `PiperBackend._load_voice`: a test
    using a fake sink shouldn't have to pay PortAudio's import/init cost.
    """

    def __init__(self, device: int | str | None = None) -> None:
        self._device = device

    def play(self, samples: np.ndarray, sample_rate: int) -> None:
        import sounddevice as sd

        sd.play(samples, sample_rate, device=self._device)

    def stop(self) -> None:
        import sounddevice as sd

        sd.stop()

    def is_playing(self) -> bool:
        import sounddevice as sd

        stream = sd.get_stream()
        return stream is not None and stream.active


def resolve_output_device(name_substring: str | None) -> int | None:
    """Finds an output device whose name contains `name_substring`
    (case-insensitive) -- e.g. "CABLE Input" for VB-Audio Virtual Cable.
    Returns `None` (sounddevice's default-device sentinel) if no substring
    is given, falling back to the system default output the same way
    `_run_vts_subscriber` falls back to "no VTS" when it's unavailable.

    Unlike that precedent, a *requested* device that isn't found still
    falls back to `None` rather than raising -- but prints a warning first,
    because the silent version of this is a real live-stream failure mode:
    audio would come out the user's speakers (and, if monitored, straight
    back into the mic) instead of into OBS via the virtual cable, with
    nothing on screen or in the logs to explain why.
    """
    if not name_substring:
        return None
    import sounddevice as sd

    needle = name_substring.lower()
    for index, info in enumerate(sd.query_devices()):
        if info["max_output_channels"] > 0 and needle in info["name"].lower():
            return index
    print(f"  (no output device matching '{name_substring}', using system default)")
    return None


class AudioPlayer:
    """`sd.play`/`sd.stop`/`sd.get_stream()` all act on sounddevice's one
    module-level stream, so `stop()` in `play()`'s `finally` stops
    *whatever* is currently playing, not specifically this call's clip.
    Fine today (one `Speaker`, sentences played strictly sequentially by
    `brain/turn.py`'s drain loop) -- would need real isolation (per-instance
    `sd.OutputStream`) if a second concurrent caller (e.g. a future ambient
    notification sound) ever shows up.
    """

    def __init__(self, *, sink: _Sink | None = None, device: int | None = None) -> None:
        self._sink = sink
        self._device = device

    def _get_sink(self) -> _Sink:
        if self._sink is None:
            self._sink = SoundDeviceSink(self._device)
        return self._sink

    async def play(self, samples: np.ndarray, sample_rate: int, *, cancel: asyncio.Event) -> bool:
        """Plays `samples` to completion, or stops early if `cancel` fires
        mid-playback. Returns True if playback completed, False if cut short.
        """
        sink = self._get_sink()
        sink.play(samples, sample_rate)
        try:
            while sink.is_playing():
                if cancel.is_set():
                    return False
                await asyncio.sleep(_POLL_INTERVAL_S)
        finally:
            # Always, not just on the cancel path -- a hard task.cancel()
            # from bus.py's preemption escalation raises out of the
            # `await asyncio.sleep` above, and without this `finally` that
            # would leave PortAudio playing into the virtual cable with
            # nothing left owning the stream.
            sink.stop()
        return True
