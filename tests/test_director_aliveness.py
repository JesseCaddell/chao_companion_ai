import random

from chao.director.aliveness import Aliveness, Fly, FlyConfig, load_fly_config
from chao.director.director import EmoteConfig, EmotePool
from chao.events import Event, Kind


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_config(reactions: dict[str, str] | None = None) -> EmoteConfig:
    return EmoteConfig(
        pools={
            "curious": EmotePool(hotkeys=["question.exp3.json"], cooldown_s=3.0, duration_s=2.0),
            "happy": EmotePool(hotkeys=["happy.exp3.json"], cooldown_s=4.0, duration_s=2.5),
        },
        reactions=reactions if reactions is not None else {"anticipation": "curious"},
    )


def make_aliveness(config=None) -> tuple[Aliveness, list[Event]]:
    events: list[Event] = []
    aliveness = Aliveness(
        emote_config=config or make_config(), publish=events.append, rng=random.Random(0)
    )
    return aliveness, events


def test_brain_request_fires_the_configured_reaction_pool():
    aliveness, events = make_aliveness()

    aliveness.handle(Event(kind=Kind.BRAIN_REQUEST, turn_id="t1"))

    assert len(events) == 1
    assert events[0].kind == Kind.DIRECTOR_EMOTE
    assert events[0].payload["pool"] == "curious"
    assert events[0].payload["hotkey_id"] == "question.exp3.json"
    assert events[0].payload["reason"] == "anticipation"


def test_carries_the_triggering_events_turn_id():
    aliveness, events = make_aliveness()

    aliveness.handle(Event(kind=Kind.BRAIN_REQUEST, turn_id="t42"))

    assert events[0].turn_id == "t42"


def test_ignores_other_event_kinds():
    aliveness, events = make_aliveness()

    aliveness.handle(Event(kind=Kind.BRAIN_TOKEN, turn_id="t1", payload={"text": "hi"}))
    aliveness.handle(Event(kind=Kind.BRAIN_COMPLETE, turn_id="t1"))

    assert events == []


def test_falls_back_to_curious_when_reactions_missing_from_config():
    aliveness, events = make_aliveness(config=make_config(reactions={}))

    aliveness.handle(Event(kind=Kind.BRAIN_REQUEST, turn_id="t1"))

    assert events[0].payload["pool"] == "curious"


def test_reactions_can_repoint_anticipation_at_a_different_pool():
    aliveness, events = make_aliveness(config=make_config(reactions={"anticipation": "happy"}))

    aliveness.handle(Event(kind=Kind.BRAIN_REQUEST, turn_id="t1"))

    assert events[0].payload["pool"] == "happy"
    assert events[0].payload["hotkey_id"] == "happy.exp3.json"


def test_missing_pool_in_config_does_not_crash():
    config = EmoteConfig(pools={}, reactions={"anticipation": "curious"})
    aliveness, events = make_aliveness(config=config)

    aliveness.handle(Event(kind=Kind.BRAIN_REQUEST, turn_id="t1"))

    assert events == []


def make_fly_config(**overrides) -> FlyConfig:
    defaults = {
        "on_above": 0.70,
        "off_below": 0.40,
        "min_dwell_s": 5.0,
        "x_min": -0.5,
        "x_max": 0.5,
        "move_duration_s": 2.0,
        "land_duration_s": 1.5,
        "min_reposition_s": 8.0,
    }
    defaults.update(overrides)
    return FlyConfig(**defaults)


def make_fly(config=None, clock=None, rng=None) -> tuple[Fly, list[Event]]:
    events: list[Event] = []
    fly = Fly(
        config=config or make_fly_config(),
        publish=events.append,
        clock=clock or FakeClock(),
        rng=rng or random.Random(0),
    )
    return fly, events


def mood_event(arousal: float, turn_id: str = "t1") -> Event:
    return Event(
        kind=Kind.DIRECTOR_MOOD,
        turn_id=turn_id,
        payload={
            "valence": 0.0,
            "arousal": arousal,
            "baseline_valence": 0.0,
            "baseline_arousal": 0.0,
        },
    )


def tag_event(tag: str, turn_id: str = "t1") -> Event:
    return Event(kind=Kind.DIRECTOR_TAG, turn_id=turn_id, payload={"tag": tag})


# --- load_fly_config() ---


def test_load_fly_config_the_real_file():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "config" / "emotes.yaml"
    config = load_fly_config(path)

    assert config.x_min < config.x_max
    assert config.on_above > config.off_below


def test_load_fly_config_defaults_when_fly_block_missing(tmp_path):
    path = tmp_path / "emotes.yaml"
    path.write_text("pools: {}\n")
    config = load_fly_config(path)
    assert config == FlyConfig()


# --- Fly.handle(): on/off hysteresis ---


def test_launches_when_arousal_crosses_on_above():
    fly, events = make_fly()

    fly.handle(mood_event(0.71))

    assert fly.flying is True
    assert events[0].kind == Kind.STATE_FLY
    assert events[0].payload["flying"] is True
    assert events[0].payload["trigger"] == "arousal_high"
    assert events[0].payload["target_x"] is not None


