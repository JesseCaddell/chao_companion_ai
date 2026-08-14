import asyncio

from chao.brain.turn import TurnOrchestrator
from chao.bus import Bus
from chao.director.director import Director, EmoteConfig, EmotePool
from chao.events import Event, Kind


class FakeBackend:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.calls: list[tuple[str, list]] = []

    async def stream(self, system, messages, cancel):
        self.calls.append((system, list(messages)))
        for c in self.chunks:
            yield c
            if cancel.is_set():
                return


def default_emote_config() -> EmoteConfig:
    return EmoteConfig(
        pools={"happy_eyes": EmotePool(hotkeys=["chao.happy"], cooldown_s=4.0, duration_s=2.5)}
    )


def make_orchestrator(backend, *, identity="Identity text.", history_limit=40):
    events: list[Event] = []
    director = Director(emote_config=default_emote_config(), publish=events.append)
    orchestrator = TurnOrchestrator(
        backend=backend,
        director=director,
        publish=events.append,
        identity=identity,
        history_limit=history_limit,
    )
    return orchestrator, events


def make_input_event(text: str, kind: str = Kind.INPUT_MANUAL, **extra_payload) -> Event:
    return Event(kind=kind, payload={"text": text, **extra_payload})


async def test_publishes_brain_request_with_prompt_info():
    backend = FakeBackend(["Hi there."])
    orchestrator, events = make_orchestrator(backend, identity="Be curious.")

    await orchestrator(make_input_event("Hello"), asyncio.Event(), "t1")

    request = next(e for e in events if e.kind == Kind.BRAIN_REQUEST)
    assert request.turn_id == "t1"
    assert "stable" in request.payload["prompt_sections"]
    assert "messages" in request.payload["prompt_sections"]
    assert request.payload["backend"] == "FakeBackend"
    assert request.payload["token_counts"]["stable"] > 0
    # brain.request must be published before any brain.token — the
    # anticipation nudge (§10.5) needs it as early as possible.
    assert events.index(request) < events.index(
        next(e for e in events if e.kind == Kind.BRAIN_TOKEN)
    )


async def test_streams_tokens_and_publishes_brain_token_per_chunk():
    backend = FakeBackend(["Hel", "lo."])
    orchestrator, events = make_orchestrator(backend)

    await orchestrator(make_input_event("Hi"), asyncio.Event(), "t1")

    token_events = [e for e in events if e.kind == Kind.BRAIN_TOKEN]
    assert [e.payload["text"] for e in token_events] == ["Hel", "lo."]
    assert all(e.turn_id == "t1" for e in token_events)


async def test_publishes_brain_complete_with_full_text_and_latency():
    backend = FakeBackend(["[happy] Hi there!"])
    orchestrator, events = make_orchestrator(backend)

    await orchestrator(make_input_event("Hi"), asyncio.Event(), "t1")

    complete = next(e for e in events if e.kind == Kind.BRAIN_COMPLETE)
    assert complete.turn_id == "t1"
    # full_text is the raw stream (tags included), unlike history below.
    assert complete.payload["full_text"] == "[happy] Hi there!"
    assert complete.payload["latency_ms"] >= 0
    assert complete.payload["usage"] is None


async def test_emote_fires_via_director_during_turn():
    backend = FakeBackend(["[happy] Hi there! "])
    orchestrator, events = make_orchestrator(backend)

    await orchestrator(make_input_event("Hi"), asyncio.Event(), "t1")

    assert any(e.kind == Kind.DIRECTOR_TAG for e in events)
    emote = next(e for e in events if e.kind == Kind.DIRECTOR_EMOTE)
    assert emote.turn_id == "t1"
    assert emote.payload["pool"] == "happy_eyes"


async def test_history_strips_tags_but_full_text_keeps_them():
    backend = FakeBackend(["[happy] Hi there! "])
    orchestrator, _events = make_orchestrator(backend)

    await orchestrator(make_input_event("Hi"), asyncio.Event(), "t1")

    assistant_turn = orchestrator._history[-1]
    assert assistant_turn.role == "assistant"
    assert assistant_turn.text == "Hi there!"
    assert "[happy]" not in assistant_turn.text


