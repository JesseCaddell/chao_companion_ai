import itertools
import math
import random

import pytest

from chao.director.idle_drift import IdleDrift, IdleDriftConfig, load_idle_drift_config, smoothstep


class FakeClock:
    """Same shape as aliveness.py's/mood.py's test fakes -- a manually-
    advanced clock so phase-timing assertions are exact instead of racing
    real wall-clock jitter.
    """

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_config(**overrides) -> IdleDriftConfig:
    defaults = {
        "x_parameter_name": "ChaoHeadTurn",
        "z_parameter_name": "ChaoHeadTilt",
        "fps": 15.0,
        "x_amplitude": 22.0,
        "z_amplitude": 12.0,
        "min_move_distance": 8.0,
        "ease_min_s": 2.0,
        "ease_max_s": 2.0,
        "hold_min_s": 3.0,
        "hold_max_s": 3.0,
    }
    defaults.update(overrides)
    return IdleDriftConfig(**defaults)


def test_smoothstep_boundary_values():
    assert smoothstep(0.0) == 0.0
    assert smoothstep(1.0) == 1.0


def test_smoothstep_clamps_outside_unit_range():
    assert smoothstep(-0.5) == 0.0
    assert smoothstep(1.5) == 1.0


def test_smoothstep_is_symmetric_at_midpoint():
    assert smoothstep(0.5) == 0.5


def test_smoothstep_is_monotonic():
    values = [smoothstep(t / 10) for t in range(11)]
    assert values == sorted(values)


def test_idle_drift_starts_holding_at_origin():
    clock = FakeClock()
    idle = IdleDrift(config=make_config(), clock=clock, rng=random.Random(1))

    assert idle.tick() == (0.0, 0.0)
    clock.advance(1.0)  # still well within hold_min_s/max_s == 3.0
    assert idle.tick() == (0.0, 0.0)


def test_idle_drift_full_hold_ease_hold_cycle():
    """Walks one complete cycle, checking the properties that actually
    matter: holds are static, the ease starts exactly at the start value,
    passes through the analytically-known smoothstep(0.5) midpoint, and
    snaps exactly to the target at the end -- not an approximation.
    """
    clock = FakeClock()
    config = make_config(ease_min_s=2.0, ease_max_s=2.0, hold_min_s=3.0, hold_max_s=3.0)
    idle = IdleDrift(config=config, clock=clock, rng=random.Random(7))

    # Initial hold (3s) -- static.
    assert idle.tick() == (0.0, 0.0)
    clock.advance(3.0)  # exactly at the hold boundary

    # First tick past the boundary transitions to easing; t=0 at the
    # instant the ease starts, so the value is still the start value.
    start_value = idle.tick()
    assert start_value == (0.0, 0.0)

    clock.advance(1.0)  # halfway through the 2s ease
    midpoint_value = idle.tick()

    clock.advance(1.0)  # exactly at the ease boundary -> snaps to target
    target_value = idle.tick()

    # The target must actually be a real move -- min_move_distance=8
    # guarantees this isn't a near-adjacent, invisible "ease."
    assert math.hypot(target_value[0], target_value[1]) >= config.min_move_distance
    assert abs(target_value[0]) <= config.x_amplitude
    assert abs(target_value[1]) <= config.z_amplitude

    # smoothstep(0.5) == 0.5 exactly -> midpoint must be the arithmetic
    # mean of start and target, not just "somewhere in between."
    assert midpoint_value[0] == pytest.approx((start_value[0] + target_value[0]) / 2)
    assert midpoint_value[1] == pytest.approx((start_value[1] + target_value[1]) / 2)

    # Now holding at the target -- static across further ticks, even as
    # the clock advances within the hold window.
    assert idle.tick() == target_value
    clock.advance(1.5)
    assert idle.tick() == target_value


