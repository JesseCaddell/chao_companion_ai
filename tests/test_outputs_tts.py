from pathlib import Path

import numpy as np

from chao.outputs.tts import AudioChunk, PiperBackend, _fix_pronunciation


class FakePiperChunk:
    def __init__(self, samples: np.ndarray, sample_rate: int) -> None:
        self.audio_float_array = samples
        self.sample_rate = sample_rate


class FakeVoice:
    def __init__(self, chunks: list[FakePiperChunk]) -> None:
        self.chunks = chunks
        self.calls: list[str] = []
        self.syn_configs: list = []

    def synthesize(self, text: str, syn_config=None):
        self.calls.append(text)
        self.syn_configs.append(syn_config)
        return self.chunks


async def test_synthesize_yields_chunks_from_the_voice():
    samples = np.array([0.1, -0.2, 0.3], dtype=np.float32)
    voice = FakeVoice([FakePiperChunk(samples, 22050)])
    backend = PiperBackend(Path("unused.onnx"), voice=voice)

    result = [chunk async for chunk in backend.synthesize("hello")]

    assert len(result) == 1
    assert isinstance(result[0], AudioChunk)
    assert np.array_equal(result[0].samples, samples)
    assert result[0].sample_rate == 22050
    assert voice.calls == ["hello"]


async def test_synthesize_passes_length_scale_through_to_syn_config():
    voice = FakeVoice([FakePiperChunk(np.zeros(1, dtype=np.float32), 22050)])
    backend = PiperBackend(Path("unused.onnx"), voice=voice, length_scale=1.15)

    await anext(backend.synthesize("hello"))

    assert voice.syn_configs[0].length_scale == 1.15


async def test_synthesize_defaults_length_scale_to_none():
    voice = FakeVoice([FakePiperChunk(np.zeros(1, dtype=np.float32), 22050)])
    backend = PiperBackend(Path("unused.onnx"), voice=voice)

    await anext(backend.synthesize("hello"))

    assert voice.syn_configs[0].length_scale is None


async def test_synthesize_yields_one_chunk_per_sentence():
    chunks = [
        FakePiperChunk(np.array([0.1], dtype=np.float32), 22050),
        FakePiperChunk(np.array([0.2], dtype=np.float32), 22050),
    ]
    voice = FakeVoice(chunks)
    backend = PiperBackend(Path("unused.onnx"), voice=voice)

    result = [chunk async for chunk in backend.synthesize("Hi. There.")]

    assert len(result) == 2


async def test_synthesize_with_no_sentences_yields_nothing():
    voice = FakeVoice([])
    backend = PiperBackend(Path("unused.onnx"), voice=voice)

    result = [chunk async for chunk in backend.synthesize("")]

    assert result == []


async def test_loads_the_voice_lazily_and_only_once():
    """`voice=None` means the real `PiperVoice.load` path -- can't unit test
    that without a real model file, but we can confirm the injected fake is
    reused across calls rather than reloaded (mirrors the lazy-init pattern
    `AnthropicBackend`'s client uses).
    """
    voice = FakeVoice([FakePiperChunk(np.array([0.0], dtype=np.float32), 22050)])
    backend = PiperBackend(Path("unused.onnx"), voice=voice)

    assert backend._load_voice() is voice
    assert backend._load_voice() is voice


def test_fix_pronunciation_rewrites_chao_to_chow():
    assert _fix_pronunciation("chao is happy") == "chow is happy"


def test_fix_pronunciation_rewrites_the_possessive():
    assert _fix_pronunciation("chao's ball") == "chow's ball"


def test_fix_pronunciation_preserves_capitalization():
    assert _fix_pronunciation("Chao waved") == "Chow waved"


def test_fix_pronunciation_leaves_unrelated_words_alone():
    assert _fix_pronunciation("hello there") == "hello there"


def test_fix_pronunciation_does_not_touch_chaos():
    assert _fix_pronunciation("total chaos") == "total chaos"


def test_fix_pronunciation_handles_a_quoted_word():
    assert _fix_pronunciation("say 'chao' now") == "say 'chow' now"


async def test_synthesize_applies_the_pronunciation_fix_before_calling_the_voice():
    voice = FakeVoice([FakePiperChunk(np.array([0.0], dtype=np.float32), 22050)])
    backend = PiperBackend(Path("unused.onnx"), voice=voice)

    [_ async for _ in backend.synthesize("chao is here")]

    assert voice.calls == ["chow is here"]
