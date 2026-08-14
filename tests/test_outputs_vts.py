import asyncio
import random

import pytest

from chao.director.aliveness import FlyConfig
from chao.director.director import EmoteConfig, EmotePool
from chao.director.idle_drift import IdleDrift, IdleDriftConfig
from chao.events import Event, Kind
from chao.outputs.vts import (
    VTSEmoteSubscriber,
    VTSFlySubscriber,
    VTSIdleDriftPlayer,
    VTSMotionPlayer,
)


class FakeVTSClient:
    def __init__(
        self,
        fail_on: set[tuple[str, bool]] | None = None,
        responses: dict[str, dict] | None = None,
        fail_message_types: set[str] | None = None,
    ):
        self.calls: list[tuple[str, dict]] = []
        self._fail_on = fail_on or set()
        self._responses = responses or {}
        self._fail_message_types = fail_message_types or set()

    async def request(self, message_type, data=None):
        data = dict(data or {})
        self.calls.append((message_type, data))
        if message_type in self._fail_message_types:
            raise RuntimeError("simulated VTS failure")
        if (data.get("expressionFile"), data.get("active")) in self._fail_on:
            raise RuntimeError("simulated VTS failure")
        return {"data": self._responses.get(message_type, {})}


class RecordingSleep:
    def __init__(self):
        self.durations: list[float] = []

    async def __call__(self, duration: float) -> None:
        self.durations.append(duration)


class FakeClock:
    """Same shape as director/mood.py's and director/aliveness.py's test
    fakes -- a manually-advanced clock so timing assertions are exact
    instead of racing real wall-clock jitter.
    """

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ClockAdvancingSleep:
    """A sleep fake that actually advances the shared FakeClock by the
    slept duration, so a test can assert on both "how long did each sleep
    ask for" and "what did the clock read afterwards" without any real
    delay.
    """

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.durations: list[float] = []

    async def __call__(self, duration: float) -> None:
        self.durations.append(duration)
        self.clock.advance(duration)


class InjectionCostVTSClient(FakeVTSClient):
    """Simulates a real VTS round trip costing real time -- session 10
    measured ~17ms/InjectParameterDataRequest against the live API. Used to
    prove VTSMotionPlayer.play paces to a deadline rather than sleeping a
    full frame period on top of that cost.
    """

    def __init__(self, clock: FakeClock, cost: float, **kwargs):
        super().__init__(**kwargs)
        self._clock = clock
        self._cost = cost

    async def request(self, message_type, data=None):
        result = await super().request(message_type, data)
        self._clock.advance(self._cost)
        return result


class GatedSleep:
    """Lets a test control exactly when a scheduled deactivate proceeds,
    instead of racing real wall-clock durations from config (2-4s).
    """

    def __init__(self):
        self.calls: list[float] = []
        self._events: list[asyncio.Event] = []

    async def __call__(self, duration: float) -> None:
        self.calls.append(duration)
        ev = asyncio.Event()
        self._events.append(ev)
        await ev.wait()

    def release(self, index: int) -> None:
        self._events[index].set()


def make_config(duration_s: float = 2.0, cooldown_s: float = 4.0) -> EmoteConfig:
    return EmoteConfig(
        pools={
            "happy": EmotePool(
                hotkeys=["happy.exp3.json"], cooldown_s=cooldown_s, duration_s=duration_s
            )
        }
    )


def make_two_pool_config(
    duration_s: float = 2.0, channels: tuple[str, str] = ("eyes", "eyes")
) -> EmoteConfig:
    return EmoteConfig(
        pools={
            "curious": EmotePool(
                hotkeys=["question.exp3.json"],
                cooldown_s=3.0,
                duration_s=duration_s,
                channel=channels[0],
            ),
            "confused": EmotePool(
                hotkeys=["confused.exp3.json"],
                cooldown_s=8.0,
                duration_s=duration_s,
                channel=channels[1],
            ),
        }
    )


def emote_event(pool: str = "happy", hotkey_id: str = "happy.exp3.json") -> Event:
    return Event(
        kind=Kind.DIRECTOR_EMOTE, payload={"pool": pool, "hotkey_id": hotkey_id, "reason": pool}
    )


