import asyncio

from chao.__main__ import _run_mood, build_pipeline
from chao.director.aliveness import Aliveness
from chao.director.director import EmoteConfig, EmotePool
from chao.director.mood import MoodConfig
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


class SuspendingFakeBackend:
    """Like FakeBackend, but genuinely suspends (via a real checkpoint)
    before its first chunk, instead of yielding every chunk synchronously.
    Needed to test the anticipation nudge's actual point (design doc
    §10.5: cover the latency window *before* the first token) -- against
    plain FakeBackend, an entire turn runs start-to-finish in one
    uninterrupted task step with no real suspension point, so aliveness_task
    never gets scheduled until after brain.complete already fired, making
    "arrives before the first token" untestable.
    """

    def __init__(self, chunks):
        self.chunks = list(chunks)

    async def stream(self, system, messages, cancel):
        await asyncio.sleep(0)
        for c in self.chunks:
            yield c
            if cancel.is_set():
                return


def default_emote_config() -> EmoteConfig:
    return EmoteConfig(
        pools={"happy_eyes": EmotePool(hotkeys=["chao.happy"], cooldown_s=4.0, duration_s=2.5)}
    )


async def _next_of_kind(sub: asyncio.Queue, kind: str) -> Event:
    while True:
        event = await sub.get()
        if event.kind == kind:
            return event


async def test_build_pipeline_wires_manual_input_to_brain_complete():
    backend = FakeBackend(["Hi there."])
    bus, orchestrator = build_pipeline(
        identity="Be curious.", emote_config=default_emote_config(), backend=backend
    )

    sub = bus.subscribe()
    task = asyncio.create_task(bus.run(orchestrator))
    bus.publish(Event(kind=Kind.INPUT_MANUAL, payload={"text": "Hi"}))

    complete = await asyncio.wait_for(_next_of_kind(sub, Kind.BRAIN_COMPLETE), timeout=1.0)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert complete.payload["full_text"] == "Hi there."
    assert backend.calls[0][0]  # system prompt is non-empty (identity made it through)
    assert "Be curious." in backend.calls[0][0]


async def test_build_pipeline_fires_emotes_via_director():
    backend = FakeBackend(["[happy] Hi there! "])
    bus, orchestrator = build_pipeline(
        identity="", emote_config=default_emote_config(), backend=backend
    )

    sub = bus.subscribe()
    task = asyncio.create_task(bus.run(orchestrator))
    bus.publish(Event(kind=Kind.INPUT_MANUAL, payload={"text": "Hi"}))

    emote = await asyncio.wait_for(_next_of_kind(sub, Kind.DIRECTOR_EMOTE), timeout=1.0)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert emote.payload["pool"] == "happy_eyes"


async def test_aliveness_fires_anticipation_emote_on_a_real_bus():
    """Wires Aliveness the same way __main__.py's _run_aliveness does (a
    plain bus subscriber, no direct coupling to Director/TurnOrchestrator)
    and confirms the §10.5 anticipation nudge actually reaches the wire on
    a real Bus -- the same event stream the dashboard's websocket relays
    verbatim -- *before* the first brain.token. That ordering is the whole
    point of §10.5 (cover the latency window before any audio/text exists);
    just checking the emote eventually appears would pass even if it showed
    up after the reply had already finished streaming.
    """
    emote_config = EmoteConfig(
        pools={
            "curious": EmotePool(hotkeys=["question.exp3.json"], cooldown_s=3.0, duration_s=2.0)
        },
        reactions={"anticipation": "curious"},
    )
    backend = SuspendingFakeBackend(["Hi there."])
    bus, orchestrator = build_pipeline(identity="", emote_config=emote_config, backend=backend)
    aliveness = Aliveness(emote_config=emote_config, publish=bus.publish)

    sub = bus.subscribe()
    aliveness_sub = bus.subscribe()

    async def run_aliveness():
        while True:
            event = await aliveness_sub.get()
            aliveness.handle(event)

    orchestrator_task = asyncio.create_task(bus.run(orchestrator))
    aliveness_task = asyncio.create_task(run_aliveness())
    bus.publish(Event(kind=Kind.INPUT_MANUAL, payload={"text": "Hi"}))

    kinds_seen: list[str] = []
    anticipation = None
    async with asyncio.timeout(1.0):
        while Kind.BRAIN_COMPLETE not in kinds_seen:
            event = await sub.get()
            kinds_seen.append(event.kind)
            if event.kind == Kind.DIRECTOR_EMOTE:
                anticipation = event

    for task in (orchestrator_task, aliveness_task):
        task.cancel()
    await asyncio.gather(orchestrator_task, aliveness_task, return_exceptions=True)

    assert anticipation is not None
    assert anticipation.payload == {
        "pool": "curious",
        "hotkey_id": "question.exp3.json",
        "reason": "anticipation",
    }
    assert kinds_seen.index(Kind.DIRECTOR_EMOTE) < kinds_seen.index(Kind.BRAIN_TOKEN)


async def test_run_mood_ticks_on_a_quiet_stretch_over_a_real_bus():
    """__main__.py's _run_mood wraps sub.get() in asyncio.wait_for with
    tick_interval_s as the timeout -- confirms that wiring actually calls
    Mood.tick() against a real bus when nothing is published, not just
    that Mood.tick() works correctly in isolation (already covered by
    test_director_mood.py).
    """
    bus, _ = build_pipeline(
        identity="", emote_config=default_emote_config(), backend=FakeBackend([])
    )
    mood_config = MoodConfig(
        baseline_valence=0.0, baseline_arousal=0.0, half_life_s=0.05, tick_interval_s=0.05
    )

    sub = bus.subscribe()
    task = asyncio.create_task(_run_mood(bus, mood_config))
    await asyncio.sleep(0)  # let _run_mood reach its own bus.subscribe() before publishing

    bus.publish(Event(kind=Kind.DIRECTOR_TAG, turn_id="t1", payload={"tag": "angry"}))
    tag_mood = await asyncio.wait_for(_next_of_kind(sub, Kind.DIRECTOR_MOOD), timeout=2.0)
    tick_mood = await asyncio.wait_for(_next_of_kind(sub, Kind.DIRECTOR_MOOD), timeout=2.0)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert tag_mood.payload["source"] == "tag"
    assert tick_mood.payload["source"] == "tick"
    assert tick_mood.turn_id is None
