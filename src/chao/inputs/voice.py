"""Continuous mic capture -> Silero VAD endpointing -> faster-whisper
transcription (design doc §4.1, §5, §12, §18). Publishes `Kind.INPUT_VOICE`
with `{text, confidence, duration_ms}`.

**VAD**: the raw Silero VAD ONNX model (`data/vad/silero_vad.onnx`,
gitignored, downloaded once from the upstream repo -- see
`docs/rigging_check_list.md`-style session notes in SESSION_STATE.md),
loaded directly via `onnxruntime` rather than the `silero-vad` pip
package. Confirmed before choosing this: installing that package pulls a
full ~200MB CPU `torch` (plus `torchaudio`/`sympy`/`networkx`) for a
2.3MB model file -- exactly the footprint bloat invariant 1 exists to
prevent, on a machine where the whole point is staying out of the game's
way. `onnxruntime` is already present transitively via `piper-tts`.

**STT**: `faster-whisper` `base`/int8/CPU (design doc §18, invariant 1 --
never anything but `device="cpu"`). Verified via a throwaway probe against
a real synthesized WAV before writing this module: ~500ms for a ~3s clip
(design doc §12 budgets 300ms; close enough, not chased further), and
word-perfect transcription -- but only once audio reaches
`faster_whisper.audio.decode_audio`'s own resampling path. A hand-rolled
resample in an earlier probe attempt produced garbled transcriptions, not
a Piper voice-quality issue. Real mic capture sidesteps that whole problem
by recording at 16kHz directly (`VoiceConfig.sample_rate`), so nothing in
this module resamples by hand.

Also confirmed, and worth recording since it looked like a bug at first:
Silero VAD does **not** reliably detect Piper-synthesized speech as speech
(probabilities stayed under 0.1 against correctly-decoded audio) --
expected, not a defect. VAD is trained on real human speech; TTS output
has different spectral characteristics. This means synthetic audio isn't
a valid VAD test corpus -- real validation needs an actual human voice
through a real mic (see SESSION_STATE.md's live-check notes for this
module).

**Echo guard, not full duplex**: `tts.output_device` can point at the
system default (speakers), which the mic can pick back up -- the chao
would transcribe and respond to itself, a real feedback loop, not a
hypothetical. `VoiceInput.on_speech_start`/`on_speech_end` (driven by
`__main__.py` watching `output.speech_start`/`output.speech_end`) gate
this: an utterance whose endpoint lands while the chao is mid-speech is
discarded before STT ever runs on it, not just before publishing.

**Known, deliberate scope cut**: this also means the streamer cannot
currently barge in over the chao's own speech -- discussed with `advisor`
before building. The alternative (letting a second `input.voice` preempt
an in-flight voice-triggered turn) requires loosening `bus.py`'s strict
`priority > self._current.priority` preemption check, which is a bus-
contract change CLAUDE.md flags for escalation, and doing it without also
solving echo (distinguishing the human's real interruption from the
chao's own bleed) would just trade one feedback-loop-shaped bug for
another. Real barge-in needs acoustic echo cancellation against a known
reference signal -- out of scope for this pass, same as the standing
"no reconnect loop" cut in `inputs/twitch.py`.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np
import yaml

from chao.events import Event, Kind

DEFAULT_VAD_MODEL_PATH = Path("data") / "vad" / "silero_vad.onnx"


@dataclass(frozen=True, slots=True)
class VoiceConfig:
    # Off by default -- continuous mic capture is a real privacy-relevant
    # behavior change, not something that should silently start just
    # because the module exists. Same opt-in shape as tts.voice_path/
    # twitch.channel defaulting to "unset".
    enabled: bool = False
    input_device: str | None = None
    sample_rate: int = 16000
    frame_samples: int = 512  # Silero's recommended chunk size at 16kHz
    vad_threshold: float = 0.5
    silence_ms: float = 500.0  # design doc §4.1: "the single biggest perceptual latency lever"
    min_speech_ms: float = 250.0  # floor to reject noise blips, not real utterances
    max_utterance_s: float = 30.0  # safety cap if VAD never sees silence
    whisper_model: str = "base"
    whisper_compute_type: str = "int8"
    whisper_language: str | None = "en"
    vad_model_path: str = str(DEFAULT_VAD_MODEL_PATH)


def load_voice_config(path: Path) -> VoiceConfig:
    """Same load/assemble split as every other `*_config` in this project.
    Reads the `voice:` block out of `config/chao.yaml`.
    """
    data = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    raw = data.get("voice") or {}
    defaults = VoiceConfig()
    return VoiceConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        input_device=raw.get("input_device", defaults.input_device),
        sample_rate=int(raw.get("sample_rate", defaults.sample_rate)),
        frame_samples=int(raw.get("frame_samples", defaults.frame_samples)),
        vad_threshold=float(raw.get("vad_threshold", defaults.vad_threshold)),
        silence_ms=float(raw.get("silence_ms", defaults.silence_ms)),
        min_speech_ms=float(raw.get("min_speech_ms", defaults.min_speech_ms)),
        max_utterance_s=float(raw.get("max_utterance_s", defaults.max_utterance_s)),
        whisper_model=str(raw.get("whisper_model", defaults.whisper_model)),
        whisper_compute_type=str(raw.get("whisper_compute_type", defaults.whisper_compute_type)),
        whisper_language=raw.get("whisper_language", defaults.whisper_language),
        vad_model_path=str(raw.get("vad_model_path", defaults.vad_model_path)),
    )


class _VADLike(Protocol):
    """Just the shape `VoiceEndpointer` actually calls -- lets tests inject
    a fake instead of a real ONNX session, same pattern as `outputs/vts.py`'s
    `VTSTransport`.
    """

    def probability(self, chunk: np.ndarray) -> float: ...
    def reset(self) -> None: ...


class SileroVAD:
    """Thin wrapper over the raw ONNX model -- see module docstring for why
    this isn't the `silero-vad` pip package. `state` is Silero's own
    recurrent state (shape `(2, 1, 128)`), carried between calls and reset
    at each utterance boundary by `VoiceEndpointer`.

    **Context prefix, not optional**: the model was traced expecting each
    call's `input` to be `context_size + chunk_size` samples (64 + 512 at
    16kHz), where the context is the *last `context_size` samples of the
    previous chunk* -- not zero-padding, and not something the ONNX graph
    itself handles; the caller has to maintain it. Confirmed by reading
    Silero's own reference `OnnxWrapper.__call__` (upstream `utils_vad.py`)
    after this class's first version -- which fed bare 512-sample chunks
    with no context -- produced near-zero probability for real, live,
    close-mic human speech (max ~0.001, verified against several live
    mic tests, session 10 part 12) despite plausible audio amplitude at
    every stage. The shape mismatch never raised: the ONNX graph's `input`
    dimension is fully dynamic (`[None, None]`), so an under-sized input
    just silently produces numerically wrong output instead of an error.
    """

    _CONTEXT_SAMPLES = {16000: 64, 8000: 32}

    def __init__(self, model_path: Path, *, sample_rate: int = 16000) -> None:
        import onnxruntime as ort

        self._session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self._sample_rate = sample_rate
        self._context_size = self._CONTEXT_SAMPLES.get(sample_rate, 64)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(self._context_size, dtype=np.float32)

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(self._context_size, dtype=np.float32)

    def probability(self, chunk: np.ndarray) -> float:
        sr = np.array(self._sample_rate, dtype=np.int64)
        x = np.concatenate([self._context, chunk]).astype(np.float32)
        out, self._state = self._session.run(
            ["output", "stateN"],
            {"input": x[None, :], "state": self._state, "sr": sr},
        )
        self._context = x[-self._context_size :]
        return float(out[0][0])


@dataclass
class VoiceEndpointer:
    """Hold-until-silence endpointing, same state-machine shape as
    `IdleDrift`/`Fly` (clock-injectable, pure aside from the VAD call).
    `feed()` takes one `frame_samples`-length chunk at a time and returns
    the buffered utterance once `silence_ms` of non-speech follows real
    speech -- `None` on every other call. An utterance shorter than
    `min_speech_ms` is discarded (returns `None`) rather than transcribed,
    since that's almost always a noise blip, not real speech.
    """

    config: VoiceConfig
    vad: _VADLike
    clock: Callable[[], float] = time.monotonic

    _state: str = field(default="idle", init=False)
    _buffer: list[np.ndarray] = field(default_factory=list, init=False)
    _speech_start: float = field(default=0.0, init=False)
    _last_speech_ts: float = field(default=0.0, init=False)

    def feed(self, chunk: np.ndarray) -> np.ndarray | None:
        prob = self.vad.probability(chunk)
        now = self.clock()
        is_speech = prob >= self.config.vad_threshold

        if self._state == "idle":
            if is_speech:
                self._state = "speaking"
                self._buffer = [chunk]
                self._speech_start = now
                self._last_speech_ts = now
            return None

        self._buffer.append(chunk)
        if is_speech:
            self._last_speech_ts = now

        silence_elapsed_ms = (now - self._last_speech_ts) * 1000
        utterance_ms = (now - self._speech_start) * 1000
        if (
            silence_elapsed_ms < self.config.silence_ms
            and utterance_ms < self.config.max_utterance_s * 1000
        ):
            return None

        utterance = np.concatenate(self._buffer)
        self._reset_for_next_utterance()
        if utterance_ms < self.config.min_speech_ms:
            return None
        return utterance

    def _reset_for_next_utterance(self) -> None:
        self._state = "idle"
        self._buffer = []
        self.vad.reset()


class WhisperBackend:
    """Lazy-loaded, same pattern as `PiperBackend._load_voice` -- a test
    injecting a fake underlying model shouldn't have to pay a real model
    load. `transcribe` is blocking CPU work; callers run it via
    `loop.run_in_executor`, same as `PiperBackend.synthesize`'s own
    inference call.
    """

    def __init__(
        self,
        model_size: str,
        *,
        compute_type: str = "int8",
        language: str | None = "en",
    ) -> None:
        self._model_size = model_size
        self._compute_type = compute_type
        self._language = language
        self._model = None

    def _load_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            # device="cpu" is not a config knob -- invariant 1, always.
            self._model = WhisperModel(
                self._model_size, device="cpu", compute_type=self._compute_type
            )
        return self._model

    def transcribe(self, audio: np.ndarray) -> tuple[str, float]:
        """Returns `(text, confidence)`. `faster-whisper` doesn't expose a
        0-1 confidence directly -- `avg_logprob` is a log-probability
        (<=0), so `exp()` maps it onto `(0, 1]` as a rough proxy. Empty
        transcription (silence that slipped past VAD, or genuinely
        unintelligible audio) returns `("", 0.0)` rather than raising.
        """
        segments = list(self._load_model().transcribe(audio, language=self._language)[0])
        if not segments:
            return "", 0.0
        text = " ".join(s.text.strip() for s in segments).strip()
        avg_logprob = sum(s.avg_logprob for s in segments) / len(segments)
        return text, math.exp(avg_logprob)


def resolve_input_device(name_substring: str | None) -> int | None:
    """Mirrors `outputs/audio.py`'s `resolve_output_device`, filtered to
    input-capable devices instead. `None` (no substring configured) falls
    back to the system default input device.
    """
    if not name_substring:
        return None
    import sounddevice as sd

    needle = name_substring.lower()
    for index, info in enumerate(sd.query_devices()):
        if info["max_input_channels"] > 0 and needle in info["name"].lower():
            return index
    print(f"  (no input device matching '{name_substring}', using system default)")
    return None


def _start_sounddevice_stream(*, samplerate: int, blocksize: int, device: int | None, callback):
    import sounddevice as sd

    return sd.InputStream(
        samplerate=samplerate,
        channels=1,
        dtype="float32",
        blocksize=blocksize,
        device=device,
        callback=callback,
    )


class VoiceInput:
    def __init__(
        self,
        *,
        config: VoiceConfig,
        publish: Callable[[Event], None],
        vad: _VADLike | None = None,
        whisper: WhisperBackend | None = None,
        endpointer: VoiceEndpointer | None = None,
        start_stream: Callable = _start_sounddevice_stream,
    ) -> None:
        self.config = config
        self.publish = publish
        self._vad = vad or SileroVAD(Path(config.vad_model_path), sample_rate=config.sample_rate)
        self._whisper = whisper or WhisperBackend(
            config.whisper_model,
            compute_type=config.whisper_compute_type,
            language=config.whisper_language,
        )
        self._endpointer = endpointer or VoiceEndpointer(config=config, vad=self._vad)
        self._start_stream = start_stream
        self._speaking = False

    def on_speech_start(self) -> None:
        self._speaking = True

    def on_speech_end(self) -> None:
        self._speaking = False

    async def run(self) -> None:
        """Opens the mic stream and processes frames until cancelled.
        `sounddevice`'s callback fires on its own thread, not the asyncio
        loop -- same class of exception CLAUDE.md's no-threads rule
        already carves out for `AudioPlayer`'s playback and
        `KillSwitch`'s hotkey callbacks. Marshalled onto the loop via
        `call_soon_threadsafe`, same as `KillSwitch`.
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[np.ndarray] = asyncio.Queue()

        def callback(indata, _frames, _time_info, _status) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, indata[:, 0].copy())

        device = resolve_input_device(self.config.input_device)
        stream = self._start_stream(
            samplerate=self.config.sample_rate,
            blocksize=self.config.frame_samples,
            device=device,
            callback=callback,
        )
        with stream:
            while True:
                chunk = await queue.get()
                await self._process_chunk(chunk)

    async def _process_chunk(self, chunk: np.ndarray) -> None:
        utterance = self._endpointer.feed(chunk)
        if utterance is None:
            return
        if self._speaking:
            # Echo guard -- discard without ever running STT on it.
            return
        await self._transcribe_and_publish(utterance)

    async def _transcribe_and_publish(self, audio: np.ndarray) -> None:
        loop = asyncio.get_running_loop()
        text, confidence = await loop.run_in_executor(None, self._whisper.transcribe, audio)
        if not text:
            return
        duration_ms = len(audio) / self.config.sample_rate * 1000
        self.publish(
            Event(
                kind=Kind.INPUT_VOICE,
                payload={"text": text, "confidence": confidence, "duration_ms": duration_ms},
            )
        )