async def test_ignores_non_emote_events():
    client = FakeVTSClient()
    sub = VTSEmoteSubscriber(client=client, emote_config=make_config(), sleep=RecordingSleep())

    await sub.handle(Event(kind=Kind.DIRECTOR_TAG, payload={}))

    assert client.calls == []


async def test_activates_expression_with_configured_fade():
    client = FakeVTSClient()
    sub = VTSEmoteSubscriber(
        client=client, emote_config=make_config(), sleep=RecordingSleep(), fade_time_s=0.3
    )

    await sub.handle(emote_event())

    assert client.calls[0] == (
        "ExpressionActivationRequest",
        {"expressionFile": "happy.exp3.json", "active": True, "fadeTime": 0.3},
    )


async def test_deactivates_after_duration_from_config():
    client = FakeVTSClient()
    sleep = RecordingSleep()
    sub = VTSEmoteSubscriber(client=client, emote_config=make_config(duration_s=1.5), sleep=sleep)

    await sub.handle(emote_event())
    await asyncio.sleep(0)  # let the background deactivate task run to completion

    assert sleep.durations == [1.5]
    assert client.calls[-1] == (
        "ExpressionActivationRequest",
        {"expressionFile": "happy.exp3.json", "active": False, "fadeTime": 0.25},
    )


async def test_unknown_pool_defaults_duration_to_zero():
    client = FakeVTSClient()
    sleep = RecordingSleep()
    sub = VTSEmoteSubscriber(client=client, emote_config=EmoteConfig(pools={}), sleep=sleep)

    await sub.handle(emote_event(pool="not_configured"))
    await asyncio.sleep(0)

    assert sleep.durations == [0.0]


async def test_activation_failure_publishes_error_and_skips_deactivation():
    client = FakeVTSClient(fail_on={("happy.exp3.json", True)})
    sleep = RecordingSleep()
    events: list[Event] = []
    sub = VTSEmoteSubscriber(
        client=client, emote_config=make_config(), sleep=sleep, publish=events.append
    )

    await sub.handle(emote_event())

    assert sleep.durations == []  # no deactivate scheduled -- nothing was activated
    error = next(e for e in events if e.kind == Kind.ERROR)
    assert error.payload["component"] == "vts"


async def test_deactivation_failure_publishes_error_without_raising():
    client = FakeVTSClient(fail_on={("happy.exp3.json", False)})
    sleep = RecordingSleep()
    events: list[Event] = []
    sub = VTSEmoteSubscriber(
        client=client, emote_config=make_config(duration_s=0.0), sleep=sleep, publish=events.append
    )

    await sub.handle(emote_event())
    await asyncio.sleep(0)  # background deactivate task runs, hits the simulated failure

    error = next(e for e in events if e.kind == Kind.ERROR)
    assert error.payload["component"] == "vts"


# --- Per-channel mutual exclusion (session 10 parts 13-14: user-reported live overlap) ---


async def test_second_pool_deactivates_the_first_before_activating():
    client = FakeVTSClient()
    sub = VTSEmoteSubscriber(client=client, emote_config=make_two_pool_config(), sleep=GatedSleep())

    await sub.handle(emote_event(pool="curious", hotkey_id="question.exp3.json"))
    await sub.handle(emote_event(pool="confused", hotkey_id="confused.exp3.json"))

    assert client.calls == [
        (
            "ExpressionActivationRequest",
            {"expressionFile": "question.exp3.json", "active": True, "fadeTime": 0.25},
        ),
        (
            "ExpressionActivationRequest",
            {"expressionFile": "question.exp3.json", "active": False, "fadeTime": 0.25},
        ),
        (
            "ExpressionActivationRequest",
            {"expressionFile": "confused.exp3.json", "active": True, "fadeTime": 0.25},
        ),
    ]


async def test_only_one_expression_active_per_channel_after_two_same_channel_pools_fire():
    client = FakeVTSClient()
    sub = VTSEmoteSubscriber(client=client, emote_config=make_two_pool_config(), sleep=GatedSleep())

    await sub.handle(emote_event(pool="curious", hotkey_id="question.exp3.json"))
    await sub.handle(emote_event(pool="confused", hotkey_id="confused.exp3.json"))

    assert sub._active_expression == {"eyes": "confused.exp3.json"}


