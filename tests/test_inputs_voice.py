from pathlib import Path

import numpy as np
import pytest

from chao.events import Event, Kind
from chao.inputs.voice import (
    VoiceConfig,
    VoiceEndpointer,
    VoiceInput,
    WhisperBackend,
    load_voice_config,
    resolve_input_device,
)


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ScriptedVAD:
    """Returns probabilities from a fixed script, one per `probability()`
    call -- lets endpointer tests drive exact speech/silence sequences
    without a real ONNX session.
    """

    def __init__(self, probs: list[float]):
        self._probs = list(probs)
        self.reset_count = 0

    def probability(self, chunk: np.ndarray) -> float:
        return self._probs.pop(0)

    def reset(self) -> None:
        self.reset_count += 1


def make_config(**overrides) -> VoiceConfig:
    defaults = {
        "sample_rate": 16000,
        "frame_samples": 512,
        "vad_threshold": 0.5,
        "silence_ms": 500.0,
        "min_speech_ms": 250.0,
        "max_utterance_s": 30.0,
    }
    defaults.update(overrides)
    return VoiceConfig(**defaults)


def chunk() -> np.ndarray:
    return np.zeros(512, dtype=np.float32)


# --- VoiceEndpointer ---------------------------------------------------


def test_idle_with_low_probability_never_starts_buffering():
    clock = FakeClock()
    vad = ScriptedVAD([0.1, 0.1, 0.1])
    endpointer = VoiceEndpointer(config=make_config(), vad=vad, clock=clock)

    for _ in range(3):
        clock.advance(0.032)
        assert endpointer.feed(chunk()) is None


def test_speech_then_short_silence_does_not_endpoint_yet():
    clock = FakeClock()
    vad = ScriptedVAD([0.9, 0.9, 0.1])
    endpointer = VoiceEndpointer(config=make_config(silence_ms=500.0), vad=vad, clock=clock)

    endpointer.feed(chunk())
    clock.advance(0.032)
    endpointer.feed(chunk())
    clock.advance(0.032)  # only 32ms of silence -- well under 500ms
    assert endpointer.feed(chunk()) is None


def test_speech_then_enough_silence_endpoints_and_returns_utterance():
    clock = FakeClock()
    vad = ScriptedVAD([0.9, 0.9, 0.1])
    endpointer = VoiceEndpointer(config=make_config(silence_ms=500.0), vad=vad, clock=clock)

    endpointer.feed(chunk())  # speech starts
    clock.advance(0.5)
    endpointer.feed(chunk())  # still speech, resets silence clock
    clock.advance(0.6)  # past silence_ms
    utterance = endpointer.feed(chunk())

    assert utterance is not None
    assert len(utterance) == 512 * 3


def test_endpointing_resets_vad_state_for_the_next_utterance():
    clock = FakeClock()
    vad = ScriptedVAD([0.9, 0.9, 0.1])
    endpointer = VoiceEndpointer(config=make_config(silence_ms=500.0), vad=vad, clock=clock)

    endpointer.feed(chunk())
    clock.advance(0.5)
    endpointer.feed(chunk())
    clock.advance(0.6)
    endpointer.feed(chunk())

    assert vad.reset_count == 1


def test_endpointer_returns_to_idle_after_endpointing():
    clock = FakeClock()
    vad = ScriptedVAD([0.9, 0.1, 0.9])
    endpointer = VoiceEndpointer(config=make_config(silence_ms=500.0), vad=vad, clock=clock)

    endpointer.feed(chunk())  # speech starts
    clock.advance(1.0)  # past silence_ms -- endpoints
    endpointer.feed(chunk())
    clock.advance(0.032)
    # A fresh speech probability after endpointing should start a NEW
    # utterance, not be treated as more of the old one.
    result = endpointer.feed(chunk())
    assert result is None  # just started buffering again, no silence yet


