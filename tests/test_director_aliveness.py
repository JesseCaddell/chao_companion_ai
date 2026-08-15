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
            "surprise": EmotePool(hotkeys=["surprise.exp3.json"], cooldown_s=6.0, duration_s=1.5),
        },
        reactions=reactions
        if reactions is not None
        else {"anticipation": "curious", "chat_spike": "surprise"},
    )


def make_aliveness(config=None, clock=None) -> tuple[Aliveness, list[Event]]:
    events: list[Event] = []
    kwargs = {
        "emote_config": config or make_config(),
        "publish": events.append,
        "rng": random.Random(0),
    }
    if clock is not None:
        kwargs["clock"] = clock
    aliveness = Aliveness(**kwargs)
    return aliveness, events


def spike_chat(turn_id: str = "t1") -> Event:
    return Event(
        kind=Kind.INPUT_CHAT,
        turn_id=turn_id,
        payload={"login": "someone", "text": "hi", "priority": 4},
    )


def background_chat(turn_id: str = "t1") -> Event:
    return Event(
        kind=Kind.INPUT_CHAT,
        turn_id=turn_id,
        payload={"login": "someone", "text": "hi", "priority": 5},
    )


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


def test_tier_four_chat_fires_the_chat_spike_reaction():
    aliveness, events = make_aliveness()

    aliveness.handle(spike_chat())

    assert len(events) == 1
    assert events[0].kind == Kind.DIRECTOR_EMOTE
    assert events[0].payload["pool"] == "surprise"
    assert events[0].payload["reason"] == "chat_spike"


def test_background_chat_below_spike_tier_does_not_fire():
    aliveness, events = make_aliveness()

    aliveness.handle(background_chat())

    assert events == []


def test_chat_spike_reaction_respects_its_pool_cooldown():
    clock = FakeClock()
    aliveness, events = make_aliveness(clock=clock)

    aliveness.handle(spike_chat())
    aliveness.handle(spike_chat())  # immediately after -- within surprise's 6s cooldown

    assert len(events) == 1


def test_chat_spike_reaction_fires_again_after_cooldown_elapses():
    clock = FakeClock()
    aliveness, events = make_aliveness(clock=clock)

    aliveness.handle(spike_chat())
    clock.advance(6.5)  # past surprise's 6s cooldown
    aliveness.handle(spike_chat())

    assert len(events) == 2


def test_anticipation_and_chat_spike_cooldowns_are_tracked_independently():
    clock = FakeClock()
    aliveness, events = make_aliveness(clock=clock)

    aliveness.handle(Event(kind=Kind.BRAIN_REQUEST, turn_id="t1"))
    aliveness.handle(spike_chat())  # different reaction key -- must not be blocked

    assert len(events) == 2


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
        # Not overridden here on purpose: launch_probability defaults to
        # 1.0 and boredom_probability to 0.0 on FlyConfig itself, which
        # keeps every pre-existing deterministic test in this file passing
        # unchanged. Tests of the probabilistic paths override explicitly.
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