async def test_different_channels_do_not_exclude_each_other():
    client = FakeVTSClient()
    config = make_two_pool_config(channels=("eyes", "ball"))
    sub = VTSEmoteSubscriber(client=client, emote_config=config, sleep=GatedSleep())

    await sub.handle(emote_event(pool="curious", hotkey_id="question.exp3.json"))
    await sub.handle(emote_event(pool="confused", hotkey_id="confused.exp3.json"))

    # No deactivate call for question.exp3.json -- eyes and ball ran together.
    assert client.calls == [
        (
            "ExpressionActivationRequest",
            {"expressionFile": "question.exp3.json", "active": True, "fadeTime": 0.25},
        ),
        (
            "ExpressionActivationRequest",
            {"expressionFile": "confused.exp3.json", "active": True, "fadeTime": 0.25},
        ),
    ]
    assert sub._active_expression == {"eyes": "question.exp3.json", "ball": "confused.exp3.json"}


async def test_same_file_reactivating_does_not_deactivate_itself():
    client = FakeVTSClient()
    sub = VTSEmoteSubscriber(client=client, emote_config=make_config(), sleep=GatedSleep())

    await sub.handle(emote_event())
    await sub.handle(emote_event())  # same pool/file again -- extends the hold

    assert client.calls == [
        (
            "ExpressionActivationRequest",
            {"expressionFile": "happy.exp3.json", "active": True, "fadeTime": 0.25},
        ),
        (
            "ExpressionActivationRequest",
            {"expressionFile": "happy.exp3.json", "active": True, "fadeTime": 0.25},
        ),
    ]


async def test_supplanted_expressions_scheduled_deactivate_does_not_double_fire():
    client = FakeVTSClient()
    sleep = GatedSleep()
    sub = VTSEmoteSubscriber(client=client, emote_config=make_two_pool_config(), sleep=sleep)

    await sub.handle(emote_event(pool="curious", hotkey_id="question.exp3.json"))
    await sub.handle(emote_event(pool="confused", hotkey_id="confused.exp3.json"))
    # The first pool's own scheduled auto-deactivate must have been
    # cancelled by the supplant, not left pending to fire later and
    # clobber _active_expression/emit a redundant deactivate call.
    count_before = len(client.calls)
    await asyncio.sleep(0)
    assert len(client.calls) == count_before


async def test_active_expression_clears_after_natural_deactivation():
    client = FakeVTSClient()
    sleep = RecordingSleep()
    sub = VTSEmoteSubscriber(client=client, emote_config=make_config(duration_s=1.0), sleep=sleep)

    await sub.handle(emote_event())
    await asyncio.sleep(0)  # background deactivate task runs to completion

    assert sub._active_expression == {}


async def test_second_activation_before_deactivate_extends_hold_not_double_deactivate():
    """The important corner case: firing the same expression again before its
    hold expires must cancel the stale deactivate, not let it fire early and
    cut the new activation short (or double-fire the deactivate call).
    """
    client = FakeVTSClient()
    sleep = GatedSleep()
    sub = VTSEmoteSubscriber(client=client, emote_config=make_config(duration_s=2.0), sleep=sleep)

    await sub.handle(emote_event())
    await asyncio.sleep(0)
    assert len(sleep.calls) == 1  # first deactivate task now parked in sleep(2.0)

    await sub.handle(emote_event())  # re-fire before the hold expires
    await asyncio.sleep(0)
    await asyncio.sleep(0)  # let the old task's cancellation actually land

    assert len(sleep.calls) == 2  # a fresh hold was scheduled
    active_flags = [c[1]["active"] for c in client.calls]
    assert active_flags == [True, True]  # two activations, no deactivation yet

    sleep.release(0)  # even if the stale sleep were still alive, this must be a no-op
    await asyncio.sleep(0)
    assert all(c[1]["active"] is not False for c in client.calls)

    sleep.release(1)  # release the current hold
    await asyncio.sleep(0)

    deactivate_calls = [c for c in client.calls if c[1]["active"] is False]
    assert len(deactivate_calls) == 1


BASELINE_POSITION = {"positionX": -0.03, "positionY": -0.1, "rotation": 360.0, "size": -83.0}


def fly_config(**overrides) -> FlyConfig:
    defaults = {
        "hotkey": "fly.exp3.json",
        "move_duration_s": 2.0,
        "land_duration_s": 1.5,
    }
    defaults.update(overrides)
    return FlyConfig(**defaults)


