import asyncio
from pathlib import Path

import numpy as np

from chao.events import Event, Kind
from chao.outputs.audio import AudioPlayer
from chao.outputs.speech import Speaker, load_tts_config
from chao.outputs.tts import PiperBackend


class FakePiperChunk:
    def __init__(self, samples: np.ndarray, sample_rate: int) -> None:
        self.audio_float_array = samples
        self.sample_rate = sample_rate


class FakeVoice:
    def __init__(self, chunks: list[FakePiperChunk]) -> None:
        self.chunks = chunks
        self.calls: list[str] = []

    def synthesize(self, text: str):
        self.calls.append(text)
        return self.chunks


class FakeSink:
    def __init__(self) -> None:
        self.play_calls: list[tuple[np.ndarray, int]] = []

    def play(self, samples: np.ndarray, sample_rate: int) -> None:
        self.play_calls.append((samples, sample_rate))

    def stop(self) -> None:
        pass

    def is_playing(self) -> bool:
        return False  # "finishes" instantly


def make_speaker(chunks, events: list[Event]) -> tuple[Speaker, FakeVoice]:
    voice = FakeVoice(chunks)
    backend = PiperBackend(Path("unused.onnx"), voice=voice)
    player = AudioPlayer(sink=FakeSink())
    speaker = Speaker(backend=backend, player=player, publish=events.append)
    return speaker, voice


def _chunk(n_samples: int, sample_rate: int = 22050) -> FakePiperChunk:
    return FakePiperChunk(np.zeros(n_samples, dtype=np.float32), sample_rate)


async def test_speak_publishes_start_and_end_around_playback():
    events: list[Event] = []
    speaker, _voice = make_speaker([_chunk(22050)], events)

    await speaker.speak("hello", turn_id="t1", cancel=asyncio.Event())

    kinds = [e.kind for e in events]
    assert kinds == [Kind.OUTPUT_SPEECH_START, Kind.OUTPUT_SPEECH_END]
    assert all(e.turn_id == "t1" for e in events)
    assert events[0].payload["duration_ms"] == 1000.0


async def test_speak_publishes_the_original_spelling_not_the_pronunciation_fix():
    """The dashboard/session log must keep the real spelling -- if "chow"
    leaked into stored text, retrieved context would teach the LLM to write
    it that way (docs/tts_pronunciation_overrides.md's invariants).
    """
    events: list[Event] = []
    speaker, voice = make_speaker([_chunk(100)], events)

    await speaker.speak("chao is happy", turn_id="t1", cancel=asyncio.Event())

    assert events[0].payload["sentence"] == "chao is happy"
    assert voice.calls == ["chow is happy"]  # Piper still gets the fixed spelling


async def test_speak_skips_blank_sentences():
    events: list[Event] = []
    speaker, voice = make_speaker([_chunk(100)], events)

    await speaker.speak("   ", turn_id="t1", cancel=asyncio.Event())

    assert events == []
    assert voice.calls == []


async def test_speak_no_ops_when_already_cancelled():
    events: list[Event] = []
    speaker, voice = make_speaker([_chunk(100)], events)
    cancel = asyncio.Event()
    cancel.set()

    await speaker.speak("hello", turn_id="t1", cancel=cancel)

    assert events == []
    assert voice.calls == []


async def test_speak_concatenates_multiple_piper_chunks_for_duration():
    events: list[Event] = []
    speaker, _voice = make_speaker([_chunk(22050), _chunk(11025)], events)

    await speaker.speak("hello there", turn_id="t1", cancel=asyncio.Event())

    assert events[0].payload["duration_ms"] == 1500.0


async def test_speak_skips_end_event_when_playback_is_cut_short():
    class NeverFinishesSink(FakeSink):
        def is_playing(self) -> bool:
            return True  # only stops via cancel, which we set immediately

    events: list[Event] = []
    voice = FakeVoice([_chunk(22050)])
    backend = PiperBackend(Path("unused.onnx"), voice=voice)
    player = AudioPlayer(sink=NeverFinishesSink())
    speaker = Speaker(backend=backend, player=player, publish=events.append)
    cancel = asyncio.Event()

    async def cancel_soon():
        await asyncio.sleep(0.01)
        cancel.set()

    task = asyncio.create_task(cancel_soon())
    await speaker.speak("hello", turn_id="t1", cancel=cancel)
    await task

    kinds = [e.kind for e in events]
    assert kinds == [Kind.OUTPUT_SPEECH_START]  # no speech_end for the aborted clip


def test_load_tts_config_reads_the_tts_block(tmp_path):
    config_path = tmp_path / "chao.yaml"
    config_path.write_text(
        "tts:\n  voice_path: data/voices/custom.onnx\n  output_device: CABLE Input\n"
    )

    config = load_tts_config(config_path)

    assert config.voice_path == "data/voices/custom.onnx"
    assert config.output_device == "CABLE Input"


def test_load_tts_config_defaults_when_file_missing(tmp_path):
    config = load_tts_config(tmp_path / "does_not_exist.yaml")

    assert config.voice_path == ""
    assert config.output_device is None