async def test_conversation_history_flows_into_next_turns_prompt():
    backend = FakeBackend(["Nice to meet you."])
    orchestrator, _events = make_orchestrator(backend)

    await orchestrator(make_input_event("What's your name?"), asyncio.Event(), "t1")

    backend.chunks = ["Sure!"]
    await orchestrator(make_input_event("Can you help me?"), asyncio.Event(), "t2")

    second_call_messages = backend.calls[1][1]
    contents = [m.content for m in second_call_messages]
    assert any("What's your name?" in c for c in contents)
    assert any("Nice to meet you." in c for c in contents)


async def test_history_is_capped_at_limit():
    backend = FakeBackend(["ok."])
    orchestrator, _events = make_orchestrator(backend, history_limit=2)

    for i in range(3):
        backend.chunks = [f"reply {i}"]
        await orchestrator(make_input_event(f"msg {i}"), asyncio.Event(), f"t{i}")

    assert len(orchestrator._history) == 2
    assert "reply 2" in orchestrator._history[-1].text


async def test_chat_event_derives_speaker_and_untrusted_labelling():
    backend = FakeBackend(["ok"])
    orchestrator, _events = make_orchestrator(backend)
    event = make_input_event("hello chat", kind=Kind.INPUT_CHAT, display_name="Viewer1")

    await orchestrator(event, asyncio.Event(), "t1")

    user_turn = orchestrator._history[0]
    assert 'source="chat"' in user_turn.text
    assert 'trust="untrusted"' in user_turn.text
    assert 'speaker="Viewer1"' in user_turn.text


async def test_voice_event_is_trusted_with_no_speaker():
    backend = FakeBackend(["ok"])
    orchestrator, _events = make_orchestrator(backend)
    event = make_input_event("hello", kind=Kind.INPUT_VOICE)

    await orchestrator(event, asyncio.Event(), "t1")

    user_turn = orchestrator._history[0]
    assert 'source="voice"' in user_turn.text
    assert 'trust="trusted"' in user_turn.text


async def test_cancelled_turn_skips_completion_and_history():
    class HangingBackend:
        async def stream(self, system, messages, cancel):
            yield "[happy] One. "
            await cancel.wait()

    orchestrator, events = make_orchestrator(HangingBackend())
    cancel = asyncio.Event()

    task = asyncio.create_task(orchestrator(make_input_event("hi"), cancel, "t1"))
    await asyncio.sleep(0)
    cancel.set()
    await task

    assert not any(e.kind == Kind.BRAIN_COMPLETE for e in events)
    assert len(orchestrator._history) == 0
    # content that did stream before cancellation still got its due —
    # only the unflushed remainder is abandoned.
    assert any(e.kind == Kind.DIRECTOR_TAG for e in events)


async def test_empty_response_does_not_poison_history():
    """A reply that's empty after tag-stripping must never become a
    zero-content Message on a later turn — the Anthropic API rejects that,
    which CircuitBreakerBackend reads as a pre-first-token failure and
    falls back for every subsequent turn, using the same poisoned history.
    """
    backend = FakeBackend([])  # backend yields nothing at all
    orchestrator, events = make_orchestrator(backend)

    await orchestrator(make_input_event("Hi"), asyncio.Event(), "t1")

    assert len(orchestrator._history) == 0
    complete = next(e for e in events if e.kind == Kind.BRAIN_COMPLETE)
    assert complete.payload["full_text"] == ""

    # a second turn must still work cleanly, proving history wasn't poisoned
    backend.chunks = ["ok"]
    await orchestrator(make_input_event("Hi again"), asyncio.Event(), "t2")
    assert len(orchestrator._history) == 2


async def test_tag_only_response_does_not_poison_history():
    backend = FakeBackend(["[happy]"])  # cleans down to "" once the tag is stripped
    orchestrator, events = make_orchestrator(backend)

    await orchestrator(make_input_event("Hi"), asyncio.Event(), "t1")

    assert len(orchestrator._history) == 0
    assert any(e.kind == Kind.DIRECTOR_EMOTE for e in events)  # the tag still fired

    backend.chunks = ["ok"]
    await orchestrator(make_input_event("Hi again"), asyncio.Event(), "t2")
    assert len(orchestrator._history) == 2