def fly_event(*, flying: bool, target_x: float | None, trigger: str = "arousal_high") -> Event:
    return Event(
        kind=Kind.STATE_FLY,
        payload={"flying": flying, "trigger": trigger, "target_x": target_x},
    )


def make_fly_client(**responses) -> FakeVTSClient:
    return FakeVTSClient(responses={"CurrentModelRequest": {"modelPosition": BASELINE_POSITION}})


async def test_fly_ignores_non_fly_events():
    client = make_fly_client()
    sub = VTSFlySubscriber(client=client, fly_config=fly_config())

    await sub.handle(Event(kind=Kind.DIRECTOR_TAG, payload={}))

    assert client.calls == []


async def test_fly_on_moves_to_target_x_and_activates_expression():
    client = make_fly_client()
    sub = VTSFlySubscriber(client=client, fly_config=fly_config())

    await sub.handle(fly_event(flying=True, target_x=0.35))

    move_calls = [c for c in client.calls if c[0] == "MoveModelRequest"]
    assert move_calls[0][1] == {
        "timeInSeconds": 2.0,
        "valuesAreRelativeToModel": False,
        "positionX": 0.35,
        "positionY": BASELINE_POSITION["positionY"],
        "rotation": BASELINE_POSITION["rotation"],
        "size": BASELINE_POSITION["size"],
    }
    expr_calls = [c for c in client.calls if c[0] == "ExpressionActivationRequest"]
    assert expr_calls[0] == (
        "ExpressionActivationRequest",
        {"expressionFile": "fly.exp3.json", "active": True, "fadeTime": 0.25},
    )


async def test_fly_off_returns_to_baseline_x_and_deactivates_expression():
    client = make_fly_client()
    sub = VTSFlySubscriber(client=client, fly_config=fly_config())
    await sub.handle(fly_event(flying=True, target_x=0.35))  # establish baseline + launch

    await sub.handle(fly_event(flying=False, target_x=None, trigger="arousal_low"))

    move_calls = [c for c in client.calls if c[0] == "MoveModelRequest"]
    assert move_calls[-1][1] == {
        "timeInSeconds": 1.5,
        "valuesAreRelativeToModel": False,
        "positionX": BASELINE_POSITION["positionX"],
        "positionY": BASELINE_POSITION["positionY"],
        "rotation": BASELINE_POSITION["rotation"],
        "size": BASELINE_POSITION["size"],
    }
    expr_calls = [c for c in client.calls if c[0] == "ExpressionActivationRequest"]
    assert expr_calls[-1][1]["active"] is False


async def test_fly_baseline_is_looked_up_only_once():
    client = make_fly_client()
    sub = VTSFlySubscriber(client=client, fly_config=fly_config())

    await sub.handle(fly_event(flying=True, target_x=0.1))
    await sub.handle(fly_event(flying=True, target_x=0.2, trigger="reposition"))

    baseline_calls = [c for c in client.calls if c[0] == "CurrentModelRequest"]
    assert len(baseline_calls) == 1


async def test_fly_move_failure_publishes_error_without_raising():
    client = FakeVTSClient(
        responses={"CurrentModelRequest": {"modelPosition": BASELINE_POSITION}},
        fail_message_types={"MoveModelRequest"},
    )
    events: list[Event] = []
    sub = VTSFlySubscriber(client=client, fly_config=fly_config(), publish=events.append)

    await sub.handle(fly_event(flying=True, target_x=0.35))

    error = next(e for e in events if e.kind == Kind.ERROR)
    assert error.payload["component"] == "vts"


async def test_fly_baseline_lookup_failure_publishes_error_and_skips_move():
    client = FakeVTSClient(fail_message_types={"CurrentModelRequest"})
    events: list[Event] = []
    sub = VTSFlySubscriber(client=client, fly_config=fly_config(), publish=events.append)

    await sub.handle(fly_event(flying=True, target_x=0.35))

    assert not any(c[0] == "MoveModelRequest" for c in client.calls)
    error = next(e for e in events if e.kind == Kind.ERROR)
    assert error.payload["component"] == "vts"