def test_utterance_shorter_than_min_speech_ms_is_discarded():
    clock = FakeClock()
    vad = ScriptedVAD([0.9, 0.1])
    endpointer = VoiceEndpointer(
        config=make_config(silence_ms=100.0, min_speech_ms=1000.0), vad=vad, clock=clock
    )

    endpointer.feed(chunk())  # speech starts
    clock.advance(0.15)  # short utterance, past silence_ms but under min_speech_ms
    result = endpointer.feed(chunk())

    assert result is None


def test_max_utterance_s_forces_an_endpoint_even_with_continuous_speech():
    clock = FakeClock()
    vad = ScriptedVAD([0.9, 0.9, 0.9])
    endpointer = VoiceEndpointer(
        config=make_config(silence_ms=500.0, max_utterance_s=1.0), vad=vad, clock=clock
    )

    endpointer.feed(chunk())  # speech starts
    clock.advance(0.5)
    endpointer.feed(chunk())  # still speech, no silence
    clock.advance(0.6)  # total utterance now past max_utterance_s, still "speech"
    utterance = endpointer.feed(chunk())

    assert utterance is not None


# --- WhisperBackend ------------------------------------------------------


class FakeSegment:
    def __init__(self, text: str, avg_logprob: float):
        self.text = text
        self.avg_logprob = avg_logprob


class FakeWhisperModel:
    def __init__(self, segments: list[FakeSegment]):
        self._segments = segments

    def transcribe(self, audio, language=None):
        return iter(self._segments), object()


def test_transcribe_joins_segment_text():
    backend = WhisperBackend("base")
    backend._model = FakeWhisperModel([FakeSegment(" hello ", -0.1), FakeSegment(" world ", -0.2)])

    text, _confidence = backend.transcribe(np.zeros(16000, dtype=np.float32))

    assert text == "hello world"


def test_transcribe_confidence_is_exp_of_avg_logprob():
    import math

    backend = WhisperBackend("base")
    backend._model = FakeWhisperModel([FakeSegment("hi", -0.5)])

    _text, confidence = backend.transcribe(np.zeros(16000, dtype=np.float32))

    assert confidence == pytest.approx(math.exp(-0.5))


def test_transcribe_returns_empty_for_no_segments():
    backend = WhisperBackend("base")
    backend._model = FakeWhisperModel([])

    text, confidence = backend.transcribe(np.zeros(16000, dtype=np.float32))

    assert text == ""
    assert confidence == 0.0


# --- load_voice_config -----------------------------------------------------


def test_load_voice_config_reads_the_voice_block(tmp_path: Path):
    config_path = tmp_path / "chao.yaml"
    config_path.write_text(
        "voice:\n"
        "  enabled: true\n"
        "  input_device: 'USB Mic'\n"
        "  silence_ms: 400\n"
        "  vad_threshold: 0.6\n"
    )

    config = load_voice_config(config_path)

    assert config.enabled is True
    assert config.input_device == "USB Mic"
    assert config.silence_ms == 400.0
    assert config.vad_threshold == 0.6


def test_load_voice_config_defaults_when_file_missing(tmp_path: Path):
    assert load_voice_config(tmp_path / "does_not_exist.yaml") == VoiceConfig()


def test_load_voice_config_defaults_when_block_absent(tmp_path: Path):
    config_path = tmp_path / "chao.yaml"
    config_path.write_text("tts:\n  voice_path: foo.onnx\n")

    assert load_voice_config(config_path) == VoiceConfig()


def test_load_voice_config_defaults_to_disabled():
    assert VoiceConfig().enabled is False


# --- resolve_input_device -----------------------------------------------------


def test_resolve_input_device_returns_none_when_unset():
    assert resolve_input_device(None) is None


def test_resolve_input_device_returns_none_when_empty_string():
    assert resolve_input_device("") is None


# --- VoiceInput: echo guard + wiring --------------------------------------