async def test_works_as_a_real_bus_turn_handler():
    """TurnOrchestrator instances must satisfy bus.py's TurnHandler shape
    directly — this is the whole point of the file, so it needs one test
    against a real Bus, not just direct __call__ invocations.
    """
    backend = FakeBackend(["[happy] Hi there! "])
    bus = Bus()
    director = Director(emote_config=default_emote_config(), publish=bus.publish)
    orchestrator = TurnOrchestrator(backend=backend, director=director, publish=bus.publish)

    sub = bus.subscribe()
    task = asyncio.create_task(bus.run(orchestrator))
    bus.publish(Event(kind=Kind.INPUT_MANUAL, payload={"text": "Hi"}))

    complete = await asyncio.wait_for(_next_of_kind(sub, Kind.BRAIN_COMPLETE), timeout=1.0)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert complete.payload["full_text"] == "[happy] Hi there! "
    assert complete.turn_id is not None
    assert complete.turn_id != ""


async def _next_of_kind(sub: asyncio.Queue, kind: str) -> Event:
    while True:
        event = await sub.get()
        if event.kind == kind:
            return event


class FakeSpeaker:
    """Records calls and, optionally, sleeps briefly per sentence -- enough
    to prove __call__ actually waits for speech to drain rather than
    returning while a fake "playback" is still in flight.
    """

    def __init__(self, delay_s: float = 0.0) -> None:
        self.calls: list[str] = []
        self.delay_s = delay_s

    async def speak(self, sentence, *, turn_id, cancel):
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        self.calls.append(sentence)


async def test_speaker_receives_each_cleaned_sentence_in_order():
    backend = FakeBackend(["[happy] One. Two."])
    orchestrator, _events = make_orchestrator(backend)
    speaker = FakeSpeaker()
    orchestrator.speaker = speaker

    await orchestrator(make_input_event("hi"), asyncio.Event(), "t1")

    assert speaker.calls == ["One.", "Two."]


async def test_call_awaits_speech_drain_before_returning():
    backend = FakeBackend(["Hi there."])
    orchestrator, _events = make_orchestrator(backend)
    speaker = FakeSpeaker(delay_s=0.05)
    orchestrator.speaker = speaker

    await orchestrator(make_input_event("hi"), asyncio.Event(), "t1")

    # If __call__ returned before the drain task finished, this would be
    # empty -- the whole point of awaiting the sentinel in turn.py's
    # `finally` block.
    assert speaker.calls == ["Hi there."]


async def test_cancelled_turn_still_drains_the_speech_queue_without_hanging():
    class HangingBackend:
        async def stream(self, system, messages, cancel):
            yield "One. "
            await cancel.wait()

    orchestrator, _events = make_orchestrator(HangingBackend())
    speaker = FakeSpeaker()
    orchestrator.speaker = speaker
    cancel = asyncio.Event()

    task = asyncio.create_task(orchestrator(make_input_event("hi"), cancel, "t1"))
    await asyncio.sleep(0)
    cancel.set()
    # await task alone would hang forever if turn.py's `finally` block
    # didn't push the sentinel -- the timeout turns a hang into a failure
    # instead of a stuck test run.
    await asyncio.wait_for(task, timeout=1.0)

    # "One." was queued before cancellation fired, so it still reaches the
    # (fake) speaker -- turn.py's job is just to drain the queue, not decide
    # what to skip; that's Speaker.speak()'s own cancel check (see
    # test_speak_no_ops_when_already_cancelled in test_outputs_speech.py).
    assert speaker.calls == ["One."]


async def test_speech_failure_reports_an_error_instead_of_crashing_the_turn():
    """A bad voice file or a PortAudio device error must not make an
    otherwise-successful text turn look like it raised -- brain.complete
    already published by the time the drain task (awaited from __call__'s
    own `finally`) would otherwise propagate the exception.
    """

    class BrokenSpeaker:
        async def speak(self, sentence, *, turn_id, cancel):
            raise RuntimeError("model failed to load")

    backend = FakeBackend(["Hi there."])
    orchestrator, events = make_orchestrator(backend)
    orchestrator.speaker = BrokenSpeaker()

    await orchestrator(make_input_event("hi"), asyncio.Event(), "t1")

    assert any(e.kind == Kind.BRAIN_COMPLETE for e in events)
    error = next(e for e in events if e.kind == Kind.ERROR)
    assert "model failed to load" in error.payload["message"]
    assert error.turn_id == "t1"
