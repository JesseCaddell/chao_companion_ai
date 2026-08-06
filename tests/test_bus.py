import asyncio

import pytest

from chao.bus import Bus
from chao.events import Event, Kind


def make_manual(text: str = "hi") -> Event:
    return Event(kind=Kind.INPUT_MANUAL, payload={"text": text})


def make_chat(priority: int = 5, text: str = "hi", ts: float | None = None) -> Event:
    payload = {"priority": priority, "text": text}
    return (
        Event(kind=Kind.INPUT_CHAT, payload=payload)
        if ts is None
        else Event(kind=Kind.INPUT_CHAT, payload=payload, ts=ts)
    )


async def drain(queue: asyncio.Queue) -> list[Event]:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.fixture
def bus() -> Bus:
    return Bus(speech_cooldown_s=15.0)


async def test_ambient_bypasses_arbitration_entirely(bus: Bus):
    sub = bus.subscribe()
    called = False

    async def handler(event, cancel, turn_id):
        nonlocal called
        called = True

    bus.publish(Event(kind=Kind.INPUT_AMBIENT, payload={"source": "timer"}))
    await asyncio.sleep(0)

    assert not called
    seen = [e.kind for e in await drain(sub)]
    assert seen == [Kind.INPUT_AMBIENT]


async def test_single_manual_turn_runs(bus: Bus):
    sub = bus.subscribe()
    ran = asyncio.Event()

    async def handler(event, cancel, turn_id):
        ran.set()

    task = asyncio.create_task(bus.run(handler))
    bus.publish(make_manual())
    await asyncio.wait_for(ran.wait(), timeout=1.0)
    await asyncio.sleep(0)

    await stop(task)
    seen = [e.kind for e in await drain(sub)]
    assert Kind.DECISION_SELECTED in seen
    assert bus._current is None


async def test_lower_priority_dropped_while_busy(bus: Bus):
    sub = bus.subscribe()
    release = asyncio.Event()

    async def handler(event, cancel, turn_id):
        await release.wait()

    task = asyncio.create_task(bus.run(handler))
    bus.publish(make_chat(priority=1))  # CHAT_DIRECT, occupies the turn
    await asyncio.sleep(0)

    bus.publish(make_chat(priority=5))  # CHAT_BACKGROUND, should be dropped
    await asyncio.sleep(0)

    release.set()
    await asyncio.sleep(0)
    await stop(task)

    dropped = [e for e in await drain(sub) if e.kind == Kind.DECISION_DROPPED]
    assert any(e.payload["reason"] == "busy" for e in dropped)


async def test_higher_priority_preempts_and_cancels(bus: Bus):
    sub = bus.subscribe()
    first_cancelled = asyncio.Event()

    async def handler(event, cancel, turn_id):
        if event.payload.get("priority") == 5:  # the low-priority first turn
            await cancel.wait()
            first_cancelled.set()

    task = asyncio.create_task(bus.run(handler))
    bus.publish(make_chat(priority=5))  # CHAT_BACKGROUND
    await asyncio.sleep(0)

    bus.publish(make_manual())  # MANUAL always outranks chat
    await asyncio.wait_for(first_cancelled.wait(), timeout=1.0)
    await asyncio.sleep(0)

    await stop(task)
    selected = [e for e in await drain(sub) if e.kind == Kind.DECISION_SELECTED]
    assert len(selected) == 2


async def test_cooldown_drops_chat_but_not_voice(bus: Bus):
    sub = bus.subscribe()

    async def handler(event, cancel, turn_id):
        return

    task = asyncio.create_task(bus.run(handler))
    bus.mark_speech_end(0.0)

    bus.publish(make_chat(priority=1, ts=1.0))  # 1s after speech end, inside 15s cooldown
    await asyncio.sleep(0)

    bus.publish(Event(kind=Kind.INPUT_VOICE, payload={"text": "hey"}, ts=1.0))
    await asyncio.sleep(0)

    await stop(task)
    events = await drain(sub)
    dropped = [e for e in events if e.kind == Kind.DECISION_DROPPED]
    selected = [e for e in events if e.kind == Kind.DECISION_SELECTED]
    assert any(e.payload["reason"] == "cooldown" for e in dropped)
    assert len(selected) == 1


async def test_kill_switch_drops_new_turns(bus: Bus):
    sub = bus.subscribe()

    async def handler(event, cancel, turn_id):
        return

    task = asyncio.create_task(bus.run(handler))
    bus.kill()
    bus.publish(make_manual())
    await asyncio.sleep(0)

    await stop(task)
    events = await drain(sub)
    assert any(e.kind == Kind.DECISION_DROPPED and e.payload["reason"] == "killed" for e in events)