def mood_event(arousal: float, turn_id: str = "t1", source: str = "tag") -> Event:
    return Event(
        kind=Kind.DIRECTOR_MOOD,
        turn_id=turn_id,
        payload={
            "valence": 0.0,
            "arousal": arousal,
            "baseline_valence": 0.0,
            "baseline_arousal": 0.0,
            "source": source,
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


# --- Fly.handle(): directed fly tags (session 10 part 16) ---


def test_directed_fly_tag_launches_from_grounded():
    fly, events = make_fly()

    fly.handle(tag_event("fly:left"))

    assert fly.flying is True
    assert events[-1].payload["trigger"] == "directed"
    assert events[-1].payload["target_x"] == fly.config.x_min


def test_directed_fly_tag_right_targets_x_max():
    fly, events = make_fly()

    fly.handle(tag_event("fly:right"))

    assert events[-1].payload["target_x"] == fly.config.x_max


def test_directed_fly_tag_center_targets_midpoint():
    fly, events = make_fly(config=make_fly_config(x_min=-0.6, x_max=0.6))

    fly.handle(tag_event("fly:center"))

    assert events[-1].payload["target_x"] == 0.0


def test_directed_fly_tag_retargets_immediately_bypassing_reposition_floor():
    clock = FakeClock()
    fly, events = make_fly(clock=clock, config=make_fly_config(min_reposition_s=8.0))
    fly.handle(mood_event(0.9))  # launches, random target
    clock.advance(0.1)  # nowhere near the 8s floor

    fly.handle(tag_event("fly:right"))

    assert fly.target_x == fly.config.x_max
    assert events[-1].payload["trigger"] == "directed"


def test_directed_target_latches_against_ambient_reposition():
    """A chao told to go left must stay left -- an ambient, mood-tick-driven
    reposition check past the floor must not randomly re-scatter a directed
    target. See aliveness.py's Fly docstring, session 10 part 16.
    """
    clock = FakeClock()
    fly, events = make_fly(clock=clock, config=make_fly_config(min_reposition_s=8.0))
    fly.handle(tag_event("fly:left"))  # directed launch
    clock.advance(8.0)  # past the reposition floor

    fly.handle(mood_event(0.8, source="tag"))  # ambient activity, still flying

    assert fly.target_x == fly.config.x_min
    assert events[-1].payload["trigger"] != "reposition"


def test_directed_latch_clears_on_landing():
    """After landing, the next autonomous launch must be free to roam again
    -- a directed target shouldn't pin the chao's position forever.
    """
    clock = FakeClock()
    fly, events = make_fly(
        clock=clock, config=make_fly_config(min_dwell_s=5.0, min_reposition_s=8.0)
    )
    fly.handle(tag_event("fly:left"))  # directed launch
    clock.advance(5.0)
    fly.handle(mood_event(0.1))  # lands (arousal_low, past min_dwell_s)
    assert fly.flying is False

    fly.handle(mood_event(0.9))  # relaunches (arousal_high)
    clock.advance(8.0)
    fly.handle(mood_event(0.8, source="tag"))  # ambient activity past the floor

    assert events[-1].payload["trigger"] == "reposition"


def test_unknown_fly_direction_is_ignored():
    fly, events = make_fly()

    fly.handle(tag_event("fly:up"))

    assert fly.flying is False
    assert events == []


# --- Fly.handle(): tick-sourced director.mood events (session 9) ---


def test_tick_sourced_mood_event_can_land_during_a_quiet_stretch():
    """The whole point of Mood.tick(): a flying chao must be able to land
    from a background poll, not just from a fresh tag.
    """
    clock = FakeClock()
    fly, events = make_fly(clock=clock)
    fly.handle(mood_event(0.9, source="tag"))  # launches
    clock.advance(5.0)  # past min_dwell_s

    fly.handle(mood_event(0.1, source="tick"))

    assert fly.flying is False
    assert events[-1].payload["trigger"] == "arousal_low"


def test_tick_sourced_mood_event_does_not_trigger_reposition():
    """Repositioning must stay tied to real activity (tag-sourced events) --
    a tick triggering it would make a flying chao pace to a new spot every
    min_reposition_s with zero activity, the exact "pacing, not aliveness"
    failure §10.2 warns against.
    """
    clock = FakeClock()
    fly, events = make_fly(clock=clock, config=make_fly_config(min_reposition_s=8.0))
    fly.handle(mood_event(0.9, source="tag"))  # launches
    clock.advance(8.0)

    fly.handle(mood_event(0.8, source="tick"))  # eligible by timing, wrong source

    assert len(events) == 1  # only the launch -- no reposition fired


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


# --- Fly.handle(): session 11 probabilistic launch (high arousal) ---


def test_launch_probability_below_one_can_prevent_launch_on_crossing():
    # seed 0's first draw is ~0.844, which fails a 0.5 probability check.
    fly, events = make_fly(config=make_fly_config(launch_probability=0.5), rng=random.Random(0))

    fly.handle(mood_event(0.9))

    assert fly.flying is False
    assert events == []


def test_launch_probability_below_one_can_still_launch():
    # seed 1's first draw is ~0.134, which passes a 0.5 probability check.
    fly, events = make_fly(config=make_fly_config(launch_probability=0.5), rng=random.Random(1))

    fly.handle(mood_event(0.9))

    assert fly.flying is True
    assert events[-1].payload["trigger"] == "arousal_high"


def test_failed_launch_roll_is_edge_triggered_not_retried_while_sustained():
    """A failed roll must not be retried on every subsequent mood event
    while arousal stays above on_above -- otherwise launch_probability
    barely reduces frequency at all, since arousal typically stays above
    threshold for several ticks before decaying back down. Seed 0's first
    three draws are ~0.844/0.758/0.421 -- the third would pass a 0.5 check
    if it were ever consumed, so staying grounded here proves no re-roll
    happened, not just that this particular draw failed too.
    """
    fly, events = make_fly(config=make_fly_config(launch_probability=0.5), rng=random.Random(0))

    fly.handle(mood_event(0.9))
    fly.handle(mood_event(0.85))
    fly.handle(mood_event(0.95))

    assert fly.flying is False
    assert events == []


def test_launch_roll_re_arms_after_dropping_back_below_on_above():
    # seed 10: first draw ~0.571 fails a 0.5 check, second ~0.429 passes --
    # only reachable if dropping below on_above genuinely re-arms the roll.
    fly, events = make_fly(config=make_fly_config(launch_probability=0.5), rng=random.Random(10))

    fly.handle(mood_event(0.9))  # crosses, rolls, fails
    assert fly.flying is False

    fly.handle(mood_event(0.5))  # drops back below on_above, re-arms
    fly.handle(mood_event(0.9))  # crosses again, rolls, succeeds

    assert fly.flying is True
    assert events[-1].payload["trigger"] == "arousal_high"


# --- Fly.handle(): session 11 boredom launch path ---


def test_boredom_launch_requires_sustained_dwell_before_eligible():
    clock = FakeClock()
    fly, events = make_fly(
        clock=clock,
        config=make_fly_config(boredom_below=0.25, boredom_dwell_s=10.0, boredom_probability=1.0),
    )

    fly.handle(mood_event(0.1))  # arousal below boredom_below -- starts the dwell clock
    clock.advance(5.0)  # short of boredom_dwell_s=10.0
    fly.handle(mood_event(0.1))

    assert fly.flying is False
    assert events == []


def test_boredom_launches_once_dwell_elapses():
    clock = FakeClock()
    fly, events = make_fly(
        clock=clock,
        config=make_fly_config(boredom_below=0.25, boredom_dwell_s=10.0, boredom_probability=1.0),
    )

    fly.handle(mood_event(0.1))
    clock.advance(11.0)  # past boredom_dwell_s=10.0
    fly.handle(mood_event(0.1))

    assert fly.flying is True
    assert events[-1].payload["trigger"] == "boredom"
    assert events[-1].payload["target_x"] is not None


def test_boredom_dwell_resets_if_arousal_rises_above_threshold():
    clock = FakeClock()
    fly, events = make_fly(
        clock=clock,
        config=make_fly_config(boredom_below=0.25, boredom_dwell_s=10.0, boredom_probability=1.0),
    )

    fly.handle(mood_event(0.1))  # starts the dwell clock at t=0
    clock.advance(5.0)
    fly.handle(mood_event(0.5))  # rises above boredom_below -- interrupts the dwell
    clock.advance(6.0)  # 11s since t=0, but only 6s since the reset
    fly.handle(mood_event(0.1))  # restarts the dwell clock at t=11

    assert fly.flying is False
    assert events == []


def test_boredom_probability_can_prevent_launch_once_eligible():
    # dwell=0 -> eligible on the very first below-threshold event. Seed 0's
    # first draw (~0.844) fails a 0.5 probability check.
    fly, events = make_fly(
        config=make_fly_config(boredom_below=0.25, boredom_dwell_s=0.0, boredom_probability=0.5),
        rng=random.Random(0),
    )

    fly.handle(mood_event(0.1))

    assert fly.flying is False
    assert events == []


def test_boredom_probability_can_launch_once_eligible():
    # seed 1's first draw (~0.134) passes a 0.5 probability check.
    fly, events = make_fly(
        config=make_fly_config(boredom_below=0.25, boredom_dwell_s=0.0, boredom_probability=0.5),
        rng=random.Random(1),
    )

    fly.handle(mood_event(0.1))

    assert fly.flying is True
    assert events[-1].payload["trigger"] == "boredom"


def test_boredom_probability_zero_never_launches_by_default():
    fly, events = make_fly(
        config=make_fly_config(boredom_below=0.25, boredom_dwell_s=0.0)  # boredom_probability=0.0
    )

    for _ in range(20):
        fly.handle(mood_event(0.1))

    assert fly.flying is False
    assert events == []


def test_boredom_path_does_not_apply_while_already_flying():
    clock = FakeClock()
    fly, events = make_fly(
        clock=clock,
        config=make_fly_config(boredom_below=0.25, boredom_dwell_s=0.0, boredom_probability=1.0),
    )
    fly.handle(mood_event(0.9))  # launches via high arousal
    clock.advance(5.0)  # past min_dwell_s
    events.clear()

    fly.handle(mood_event(0.1))  # low arousal while flying -- lands, doesn't re-launch as bored

    assert fly.flying is False
    assert events[-1].payload["trigger"] == "arousal_low"


def test_load_fly_config_parses_launch_and_boredom_fields(tmp_path):
    path = tmp_path / "emotes.yaml"
    path.write_text(
        "fly:\n"
        "  launch_probability: 0.4\n"
        "  boredom_below: 0.2\n"
        "  boredom_dwell_s: 30.0\n"
        "  boredom_probability: 0.1\n"
    )

    config = load_fly_config(path)

    assert config.launch_probability == 0.4
    assert config.boredom_below == 0.2
    assert config.boredom_dwell_s == 30.0
    assert config.boredom_probability == 0.1