def inject_calls(client: FakeVTSClient) -> list[dict]:
    return [data for (mtype, data) in client.calls if mtype == "InjectParameterDataRequest"]


async def test_motion_player_empty_envelope_is_a_noop():
    client = FakeVTSClient()
    player = VTSMotionPlayer(client=client, parameter_name="ChaoHeadBob", sleep=RecordingSleep())

    await player.play([], fps=30.0, cancel=asyncio.Event())

    assert client.calls == []


async def test_motion_player_noop_when_already_cancelled():
    client = FakeVTSClient()
    player = VTSMotionPlayer(client=client, parameter_name="ChaoHeadBob", sleep=RecordingSleep())
    cancel = asyncio.Event()
    cancel.set()

    await player.play([1.0, 2.0], fps=30.0, cancel=cancel)

    assert client.calls == []


async def test_motion_player_injects_each_frame_then_ramps_to_zero():
    client = FakeVTSClient()
    clock = FakeClock()
    sleep = ClockAdvancingSleep(clock)  # zero-cost injections -> exact frame_dt spacing
    player = VTSMotionPlayer(client=client, parameter_name="ChaoHeadBob", sleep=sleep, clock=clock)

    await player.play([5.0, 10.0], fps=30.0, cancel=asyncio.Event())

    values = [c["parameterValues"][0]["value"] for c in inject_calls(client)]
    assert values == [5.0, 10.0, 0.0]  # two frames, then the explicit ramp-to-rest
    assert inject_calls(client)[0] == {
        "faceFound": True,
        "mode": "set",
        "parameterValues": [{"id": "ChaoHeadBob", "value": 5.0, "weight": 1}],
    }
    assert sleep.durations == [1.0 / 30.0, 1.0 / 30.0]  # no sleep after the final ramp


async def test_motion_player_paces_to_a_deadline_not_a_fixed_period_on_top_of_injection_cost():
    """The bug this pins: sleeping a full frame_dt *after* each injection
    call (rather than only the remainder of that frame's deadline) makes a
    clip run ~1.5x longer than the audio it's supposed to track, once a
    real ~17ms round trip (session 10's measurement) is added on top of a
    ~33ms frame period. Caught by advisor review before this ever ran live.
    """
    clock = FakeClock()
    sleep = ClockAdvancingSleep(clock)
    client = InjectionCostVTSClient(clock, cost=0.017)
    player = VTSMotionPlayer(client=client, parameter_name="ChaoHeadBob", sleep=sleep, clock=clock)
    fps = 30.0
    frame_dt = 1.0 / fps
    envelope = [1.0, 2.0, 3.0, 4.0]

    await player.play(envelope, fps=fps, cancel=asyncio.Event())

    # Fixed-period-after-injection code always sleeps exactly frame_dt;
    # deadline-paced code must sleep less to absorb the injection cost.
    assert all(d < frame_dt for d in sleep.durations)
    # Final clock reading = the paced loop landing exactly on
    # len(envelope) * frame_dt, plus the one trailing ramp-to-rest
    # injection's cost (that call isn't paced -- it's the deliberate,
    # unconditional "return to neutral" after the clip ends). The old
    # fixed-sleep-after-injection code would instead land at roughly
    # len(envelope) * (frame_dt + cost) -- ~1.5x this value at these
    # numbers.
    assert clock.now == pytest.approx(len(envelope) * frame_dt + 0.017, abs=1e-6)


async def test_motion_player_publishes_sampled_vts_param_events():
    client = FakeVTSClient()
    events: list[Event] = []
    player = VTSMotionPlayer(
        client=client,
        parameter_name="ChaoHeadBob",
        publish=events.append,
        publish_every_n_frames=6,
        sleep=RecordingSleep(),
    )
    envelope = [float(i) for i in range(7)]  # frames 0..6

    await player.play(envelope, fps=30.0, cancel=asyncio.Event())

    param_events = [e for e in events if e.kind == Kind.VTS_PARAM]
    assert [e.payload["value"] for e in param_events] == [0.0, 6.0]  # frame 0 and frame 6 only


