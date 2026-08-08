import math

from chao.director.mood import Mood, MoodConfig, decay, load_mood_config
from chao.events import Event, Kind


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_config(**overrides) -> MoodConfig:
    defaults = {"baseline_valence": 0.0, "baseline_arousal": 0.0, "half_life_s": 10.0}
    defaults.update(overrides)
    return MoodConfig(**defaults)


def make_mood(config=None, clock=None) -> tuple[Mood, list[Event]]:
    events: list[Event] = []
    mood = Mood(config=config or make_config(), publish=events.append, clock=clock or FakeClock())
    return mood, events


def tag_event(tag: str, turn_id: str = "t1") -> Event:
    return Event(kind=Kind.DIRECTOR_TAG, turn_id=turn_id, payload={"tag": tag})


# --- decay(): pure function ---


def test_decay_elapsed_zero_is_identity():
    assert decay(0.8, 0.0, elapsed=0.0, half_life_s=10.0) == 0.8


def test_decay_at_one_half_life_lands_exactly_halfway():
    result = decay(1.0, 0.0, elapsed=10.0, half_life_s=10.0)
    assert math.isclose(result, 0.5, abs_tol=1e-9)


def test_decay_at_two_half_lives_lands_at_a_quarter():
    result = decay(1.0, 0.0, elapsed=20.0, half_life_s=10.0)
    assert math.isclose(result, 0.25, abs_tol=1e-9)


def test_decay_moves_toward_a_nonzero_baseline():
    result = decay(1.0, 0.2, elapsed=10.0, half_life_s=10.0)
    assert math.isclose(result, 0.6, abs_tol=1e-9)  # 0.2 + (1.0 - 0.2) * 0.5


def test_decay_is_monotone_toward_baseline():
    a = decay(1.0, 0.0, elapsed=5.0, half_life_s=10.0)
    b = decay(1.0, 0.0, elapsed=15.0, half_life_s=10.0)
    assert 0.0 < b < a < 1.0


def test_decay_zero_half_life_returns_current_unchanged():
    assert decay(0.8, 0.0, elapsed=5.0, half_life_s=0.0) == 0.8


# --- load_mood_config() ---


def test_load_mood_config_the_real_file():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "config" / "emotes.yaml"
    config = load_mood_config(path)

    assert -1.0 <= config.baseline_valence <= 1.0
    assert -1.0 <= config.baseline_arousal <= 1.0
    assert config.half_life_s > 0


def test_load_mood_config_defaults_when_mood_block_missing(tmp_path):
    path = tmp_path / "emotes.yaml"
    path.write_text("pools: {}\n")
    config = load_mood_config(path)
    assert config == MoodConfig()


# --- Mood.handle() ---


def test_known_tag_nudges_valence_and_arousal_from_baseline():
    mood, events = make_mood(config=make_config(baseline_valence=0.0, baseline_arousal=0.0))

    mood.handle(tag_event("happy"))

    assert mood.valence == 0.7
    assert mood.arousal == 0.6
    assert events[0].kind == Kind.DIRECTOR_MOOD
    assert events[0].payload == {
        "valence": 0.7,
        "arousal": 0.6,
        "baseline_valence": 0.0,
        "baseline_arousal": 0.0,
    }


def test_happy_and_affection_use_different_deltas_despite_sharing_a_pool():
    """director.py's _TAG_TO_POOL maps both to the "happy" emote pool, but
    §6.2 gives them different valence/arousal deltas -- mood.py must key on
    the tag, not the pool, or affection would silently get happy's numbers.
    """
    mood_happy, _ = make_mood()
    mood_happy.handle(tag_event("happy"))

    mood_affection, _ = make_mood()
    mood_affection.handle(tag_event("affection"))

    assert (mood_happy.valence, mood_happy.arousal) != (
        mood_affection.valence,
        mood_affection.arousal,
    )
    assert mood_affection.valence == 0.9
    assert mood_affection.arousal == 0.3


def test_thinking_is_transient_only_and_does_not_nudge_mood():
    mood, events = make_mood()

    mood.handle(tag_event("thinking"))

    assert events == []
    assert mood.valence == mood.config.baseline_valence
    assert mood.arousal == mood.config.baseline_arousal


def test_look_and_pause_tags_do_not_nudge_mood():
    mood, events = make_mood()

    mood.handle(tag_event("look:chat"))
    mood.handle(tag_event("pause"))

    assert events == []


def test_ignores_non_tag_event_kinds():
    mood, events = make_mood()

    mood.handle(Event(kind=Kind.BRAIN_TOKEN, turn_id="t1", payload={"text": "hi"}))

    assert events == []


def test_decay_applied_lazily_before_the_next_nudge():
    clock = FakeClock()
    mood, _ = make_mood(config=make_config(half_life_s=10.0), clock=clock)

    mood.handle(tag_event("angry"))  # valence -0.6, arousal +0.8 from a 0.0 baseline
    clock.advance(10.0)  # exactly one half-life
    mood.handle(tag_event("sad"))  # applies its own delta on top of the decayed point

    # angry's -0.6 decays halfway to 0.0 baseline -> -0.3, then sad's -0.7 lands on top.
    assert math.isclose(mood.valence, -1.0, abs_tol=1e-9)


def test_nudge_clamps_at_the_positive_bound():
    mood, _ = make_mood(config=make_config(baseline_arousal=0.9))

    mood.handle(tag_event("surprise"))  # arousal delta +0.9, would overflow past 1.0

    assert mood.arousal == 1.0


def test_nudge_clamps_at_the_negative_bound():
    mood, _ = make_mood(config=make_config(baseline_valence=-0.9))

    mood.handle(tag_event("sad"))  # valence delta -0.7, would overflow past -1.0

    assert mood.valence == -1.0


def test_carries_the_triggering_events_turn_id():
    mood, events = make_mood()

    mood.handle(tag_event("happy", turn_id="t42"))

    assert events[0].turn_id == "t42"


def test_starts_at_the_configured_baseline():
    mood, _ = make_mood(config=make_config(baseline_valence=0.2, baseline_arousal=0.3))

    assert mood.valence == 0.2
    assert mood.arousal == 0.3
