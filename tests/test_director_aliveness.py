import random

from chao.director.aliveness import Aliveness
from chao.director.director import EmoteConfig, EmotePool
from chao.events import Event, Kind


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