async def test_motion_player_stops_early_when_cancelled_mid_clip():
    client = FakeVTSClient()
    cancel = asyncio.Event()

    class CancelAfterFirstSleep:
        def __init__(self):
            self.calls = 0

        async def __call__(self, duration: float) -> None:
            self.calls += 1
            cancel.set()

    player = VTSMotionPlayer(
        client=client, parameter_name="ChaoHeadBob", sleep=CancelAfterFirstSleep()
    )

    await player.play([1.0, 2.0, 3.0], fps=30.0, cancel=cancel)

    values = [c["parameterValues"][0]["value"] for c in inject_calls(client)]
    assert values == [1.0, 0.0]  # first frame injected, then cancelled before the second


async def test_motion_player_injection_failure_stops_clip_and_publishes_error():
    client = FakeVTSClient(fail_message_types={"InjectParameterDataRequest"})
    events: list[Event] = []
    player = VTSMotionPlayer(
        client=client, parameter_name="ChaoHeadBob", publish=events.append, sleep=RecordingSleep()
    )

    await player.play([1.0, 2.0, 3.0], fps=30.0, cancel=asyncio.Event())

    # First injection fails -> loop breaks; the trailing ramp-to-zero also fails.
    assert len(inject_calls(client)) == 2
    errors = [e for e in events if e.kind == Kind.ERROR]
    assert len(errors) == 2
    assert all(e.payload["component"] == "vts" for e in errors)


class ClockAdvancingCancelAfterN:
    """Combines ClockAdvancingSleep's clock-advance with a bounded call
    count -- lets an idle-drift test run exactly N frames of real (faked)
    time progression, so IdleDrift actually moves through phase
    transitions, then stops. Cancel is checked at the top of the while
    loop, so setting it inside the Nth sleep call lets that frame finish
    and stops before the (N+1)th.
    """

    def __init__(self, clock: FakeClock, cancel: asyncio.Event, n: int):
        self.clock = clock
        self.cancel = cancel
        self.n = n
        self.calls = 0

    async def __call__(self, duration: float) -> None:
        self.calls += 1
        self.clock.advance(duration)
        if self.calls >= self.n:
            self.cancel.set()


def idle_config(**overrides) -> IdleDriftConfig:
    defaults = {
        "x_parameter_name": "ChaoHeadTurn",
        "z_parameter_name": "ChaoHeadTilt",
        "fps": 10.0,
        "x_amplitude": 22.0,
        "z_amplitude": 12.0,
        "min_move_distance": 8.0,
        "ease_min_s": 0.5,
        "ease_max_s": 0.5,
        "hold_min_s": 0.5,
        "hold_max_s": 0.5,
    }
    defaults.update(overrides)
    return IdleDriftConfig(**defaults)


def make_idle_drift(clock: FakeClock, seed: int) -> IdleDrift:
    return IdleDrift(config=idle_config(), clock=clock, rng=random.Random(seed))


async def test_idle_drift_noop_when_already_cancelled():
    client = FakeVTSClient()
    player = VTSIdleDriftPlayer(
        client=client, x_parameter_name="ChaoHeadTurn", z_parameter_name="ChaoHeadTilt"
    )
    cancel = asyncio.Event()
    cancel.set()

    await player.run(make_idle_drift(FakeClock(), seed=1), cancel=cancel)

    assert client.calls == []


async def test_idle_drift_injects_both_axes_in_one_call_per_frame():
    client = FakeVTSClient()
    clock = FakeClock()
    cancel = asyncio.Event()
    player = VTSIdleDriftPlayer(
        client=client,
        x_parameter_name="ChaoHeadTurn",
        z_parameter_name="ChaoHeadTilt",
        clock=clock,
        sleep=ClockAdvancingCancelAfterN(clock, cancel, n=3),
    )

    await player.run(make_idle_drift(clock, seed=1), cancel=cancel)

    inject_calls_ = [c for c in client.calls if c[0] == "InjectParameterDataRequest"]
    assert len(inject_calls_) == 4  # 3 frames + the trailing ramp-to-zero
    for _mtype, data in inject_calls_:
        ids = {pv["id"] for pv in data["parameterValues"]}
        assert ids == {"ChaoHeadTurn", "ChaoHeadTilt"}  # both axes, one call


