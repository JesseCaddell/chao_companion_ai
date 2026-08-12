import asyncio

from chao.director.aliveness import FlyConfig
from chao.director.director import EmoteConfig, EmotePool
from chao.events import Event, Kind
from chao.outputs.vts import VTSEmoteSubscriber, VTSFlySubscriber


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
