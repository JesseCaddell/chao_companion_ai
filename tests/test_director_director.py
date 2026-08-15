import random

from chao.director.director import Director, EmoteConfig, EmotePool, load_emote_config
from chao.events import Kind


def make_config(**pools: EmotePool) -> EmoteConfig:
    return EmoteConfig(pools=pools)


def default_config() -> EmoteConfig:
    return make_config(
        happy_eyes=EmotePool(
            hotkeys=["chao.happy"], cooldown_s=4.0, duration_s=2.5, channel="eyes"
        ),
        question_bub=EmotePool(
            hotkeys=["chao.question"], cooldown_s=3.0, duration_s=2.0, channel="ball"
        ),
        surprise_bub=EmotePool(
            hotkeys=["chao.surprise"], cooldown_s=6.0, duration_s=1.5, channel="ball"
        ),
        confused_eyes=EmotePool(
            hotkeys=["chao.confused_eyes"], cooldown_s=8.0, duration_s=3.0, channel="eyes"
        ),
        confused_bub=EmotePool(
            hotkeys=["chao.confused_bub"], cooldown_s=8.0, duration_s=3.0, channel="ball"
        ),
        sad_eyes=EmotePool(hotkeys=["chao.sad"], cooldown_s=10.0, duration_s=4.0, channel="eyes"),
        angry_eyes=EmotePool(
            hotkeys=["chao.angry"], cooldown_s=15.0, duration_s=3.0, channel="eyes"
        ),
        heart_bub=EmotePool(
            hotkeys=["chao.heart"], cooldown_s=12.0, duration_s=3.0, channel="ball"
        ),
    )


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_director(config=None, clock=None, rng=None) -> tuple[Director, list]:
    events = []
    director = Director(
        emote_config=config or default_config(),
        publish=events.append,
        clock=clock or FakeClock(),
        rng=rng or random.Random(0),
    )
    return director, events


def test_complete_sentence_in_one_chunk_fires_immediately():
    director, events = make_director()
    director.begin_turn("t1")

    # Trailing space matters: a terminator with nothing after it yet is
    # ambiguous (could be "Dr." or generation still in flight) — see
    # test_incomplete_sentence_waits_for_end_turn.
    ready = director.process_chunk("[happy] Hi there! ")

    assert ready == ["Hi there!"]
    kinds = [e.kind for e in events]
    assert kinds == [Kind.DIRECTOR_TAG, Kind.DIRECTOR_EMOTE]


def test_sentence_split_across_chunks_buffers_correctly():
    director, _events = make_director()
    director.begin_turn("t1")

    assert director.process_chunk("[happy] Hi ") == []
    assert director.process_chunk("there!") == []
    assert director.process_chunk(" ") == ["Hi there!"]


def test_action_narration_is_stripped_from_ready_sentence():
    """Same backstop as tags.py's strip_actions, exercised through the real
    process_chunk path — this is what actually reaches TTS and the OBS
    subtitle overlay, not just the unit under tags.py.
    """
    director, _events = make_director()
    director.begin_turn("t1")

    ready = director.process_chunk("[happy] *flutters over here* Hi there! ")

    assert ready == ["Hi there!"]


def test_incomplete_sentence_waits_for_end_turn():
    director, events = make_director()
    director.begin_turn("t1")

    assert director.process_chunk("[happy] Hi there") == []
    assert events == []

    result = director.end_turn()

    assert result == "Hi there"
    assert [e.kind for e in events] == [Kind.DIRECTOR_TAG, Kind.DIRECTOR_EMOTE]
    # end_turn must clear _turn_id only *after* publishing, or the final
    # flush's events lose the turn_id the latency waterfall depends on.
    assert all(e.turn_id == "t1" for e in events)


def test_stray_bracket_does_not_stall_the_rest_of_the_turn():
    """A genuinely malformed stray "[" in prose must not block all further
    sentences from streaming for the rest of the turn — that would defeat
    the sentence-streaming latency budget (§12). tags.py already tolerates
    stray brackets by leaving them as literal text; director.py must not
    be stricter than that.
    """
    director, _events = make_director()
    director.begin_turn("t1")

    ready1 = director.process_chunk("Oops [ here. ")
    assert ready1 == ["Oops [ here."]

    ready2 = director.process_chunk("Next sentence. ")
    assert ready2 == ["Next sentence."]


def test_tag_split_across_chunk_boundary_does_not_leak():
    director, events = make_director()
    director.begin_turn("t1")

    # "Hi." is already complete and tag-free, so it releases immediately;
    # the not-yet-closed "[hap" must not fire early or leak as literal text.
    ready1 = director.process_chunk("Hi. [hap")
    assert ready1 == ["Hi."]
    assert events == []

    ready2 = director.process_chunk("py] Bye. ")

    assert ready2 == ["Bye."]
    assert [e.kind for e in events] == [Kind.DIRECTOR_TAG, Kind.DIRECTOR_EMOTE]
    tag_event = events[0]
    assert tag_event.payload["sentence_index"] == 1  # second sentence overall


