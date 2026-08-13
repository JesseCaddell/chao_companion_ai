"""Envelope extraction: TTS audio -> a per-frame speech-energy signal for
non-mouth motion (design doc §8.1). Pure, no I/O -- testable directly, same
category as mood.py's decay function and the fly hysteresis.

The rig has one motion channel, not two. `docs/rigging_check_list.md` item
4: `ParamBodyAngleX/Y/Z` are confirmed inert in this model's art;
`ParamAngleY` alone, live-tested, visibly moves head and body together.
So this module produces a single per-frame amplitude, not separate
body/head signals -- §8.1's "body bob" and "head nod" collapse to one
output on this rig.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml


@dataclass(frozen=True, slots=True)
class MotionConfig:
    parameter_name: str = "ChaoHeadBob"
    # 30, not design doc §8.1's nominal 60 -- a live measurement against
    # the real VTS instance (SESSION_STATE.md session 10) found a single
    # InjectParameterDataRequest round trip averages ~17ms, right at 60Hz's
    # 16.7ms budget with zero headroom for the concurrency lock (see
    # outputs/vts.py) or scheduling jitter. 30Hz leaves ~2x headroom.
    fps: float = 30.0
    # Fixed reference, not per-sentence peak normalization -- a quiet
    # sentence should bob less than a loud one, not get stretched to fill
    # the same range every time. Tuning placeholder, not measured against
    # the real voice's typical RMS yet.
    reference_rms: float = 0.1
    attack_s: float = 0.03
    release_s: float = 0.15
    # Scaled to ChaoHeadBob's confirmed-bound range on ParamAngleY (-30..30
    # in VTS, see rigging_check_list.md item 4) -- amplitude is the peak
    # value at normalized energy 1.0, not the parameter's own min/max.
    # Session 10: live-verified at 15.0 (synced, smooth), then bumped to
    # 25.0 on the user's call -- an animated character reads better
    # exaggerated than tame, and 15 looked conservative on screen.
    amplitude: float = 25.0
    # The VTS-side custom parameter's own range, used only when
    # (re)creating it via ParameterCreationRequest -- must match
    # ParamAngleY's range for the binding to make sense. Not a tuning
    # knob like amplitude above; changing this without also rebinding in
    # VTS would desync the two.
    param_min: float = -30.0
    param_max: float = 30.0


def load_motion_config(path: Path) -> MotionConfig:
    """I/O lives here, not in extract_envelope -- same load/assemble split
    as mood.py's load_mood_config. Reads the `motion:` block out of
    `config/chao.yaml`.
    """
    data = yaml.safe_load(path.read_text()) or {} if path.exists() else {}
    raw = data.get("motion") or {}
    defaults = MotionConfig()
    return MotionConfig(
        parameter_name=str(raw.get("parameter_name", defaults.parameter_name)),
        fps=float(raw.get("fps", defaults.fps)),
        reference_rms=float(raw.get("reference_rms", defaults.reference_rms)),
        attack_s=float(raw.get("attack_s", defaults.attack_s)),
        release_s=float(raw.get("release_s", defaults.release_s)),
        amplitude=float(raw.get("amplitude", defaults.amplitude)),
        param_min=float(raw.get("param_min", defaults.param_min)),
        param_max=float(raw.get("param_max", defaults.param_max)),
    )


def extract_envelope(
    samples: np.ndarray, sample_rate: int, config: MotionConfig | None = None
) -> list[float]:
    """RMS per `1/config.fps`-second block, normalized against
    `config.reference_rms` (not the buffer's own peak) and clamped to
    [0, 1], then attack/release smoothed and scaled by `config.amplitude`.
    One value per frame across the whole buffer's duration -- the caller
    (`outputs/vts.py`'s `VTSMotionPlayer`) paces injection against this
    list's length at `config.fps`, which is the "replay against the audio
    clock" from §8.1: both playback and this list start together and run
    the same real-time duration.
    """
    if len(samples) == 0 or sample_rate <= 0:
        return []
    config = config or MotionConfig()

    frame_len = max(1, round(sample_rate / config.fps))
    frame_dt = frame_len / sample_rate

    normalized: list[float] = []
    for start in range(0, len(samples), frame_len):
        window = samples[start : start + frame_len]
        rms = float(np.sqrt(np.mean(np.square(window, dtype=np.float64))))
        level = min(1.0, rms / config.reference_rms) if config.reference_rms > 0 else 0.0
        normalized.append(level)

    return [v * config.amplitude for v in _smooth(normalized, config, frame_dt)]


def _smooth(values: list[float], config: MotionConfig, frame_dt: float) -> list[float]:
    """One-pole attack/release filter -- short attack (fast rise on
    increasing energy) so onsets read crisply, longer release (slow decay)
    so it doesn't chatter between syllables. Zero-initialized: a sentence
    always bobs in from rest rather than snapping to its first frame's
    level.
    """
    if not values:
        return []
    attack_coef = _pole_coefficient(config.attack_s, frame_dt)
    release_coef = _pole_coefficient(config.release_s, frame_dt)

    out: list[float] = []
    level = 0.0
    for v in values:
        coef = attack_coef if v > level else release_coef
        level += (v - level) * coef
        out.append(level)
    return out


def _pole_coefficient(time_constant_s: float, dt: float) -> float:
    if time_constant_s <= 0:
        return 1.0
    return 1.0 - math.exp(-dt / time_constant_s)
