import asyncio

from chao.__main__ import build_pipeline
from chao.director.director import EmoteConfig, EmotePool
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
        pools={"happy": EmotePool(hotkeys=["chao.happy"], cooldown_s=4.0, duration_s=2.5)}
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

    assert emote.payload["pool"] == "happy"
