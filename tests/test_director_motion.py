import numpy as np

from chao.director.motion import MotionConfig, extract_envelope, load_motion_config


def constant_samples(value: float, n: int) -> np.ndarray:
    return np.full(n, value, dtype=np.float32)


def test_extract_envelope_empty_samples_returns_empty():
    assert extract_envelope(np.array([], dtype=np.float32), 1000) == []


def test_extract_envelope_zero_sample_rate_returns_empty():
    assert extract_envelope(constant_samples(1.0, 100), 0) == []


def test_extract_envelope_one_frame_per_configured_block():
    # sample_rate=1000, fps=100 -> frame_len=10 samples/frame, 5 frames total
    config = MotionConfig(fps=100.0, reference_rms=1.0, amplitude=1.0)
    envelope = extract_envelope(constant_samples(1.0, 50), 1000, config)
    assert len(envelope) == 5


def test_extract_envelope_louder_signal_produces_higher_steady_state():
    config = MotionConfig(fps=100.0, reference_rms=1.0, amplitude=1.0, attack_s=0.001)
    quiet = extract_envelope(constant_samples(0.2, 500), 1000, config)
    loud = extract_envelope(constant_samples(0.8, 500), 1000, config)
    assert quiet[-1] < loud[-1]


def test_extract_envelope_clamps_at_amplitude_even_when_rms_exceeds_reference():
    config = MotionConfig(fps=100.0, reference_rms=0.1, amplitude=15.0, attack_s=0.001)
    envelope = extract_envelope(constant_samples(5.0, 500), 1000, config)
    assert all(v <= 15.0 + 1e-9 for v in envelope)
    assert envelope[-1] == 15.0


def test_extract_envelope_silence_produces_zero():
    config = MotionConfig(fps=100.0, reference_rms=0.1, amplitude=15.0)
    envelope = extract_envelope(constant_samples(0.0, 500), 1000, config)
    assert all(v == 0.0 for v in envelope)


def test_extract_envelope_smooths_rather_than_snaps_on_attack():
    """A loud block right after silence must not jump straight to the
    full scaled value in one frame -- that's the whole point of the
    attack/release filter over raw per-frame RMS.
    """
    sample_rate = 1000
    frame_len = 10  # matches fps=100
    silence = np.zeros(frame_len, dtype=np.float32)
    loud = np.full(frame_len, 1.0, dtype=np.float32)
    samples = np.concatenate([silence, loud, loud, loud])
    config = MotionConfig(fps=100.0, reference_rms=1.0, amplitude=10.0, attack_s=0.03)

    envelope = extract_envelope(samples, sample_rate, config)

    assert envelope[0] == 0.0
    assert 0.0 < envelope[1] < 10.0  # rising, not snapped
    assert envelope[1] < envelope[2] < envelope[3]  # monotone approach


def test_extract_envelope_releases_gradually_after_loud_block():
    sample_rate = 1000
    frame_len = 10
    loud = np.full(frame_len, 1.0, dtype=np.float32)
    silence = np.zeros(frame_len, dtype=np.float32)
    samples = np.concatenate([loud, loud, loud, silence, silence, silence])
    config = MotionConfig(
        fps=100.0, reference_rms=1.0, amplitude=10.0, attack_s=0.001, release_s=0.1
    )

    envelope = extract_envelope(samples, sample_rate, config)

    peak = envelope[2]
    assert 0.0 < envelope[3] < peak  # decaying
    assert envelope[3] > envelope[4] > envelope[5]  # monotone decay, not an instant drop


def test_load_motion_config_reads_the_motion_block(tmp_path):
    config_path = tmp_path / "chao.yaml"
    config_path.write_text(
        "motion:\n"
        "  parameter_name: TestParam\n"
        "  fps: 45\n"
        "  reference_rms: 0.2\n"
        "  attack_s: 0.01\n"
        "  release_s: 0.2\n"
        "  amplitude: 20\n"
        "  param_min: -40\n"
        "  param_max: 40\n"
    )

    config = load_motion_config(config_path)

    assert config == MotionConfig(
        parameter_name="TestParam",
        fps=45.0,
        reference_rms=0.2,
        attack_s=0.01,
        release_s=0.2,
        amplitude=20.0,
        param_min=-40.0,
        param_max=40.0,
    )


def test_load_motion_config_defaults_when_file_missing(tmp_path):
    config = load_motion_config(tmp_path / "does_not_exist.yaml")

    assert config == MotionConfig()


def test_load_motion_config_defaults_when_motion_block_absent(tmp_path):
    config_path = tmp_path / "chao.yaml"
    config_path.write_text("tts:\n  voice_path: foo.onnx\n")

    config = load_motion_config(config_path)

    assert config == MotionConfig()
