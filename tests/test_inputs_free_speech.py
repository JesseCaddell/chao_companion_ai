from chao.inputs.free_speech import FreeSpeech


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_disabled_by_default():
    fs = FreeSpeech()
    assert fs.enabled is False


def test_disabled_never_fires():
    clock = FakeClock()
    fs = FreeSpeech(interval_s=10.0, clock=clock)
    clock.advance(100.0)
    assert fs.tick() is False


def test_fires_once_interval_elapses():
    clock = FakeClock()
    fs = FreeSpeech(enabled=True, interval_s=10.0, clock=clock)
    clock.advance(9.0)
    assert fs.tick() is False
    clock.advance(1.0)
    assert fs.tick() is True


def test_does_not_fire_again_until_next_interval():
    clock = FakeClock()
    fs = FreeSpeech(enabled=True, interval_s=10.0, clock=clock)
    clock.advance(10.0)
    assert fs.tick() is True
    assert fs.tick() is False  # immediately after -- no time has passed
    clock.advance(10.0)
    assert fs.tick() is True


def test_enabling_after_a_long_disabled_stretch_does_not_fire_immediately():
    """The disabled clock must not "bank" elapsed time -- otherwise
    flipping enabled=True after being off for a while fires on the very
    next tick, surprising the user with instant unprompted speech.
    """
    clock = FakeClock()
    fs = FreeSpeech(enabled=False, interval_s=10.0, clock=clock)
    clock.advance(1000.0)
    fs.tick()  # still disabled, pins _last_trigger to "now"

    fs.enabled = True
    assert fs.tick() is False  # no time has passed since enabling
    clock.advance(10.0)
    assert fs.tick() is True


def test_disabling_and_re_enabling_mid_interval_restarts_the_wait():
    clock = FakeClock()
    fs = FreeSpeech(enabled=True, interval_s=10.0, clock=clock)
    clock.advance(9.0)
    fs.enabled = False
    fs.tick()  # disabled -- resets the trigger clock
    fs.enabled = True
    clock.advance(9.0)
    assert fs.tick() is False  # only 9s since re-enabling, not 18s total
    clock.advance(1.0)
    assert fs.tick() is True


def test_interval_s_is_mutable_and_takes_effect_immediately():
    clock = FakeClock()
    fs = FreeSpeech(enabled=True, interval_s=100.0, clock=clock)
    fs.interval_s = 5.0
    clock.advance(5.0)
    assert fs.tick() is True