def test_multiple_sentences_in_one_chunk_get_incrementing_indices():
    director, events = make_director()
    director.begin_turn("t1")

    director.process_chunk("[happy] One. [sad] Two. ")

    tag_events = [e for e in events if e.kind == Kind.DIRECTOR_TAG]
    assert [e.payload["sentence_index"] for e in tag_events] == [0, 1]


def test_end_turn_flushes_remaining_text_without_terminator():
    director, _events = make_director()
    director.begin_turn("t1")
    director.process_chunk("No terminator here")

    result = director.end_turn()

    assert result == "No terminator here"


def test_end_turn_with_nothing_buffered_returns_none():
    director, events = make_director()
    director.begin_turn("t1")

    assert director.end_turn() is None
    assert events == []


def test_unknown_tag_produces_no_events():
    director, events = make_director()
    director.begin_turn("t1")

    ready = director.process_chunk("[proud] Nice. ")

    assert ready == ["Nice."]
    assert events == []


def test_pause_and_look_tags_produce_tag_event_but_no_emote():
    director, events = make_director()
    director.begin_turn("t1")

    director.process_chunk("[look:chat] Hi chat. ")

    assert [e.kind for e in events] == [Kind.DIRECTOR_TAG]
    assert events[0].payload["tag"] == "look:chat"


def test_tag_sourced_fires_are_never_cooldown_gated():
    """Session 11: a tag is a signal the LLM deliberately chose to emit --
    the director must never silently swallow it because a pool happens to
    still be cooling down from an earlier fire. `pool.cooldown_s` still
    exists (aliveness.py's Aliveness reads it for its own, separate
    cooldown on autonomous reactions), but Director itself no longer gates
    on it at all.
    """
    clock = FakeClock()
    director, events = make_director(clock=clock)
    director.begin_turn("t1")

    director.process_chunk("[happy] One. ")
    director.process_chunk("[happy] Two. ")  # immediately after -- well inside "happy"'s 4.0s

    emote_events = [e for e in events if e.kind == Kind.DIRECTOR_EMOTE]
    assert len(emote_events) == 2


def test_tag_fires_again_in_a_new_turn_without_waiting():
    """A pool that fired near the end of turn N must still fire immediately
    in turn N+1 -- no cross-turn suppression either, now that cooldown
    gating is gone entirely.
    """
    clock = FakeClock()
    director, events = make_director(clock=clock)
    director.begin_turn("t1")
    director.process_chunk("[happy] One. ")

    director.begin_turn("t2")
    director.process_chunk("[happy] Two. ")

    emote_events = [e for e in events if e.kind == Kind.DIRECTOR_EMOTE]
    assert len(emote_events) == 2
    assert emote_events[0].turn_id == "t1"
    assert emote_events[1].turn_id == "t2"


def test_hotkey_never_repeats_consecutively_with_two_hotkeys():
    clock = FakeClock()
    config = make_config(
        happy_eyes=EmotePool(
            hotkeys=["chao.happy_a", "chao.happy_b"], cooldown_s=1.0, duration_s=1.0
        )
    )
    director, events = make_director(config=config, clock=clock)
    director.begin_turn("t1")

    fired = []
    for _ in range(5):
        director.process_chunk("[happy] Hi. ")
        clock.advance(2.0)
    fired = [e.payload["hotkey_id"] for e in events if e.kind == Kind.DIRECTOR_EMOTE]

    assert len(fired) == 5
    assert all(fired[i] != fired[i + 1] for i in range(len(fired) - 1))


def test_single_hotkey_pool_can_repeat():
    clock = FakeClock()
    config = make_config(
        happy_eyes=EmotePool(hotkeys=["chao.happy"], cooldown_s=1.0, duration_s=1.0)
    )
    director, events = make_director(config=config, clock=clock)
    director.begin_turn("t1")

    for _ in range(3):
        director.process_chunk("[happy] Hi. ")
        clock.advance(2.0)

    fired = [e.payload["hotkey_id"] for e in events if e.kind == Kind.DIRECTOR_EMOTE]
    assert fired == ["chao.happy", "chao.happy", "chao.happy"]


def test_missing_pool_in_config_does_not_crash():
    director, events = make_director(config=make_config())  # no pools defined at all
    director.begin_turn("t1")

    ready = director.process_chunk("[happy] Hi. ")

    assert ready == ["Hi."]
    assert [e.kind for e in events] == [Kind.DIRECTOR_TAG]


def test_turn_id_is_threaded_through_events():
    director, events = make_director()
    director.begin_turn("turn-abc")

    director.process_chunk("[happy] Hi. ")

    assert len(events) == 2  # DIRECTOR_TAG and DIRECTOR_EMOTE, not a vacuous pass
    assert all(e.turn_id == "turn-abc" for e in events)