class ScriptedWhisper:
    def __init__(self, text: str = "hello", confidence: float = 0.9):
        self.text = text
        self.confidence = confidence
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        return self.text, self.confidence


async def test_process_chunk_publishes_input_voice_on_endpoint():
    events: list[Event] = []
    vad = ScriptedVAD([0.9, 0.1])
    config = make_config(silence_ms=0.0, min_speech_ms=0.0)
    endpointer = VoiceEndpointer(config=config, vad=vad, clock=FakeClock().__call__)
    whisper = ScriptedWhisper(text="hi there")
    voice_input = VoiceInput(
        config=config, publish=events.append, vad=vad, whisper=whisper, endpointer=endpointer
    )

    await voice_input._process_chunk(chunk())
    await voice_input._process_chunk(chunk())

    chat_events = [e for e in events if e.kind == Kind.INPUT_VOICE]
    assert len(chat_events) == 1
    assert chat_events[0].payload["text"] == "hi there"
    assert chat_events[0].payload["confidence"] == 0.9
    # Two chunks got buffered before the endpoint (speech, then silence).
    assert chat_events[0].payload["duration_ms"] == pytest.approx(2 * 512 / 16000 * 1000)


async def test_process_chunk_does_not_publish_when_endpointer_returns_none():
    events: list[Event] = []
    vad = ScriptedVAD([0.1])
    config = make_config()
    endpointer = VoiceEndpointer(config=config, vad=vad, clock=FakeClock().__call__)
    whisper = ScriptedWhisper()
    voice_input = VoiceInput(
        config=config, publish=events.append, vad=vad, whisper=whisper, endpointer=endpointer
    )

    await voice_input._process_chunk(chunk())

    assert events == []
    assert whisper.calls == 0


async def test_echo_guard_discards_utterance_without_running_stt_while_speaking():
    events: list[Event] = []
    vad = ScriptedVAD([0.9, 0.1])
    config = make_config(silence_ms=0.0, min_speech_ms=0.0)
    endpointer = VoiceEndpointer(config=config, vad=vad, clock=FakeClock().__call__)
    whisper = ScriptedWhisper()
    voice_input = VoiceInput(
        config=config, publish=events.append, vad=vad, whisper=whisper, endpointer=endpointer
    )
    voice_input.on_speech_start()

    await voice_input._process_chunk(chunk())
    await voice_input._process_chunk(chunk())

    assert events == []
    assert whisper.calls == 0  # never even ran STT -- discarded before that


async def test_echo_guard_lifts_after_on_speech_end():
    events: list[Event] = []
    vad = ScriptedVAD([0.9, 0.1, 0.9, 0.1])
    config = make_config(silence_ms=0.0, min_speech_ms=0.0)
    endpointer = VoiceEndpointer(config=config, vad=vad, clock=FakeClock().__call__)
    whisper = ScriptedWhisper()
    voice_input = VoiceInput(
        config=config, publish=events.append, vad=vad, whisper=whisper, endpointer=endpointer
    )

    voice_input.on_speech_start()
    await voice_input._process_chunk(chunk())
    await voice_input._process_chunk(chunk())  # discarded (echo guard)

    voice_input.on_speech_end()
    await voice_input._process_chunk(chunk())
    await voice_input._process_chunk(chunk())  # published

    chat_events = [e for e in events if e.kind == Kind.INPUT_VOICE]
    assert len(chat_events) == 1


async def test_empty_transcription_does_not_publish():
    events: list[Event] = []
    vad = ScriptedVAD([0.9, 0.1])
    config = make_config(silence_ms=0.0, min_speech_ms=0.0)
    endpointer = VoiceEndpointer(config=config, vad=vad, clock=FakeClock().__call__)
    whisper = ScriptedWhisper(text="")
    voice_input = VoiceInput(
        config=config, publish=events.append, vad=vad, whisper=whisper, endpointer=endpointer
    )

    await voice_input._process_chunk(chunk())
    await voice_input._process_chunk(chunk())

    assert events == []