def test_does_not_launch_below_on_above_threshold():
    fly, events = make_fly()

    fly.handle(mood_event(0.70))

    assert fly.flying is False
    assert events == []


def test_lands_when_arousal_drops_below_off_below_after_min_dwell():
    clock = FakeClock()
    fly, events = make_fly(clock=clock)
    fly.handle(mood_event(0.9))  # launches
    clock.advance(5.0)  # exactly min_dwell_s

    fly.handle(mood_event(0.1))

    assert fly.flying is False
    assert events[-1].payload["flying"] is False
    assert events[-1].payload["trigger"] == "arousal_low"
    assert events[-1].payload["target_x"] is None


def test_does_not_land_before_min_dwell_elapses():
    clock = FakeClock()
    fly, events = make_fly(clock=clock)
    fly.handle(mood_event(0.9))  # launches
    clock.advance(2.0)  # short of min_dwell_s=5.0

    fly.handle(mood_event(0.1))

    assert fly.flying is True
    assert len(events) == 1  # only the launch, the landing attempt was suppressed


def test_ignores_mood_events_missing_arousal():
    fly, events = make_fly()

    fly.handle(Event(kind=Kind.DIRECTOR_MOOD, payload={"valence": 0.5}))

    assert events == []


def test_ignores_non_mood_non_tag_events():
    fly, events = make_fly()

    fly.handle(Event(kind=Kind.BRAIN_TOKEN, payload={"text": "hi"}))

    assert events == []


# --- Fly.handle(): sad hard-landing rule ---


def test_sad_tag_lands_immediately_bypassing_min_dwell():
    clock = FakeClock()
    fly, events = make_fly(clock=clock)
    fly.handle(mood_event(0.9))  # launches
    clock.advance(0.1)  # nowhere near min_dwell_s=5.0

    fly.handle(tag_event("sad"))

    assert fly.flying is False
    assert events[-1].payload["trigger"] == "sad"
    assert events[-1].payload["target_x"] is None


def test_sad_tag_while_not_flying_does_nothing():
    fly, events = make_fly()

    fly.handle(tag_event("sad"))

    assert events == []


def test_non_sad_tags_do_not_land():
    fly, events = make_fly()
    fly.handle(mood_event(0.9))  # launches

    fly.handle(tag_event("happy"))

    assert fly.flying is True
    assert len(events) == 1


# --- Fly.handle(): horizontal repositioning while already flying ---


def test_repositions_after_min_reposition_s_while_still_flying():
    clock = FakeClock()
    fly, events = make_fly(clock=clock, config=make_fly_config(min_reposition_s=8.0))
    fly.handle(mood_event(0.9))  # launches, picks first target_x
    clock.advance(8.0)

    fly.handle(mood_event(0.8))  # still above off_below, eligible to reposition

    assert len(events) == 2
    assert events[-1].payload["trigger"] == "reposition"
    assert events[-1].payload["target_x"] is not None
    # Not asserting != first_x: rng could coincidentally repeat a value: pinned
    # rng seed makes this deterministic in practice, but the behavioral
    # contract is "reposition happened," not "value changed."
    assert fly.target_x == events[-1].payload["target_x"]


def test_does_not_reposition_before_min_reposition_s_elapses():
    clock = FakeClock()
    fly, events = make_fly(clock=clock, config=make_fly_config(min_reposition_s=8.0))
    fly.handle(mood_event(0.9))  # launches
    clock.advance(3.0)  # short of min_reposition_s=8.0

    fly.handle(mood_event(0.8))

    assert len(events) == 1  # only the launch


def test_target_x_stays_within_configured_bounds():
    fly, _ = make_fly(config=make_fly_config(x_min=-0.3, x_max=0.3), rng=random.Random(42))

    fly.handle(mood_event(0.9))

    assert -0.3 <= fly.target_x <= 0.3


def test_fly_carries_the_triggering_events_turn_id():
    fly, events = make_fly()

    fly.handle(mood_event(0.9, turn_id="t42"))

    assert events[0].turn_id == "t42"


def test_does_not_share_cooldown_with_tag_triggered_curious_emotes():
    """Aliveness has no cooldown/state of its own -- it fires every single
    brain.request. This is deliberate (see aliveness.py's docstring): the
    alternative, sharing Director's cooldown state for the `curious` pool,
    would let the anticipation nudge silently suppress a [curious]/[thinking]
    tag's own emote on essentially every turn, since the nudge fires right
    as the cooldown window opens.
    """
    aliveness, events = make_aliveness()

    aliveness.handle(Event(kind=Kind.BRAIN_REQUEST, turn_id="t1"))
    aliveness.handle(Event(kind=Kind.BRAIN_REQUEST, turn_id="t2"))

    assert len([e for e in events if e.kind == Kind.DIRECTOR_EMOTE]) == 2