async def test_idle_drift_uses_face_found_true():
    """advisor flag: two concurrent writers on the same connection
    disagreeing on faceFound (VTSMotionPlayer uses True; vts_probe.py's
    throwaway diagnostic uses False) is a real last-writer-wins hazard.
    Idle drift must match VTSMotionPlayer's True.
    """
    client = FakeVTSClient()
    clock = FakeClock()
    cancel = asyncio.Event()
    player = VTSIdleDriftPlayer(
        client=client,
        x_parameter_name="ChaoHeadTurn",
        z_parameter_name="ChaoHeadTilt",
        clock=clock,
        sleep=ClockAdvancingCancelAfterN(clock, cancel, n=1),
    )

    await player.run(make_idle_drift(clock, seed=1), cancel=cancel)

    inject_calls_ = [c for c in client.calls if c[0] == "InjectParameterDataRequest"]
    assert all(data["faceFound"] is True for _mtype, data in inject_calls_)


async def test_idle_drift_ramps_both_axes_to_zero_on_exit():
    client = FakeVTSClient()
    clock = FakeClock()
    cancel = asyncio.Event()
    player = VTSIdleDriftPlayer(
        client=client,
        x_parameter_name="ChaoHeadTurn",
        z_parameter_name="ChaoHeadTilt",
        clock=clock,
        sleep=ClockAdvancingCancelAfterN(clock, cancel, n=2),
    )

    await player.run(make_idle_drift(clock, seed=1), cancel=cancel)

    inject_calls_ = [c for c in client.calls if c[0] == "InjectParameterDataRequest"]
    last_values = {pv["id"]: pv["value"] for pv in inject_calls_[-1][1]["parameterValues"]}
    assert last_values == {"ChaoHeadTurn": 0.0, "ChaoHeadTilt": 0.0}


async def test_idle_drift_publishes_sampled_vts_param_events_for_both_axes():
    client = FakeVTSClient()
    clock = FakeClock()
    cancel = asyncio.Event()
    events: list[Event] = []
    player = VTSIdleDriftPlayer(
        client=client,
        x_parameter_name="ChaoHeadTurn",
        z_parameter_name="ChaoHeadTilt",
        publish=events.append,
        publish_every_n_frames=1,
        clock=clock,
        sleep=ClockAdvancingCancelAfterN(clock, cancel, n=2),
    )

    await player.run(make_idle_drift(clock, seed=1), cancel=cancel)

    param_events = [e for e in events if e.kind == Kind.VTS_PARAM]
    names = {e.payload["name"] for e in param_events}
    assert names == {"ChaoHeadTurn", "ChaoHeadTilt"}


async def test_idle_drift_injection_failure_stops_loop_and_publishes_error():
    client = FakeVTSClient(fail_message_types={"InjectParameterDataRequest"})
    clock = FakeClock()
    cancel = asyncio.Event()
    events: list[Event] = []
    player = VTSIdleDriftPlayer(
        client=client,
        x_parameter_name="ChaoHeadTurn",
        z_parameter_name="ChaoHeadTilt",
        publish=events.append,
        clock=clock,
        sleep=ClockAdvancingSleep(clock),
    )

    await player.run(make_idle_drift(clock, seed=1), cancel=cancel)

    # First injection fails -> loop breaks; the trailing ramp-to-zero also fails.
    inject_calls_ = [c for c in client.calls if c[0] == "InjectParameterDataRequest"]
    assert len(inject_calls_) == 2
    errors = [e for e in events if e.kind == Kind.ERROR]
    assert len(errors) == 2
    assert all(e.payload["component"] == "vts" for e in errors)


async def test_idle_drift_is_deterministic_given_a_seeded_rng():
    async def run_and_collect(seed: int) -> list[dict]:
        client = FakeVTSClient()
        clock = FakeClock()
        cancel = asyncio.Event()
        player = VTSIdleDriftPlayer(
            client=client,
            x_parameter_name="ChaoHeadTurn",
            z_parameter_name="ChaoHeadTilt",
            clock=clock,
            sleep=ClockAdvancingCancelAfterN(clock, cancel, n=10),
        )
        await player.run(make_idle_drift(clock, seed=seed), cancel=cancel)
        return [
            {pv["id"]: pv["value"] for pv in data["parameterValues"]}
            for mtype, data in client.calls
            if mtype == "InjectParameterDataRequest"
        ]

    values1 = await run_and_collect(99)
    values2 = await run_and_collect(99)
    values3 = await run_and_collect(1)

    assert values1 == values2
    assert values1 != values3
