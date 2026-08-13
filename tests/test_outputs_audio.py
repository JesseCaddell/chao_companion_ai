import asyncio

import numpy as np

from chao.outputs.audio import AudioPlayer, resolve_output_device


class FakeSink:
    """`is_playing()` reports True for `poll_count` polls, then False --
    simulates a clip of known "length" without a real audio device.
    """

    def __init__(self, poll_count: int) -> None:
        self.poll_count = poll_count
        self.play_calls: list[tuple[np.ndarray, int]] = []
        self.stop_calls = 0
        self._polls_done = 0

    def play(self, samples: np.ndarray, sample_rate: int) -> None:
        self.play_calls.append((samples, sample_rate))
        self._polls_done = 0

    def stop(self) -> None:
        self.stop_calls += 1
        self._polls_done = self.poll_count  # further polls report "not playing"

    def is_playing(self) -> bool:
        if self._polls_done >= self.poll_count:
            return False
        self._polls_done += 1
        return True


async def test_play_runs_to_completion_and_returns_true():
    sink = FakeSink(poll_count=2)
    player = AudioPlayer(sink=sink)
    samples = np.array([0.1, 0.2], dtype=np.float32)

    completed = await player.play(samples, 22050, cancel=asyncio.Event())

    assert completed is True
    assert sink.play_calls == [(samples, 22050)]
    assert sink.stop_calls == 1  # the unconditional finally cleanup


async def test_cancel_stops_playback_early_and_returns_false():
    sink = FakeSink(poll_count=1_000_000)  # would "play" forever without cancel
    player = AudioPlayer(sink=sink)
    cancel = asyncio.Event()

    async def cancel_soon():
        await asyncio.sleep(0.05)
        cancel.set()

    task = asyncio.create_task(cancel_soon())
    completed = await player.play(np.array([0.0], dtype=np.float32), 22050, cancel=cancel)
    await task

    assert completed is False
    assert sink.stop_calls == 1


async def test_play_stops_the_sink_even_if_already_cancelled_before_starting():
    sink = FakeSink(poll_count=1_000_000)
    player = AudioPlayer(sink=sink)
    cancel = asyncio.Event()
    cancel.set()

    completed = await player.play(np.array([0.0], dtype=np.float32), 22050, cancel=cancel)

    assert completed is False
    assert sink.stop_calls == 1


def test_resolve_output_device_returns_none_for_no_substring():
    assert resolve_output_device(None) is None
    assert resolve_output_device("") is None


def test_resolve_output_device_warns_when_requested_device_not_found(capsys):
    # No real audio hardware is guaranteed in CI, but a nonsense substring
    # is guaranteed not to match whatever devices do exist.
    result = resolve_output_device("definitely-not-a-real-device-xyz")

    assert result is None
    assert "definitely-not-a-real-device-xyz" in capsys.readouterr().out