def test_idle_drift_late_tick_does_not_overshoot_target():
    """A tick() that arrives long after the ease should have ended (real
    scheduling jitter, or a test jumping the clock in one big leap) must
    land exactly on the target, not something extrapolated past it.
    """
    clock = FakeClock()
    config = make_config(ease_min_s=1.0, ease_max_s=1.0, hold_min_s=1.0, hold_max_s=1.0)
    idle = IdleDrift(config=config, clock=clock, rng=random.Random(3))

    clock.advance(1.0)
    idle.tick()  # enters easing
    clock.advance(50.0)  # wildly past the 1s ease duration in one jump

    value = idle.tick()
    assert abs(value[0]) <= config.x_amplitude
    assert abs(value[1]) <= config.z_amplitude
    # Immediately re-ticking (no further clock advance) must return the
    # same value -- proof the state actually settled into "holding"
    # rather than continuing to extrapolate.
    assert idle.tick() == value


def test_idle_drift_min_move_distance_and_amplitude_bounds_hold_across_many_transitions():
    clock = FakeClock()
    config = make_config(
        ease_min_s=0.1, ease_max_s=0.1, hold_min_s=0.1, hold_max_s=0.1, min_move_distance=8.0
    )
    idle = IdleDrift(config=config, clock=clock, rng=random.Random(11))

    # Advance with clear margin past each 0.1s phase boundary, not exactly
    # 0.1 -- repeated float addition of 0.1 accumulates rounding error and
    # can land `elapsed` just *under` the stored phase_duration on some
    # iterations, missing a transition for one tick (self-corrects on the
    # next real-world tick, but produces a spurious duplicate in a test
    # that samples exactly at the boundary).
    targets = []
    for _ in range(30):
        clock.advance(0.15)  # comfortably past end of hold -> start of ease
        idle.tick()
        clock.advance(0.15)  # comfortably past end of ease -> snapped to target
        targets.append(idle.tick())

    for x, z in targets:
        assert abs(x) <= config.x_amplitude
        assert abs(z) <= config.z_amplitude
    for (x1, z1), (x2, z2) in itertools.pairwise(targets):
        assert math.hypot(x2 - x1, z2 - z1) >= config.min_move_distance


def test_idle_drift_is_deterministic_given_same_seed_across_several_transitions():
    def run(seed: int) -> list[tuple[float, float]]:
        clock = FakeClock()
        config = make_config(ease_min_s=0.1, ease_max_s=0.1, hold_min_s=0.1, hold_max_s=0.1)
        idle = IdleDrift(config=config, clock=clock, rng=random.Random(seed))
        values = []
        for _ in range(20):
            clock.advance(0.05)
            values.append(idle.tick())
        return values

    assert run(42) == run(42)
    assert run(1) != run(2)


def test_load_idle_drift_config_reads_the_idle_drift_block(tmp_path):
    config_path = tmp_path / "chao.yaml"
    config_path.write_text(
        "idle_drift:\n"
        "  x_parameter_name: TestX\n"
        "  z_parameter_name: TestZ\n"
        "  fps: 5\n"
        "  x_amplitude: 20\n"
        "  z_amplitude: 10\n"
        "  min_move_distance: 6\n"
        "  ease_min_s: 1.0\n"
        "  ease_max_s: 2.0\n"
        "  hold_min_s: 2.0\n"
        "  hold_max_s: 4.0\n"
        "  param_min: -40\n"
        "  param_max: 40\n"
    )

    config = load_idle_drift_config(config_path)

    assert config == IdleDriftConfig(
        x_parameter_name="TestX",
        z_parameter_name="TestZ",
        fps=5.0,
        x_amplitude=20.0,
        z_amplitude=10.0,
        min_move_distance=6.0,
        ease_min_s=1.0,
        ease_max_s=2.0,
        hold_min_s=2.0,
        hold_max_s=4.0,
        param_min=-40.0,
        param_max=40.0,
    )


def test_load_idle_drift_config_defaults_when_file_missing(tmp_path):
    config = load_idle_drift_config(tmp_path / "does_not_exist.yaml")

    assert config == IdleDriftConfig()


def test_load_idle_drift_config_defaults_when_block_absent(tmp_path):
    config_path = tmp_path / "chao.yaml"
    config_path.write_text("tts:\n  voice_path: foo.onnx\n")

    config = load_idle_drift_config(config_path)

    assert config == IdleDriftConfig()