def test_end_turn_clears_turn_id():
    director, _events = make_director()
    director.begin_turn("turn-abc")
    director.process_chunk("Hi.")
    director.end_turn()

    assert director._turn_id is None


def test_load_emote_config_parses_pools(tmp_path):
    config_path = tmp_path / "emotes.yaml"
    config_path.write_text(
        """
pools:
  happy: { hotkeys: [chao.happy], cooldown_s: 4, duration_s: 2.5 }
  heart: { hotkeys: [chao.heart, chao.heart2], cooldown_s: 12, duration_s: 3.0 }

fly:
  hotkey: chao.fly
"""
    )

    config = load_emote_config(config_path)

    assert config.pools["happy"] == EmotePool(
        hotkeys=["chao.happy"], cooldown_s=4.0, duration_s=2.5
    )
    assert config.pools["heart"].hotkeys == ["chao.heart", "chao.heart2"]


def test_load_emote_config_parses_reactions(tmp_path):
    config_path = tmp_path / "emotes.yaml"
    config_path.write_text(
        """
pools:
  curious: { hotkeys: [chao.question], cooldown_s: 3, duration_s: 2.0 }

reactions:
  anticipation: curious
  chat_spike: surprise
"""
    )

    config = load_emote_config(config_path)

    assert config.reactions == {"anticipation": "curious", "chat_spike": "surprise"}


def test_load_emote_config_the_real_file():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "config" / "emotes.yaml"
    config = load_emote_config(path)

    for tag_pool in (
        "happy_eyes",
        "question_bub",
        "surprise_bub",
        "confused_eyes",
        "confused_bub",
        "sad_eyes",
        "angry_eyes",
    ):
        assert tag_pool in config.pools
    assert config.reactions["anticipation"] == "question_bub"


def test_load_emote_config_parses_channel(tmp_path):
    config_path = tmp_path / "emotes.yaml"
    config_path.write_text(
        """
pools:
  happy_eyes: { hotkeys: [chao.happy], cooldown_s: 4, duration_s: 2.5, channel: eyes }
  heart_bub: { hotkeys: [chao.heart], cooldown_s: 12, duration_s: 3.0, channel: ball }
"""
    )

    config = load_emote_config(config_path)

    assert config.pools["happy_eyes"].channel == "eyes"
    assert config.pools["heart_bub"].channel == "ball"


def test_load_emote_config_defaults_channel_to_eyes_when_absent(tmp_path):
    config_path = tmp_path / "emotes.yaml"
    config_path.write_text(
        "pools:\n  happy_eyes: { hotkeys: [chao.happy], cooldown_s: 4, duration_s: 2.5 }\n"
    )

    config = load_emote_config(config_path)

    assert config.pools["happy_eyes"].channel == "eyes"


def test_confused_tag_fires_both_the_eyes_and_ball_pools():
    director, events = make_director()
    director.begin_turn("t1")

    director.process_chunk("[confused] Huh? ")

    emote_events = [e for e in events if e.kind == Kind.DIRECTOR_EMOTE]
    assert {e.payload["pool"] for e in emote_events} == {"confused_eyes", "confused_bub"}
    assert all(e.payload["reason"] == "confused" for e in emote_events)


def test_affection_tag_fires_both_eyes_and_heart_pools():
    """Session 11: heart is no longer affinity-gated -- [affection] reaches
    it directly, same two-pool shape as [confused].
    """
    director, events = make_director()
    director.begin_turn("t1")

    director.process_chunk("[affection] Aww. ")

    emote_events = [e for e in events if e.kind == Kind.DIRECTOR_EMOTE]
    assert {e.payload["pool"] for e in emote_events} == {"happy_eyes", "heart_bub"}
    assert all(e.payload["reason"] == "affection" for e in emote_events)


def test_confused_tags_two_pools_both_fire_every_time():
    """One pool from a two-pool tag is unaffected by the other's cooldown
    config -- neither is cooldown-gated at all now (session 11), so a
    repeated `[confused]` fires both pools every time regardless of each
    pool's configured cooldown_s.
    """
    clock = FakeClock()
    config = make_config(
        confused_eyes=EmotePool(hotkeys=["chao.eyes"], cooldown_s=100.0, duration_s=3.0),
        confused_bub=EmotePool(hotkeys=["chao.bub"], cooldown_s=1.0, duration_s=3.0),
    )
    director, events = make_director(config=config, clock=clock)
    director.begin_turn("t1")

    director.process_chunk("[confused] One. ")
    director.process_chunk("[confused] Two. ")

    pools_fired = [e.payload["pool"] for e in events if e.kind == Kind.DIRECTOR_EMOTE]
    assert pools_fired.count("confused_eyes") == 2
    assert pools_fired.count("confused_bub") == 2
