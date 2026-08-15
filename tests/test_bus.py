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


async def test_ambient_triggers_a_turn_at_the_lowest_priority(bus: Bus):
    """Session 11: input.ambient (free speech) now competes for a turn like
    any other input -- superseding the old "ambient never arbitrates"
    behavior, which was about §10.4's non-verbal reactions specifically
    (see bus.py's module docstring). Confirmed here by checking it wins an
    otherwise-empty slot, not just that a handler ran.
    """
    sub = bus.subscribe()
    ran = asyncio.Event()

    async def handler(event, cancel, turn_id):
        ran.set()

    task = asyncio.create_task(bus.run(handler))
    bus.publish(Event(kind=Kind.INPUT_AMBIENT, payload={"text": "..."}))
    await asyncio.wait_for(ran.wait(), timeout=1.0)
    await stop(task)

    seen = [e.kind for e in await drain(sub)]
    assert Kind.INPUT_AMBIENT in seen
    assert Kind.DECISION_SELECTED in seen


async def test_ambient_loses_to_every_arbitrated_priority(bus: Bus):
    """AMBIENT must be the true floor -- even CHAT_ENGAGEMENT (priority 3,
    the lowest chat tier that actually enters arbitration at all --
    CHAT_BACKGROUND itself is fanned-out-only, same as ambient used to be)
    must win an in-flight slot over it.
    """
    cancelled = asyncio.Event()

    async def handler(event, cancel, turn_id):
        if event.kind == Kind.INPUT_AMBIENT:
            await cancel.wait()
            cancelled.set()
        else:
            await asyncio.sleep(10)

    task = asyncio.create_task(bus.run(handler))
    bus.publish(Event(kind=Kind.INPUT_AMBIENT, payload={"text": "..."}))
    await asyncio.sleep(0)

    bus.publish(make_chat(priority=3))  # CHAT_ENGAGEMENT -- still outranks AMBIENT
    await asyncio.wait_for(cancelled.wait(), timeout=1.0)

    await stop(task)


async def test_ambient_respects_the_speech_cooldown(bus: Bus):
    """The same cooldown that gates a low-priority chat reply must gate a
    free-speech turn too, or chao would chatter again immediately after
    finishing a reply.
    """
    sub = bus.subscribe()

    async def handler(event, cancel, turn_id):
        return

    task = asyncio.create_task(bus.run(handler))
    bus.mark_speech_end(0.0)

    bus.publish(Event(kind=Kind.INPUT_AMBIENT, payload={"text": "..."}, ts=1.0))
    await asyncio.sleep(0)

    await stop(task)
    dropped = [e for e in await drain(sub) if e.kind == Kind.DECISION_DROPPED]
    assert any(e.payload["reason"] == "cooldown" for e in dropped)


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

    bus.publish(make_chat(priority=3))  # CHAT_ENGAGEMENT, still arbitrated, should be dropped
    await asyncio.sleep(0)

    release.set()
    await asyncio.sleep(0)
    await stop(task)

    dropped = [e for e in await drain(sub) if e.kind == Kind.DECISION_DROPPED]
    assert any(e.payload["reason"] == "busy" for e in dropped)


async def test_higher_priority_preempts_and_cancels(bus: Bus):
    sub = bus.subscribe()
    first_cancelled = asyncio.Event()
    manual_handled = asyncio.Event()

    async def handler(event, cancel, turn_id):
        if event.kind == Kind.INPUT_MANUAL:
            manual_handled.set()
            return
        await cancel.wait()
        first_cancelled.set()

    task = asyncio.create_task(bus.run(handler))
    bus.publish(make_chat(priority=3))  # CHAT_ENGAGEMENT
    await asyncio.sleep(0)

    bus.publish(make_manual())  # MANUAL always outranks chat
    await asyncio.wait_for(first_cancelled.wait(), timeout=1.0)
    # first_cancelled fires as soon as turn 1 observes its cancel token, which
    # can race ahead of the bus actually creating turn 2 — wait for that too
    # rather than a fixed number of scheduler ticks.
    await asyncio.wait_for(manual_handled.wait(), timeout=1.0)

    await stop(task)
    selected = [e for e in await drain(sub) if e.kind == Kind.DECISION_SELECTED]
    assert len(selected) == 2


async def test_chat_background_bypasses_arbitration(bus: Bus):
    """§4.1 tiers 4-5 must never win an empty turn slot into a full response."""
    sub = bus.subscribe()
    called = False

    async def handler(event, cancel, turn_id):
        nonlocal called
        called = True

    bus.publish(make_chat(priority=5))  # CHAT_BACKGROUND
    await asyncio.sleep(0)

    assert not called
    seen = [e.kind for e in await drain(sub)]
    assert seen == [Kind.INPUT_CHAT]


async def test_turn_triggering_input_is_fanned_out(bus: Bus):
    """Raw inputs must reach subscribers too, not just their derived decisions
    (CLAUDE.md invariant 4: one stream, three consumers)."""
    sub = bus.subscribe()

    async def handler(event, cancel, turn_id):
        return

    task = asyncio.create_task(bus.run(handler))
    bus.publish(make_manual())
    await asyncio.sleep(0)
    await stop(task)

    events = await drain(sub)
    manual = next(e for e in events if e.kind == Kind.INPUT_MANUAL)
    selected = next(e for e in events if e.kind == Kind.DECISION_SELECTED)
    assert manual.turn_id is not None
    assert manual.turn_id == selected.turn_id


async def test_cooldown_checked_before_preemption(bus: Bus):
    """A candidate that will be dropped by cooldown must not tear down the
    turn that's currently running."""
    sub = bus.subscribe()
    cancelled = asyncio.Event()

    async def handler(event, cancel, turn_id):
        await cancel.wait()
        cancelled.set()

    task = asyncio.create_task(bus.run(handler))
    bus.mark_speech_end(0.0)
    bus.publish(make_chat(priority=2, ts=0.0))  # CHAT_ENGAGEMENT, occupies the turn
    await asyncio.sleep(0)

    # Higher priority than the current turn, but inside the cooldown window.
    bus.publish(make_chat(priority=1, ts=1.0))  # CHAT_DIRECT
    await asyncio.sleep(0)

    assert not cancelled.is_set()

    await stop(task)
    dropped = [e for e in await drain(sub) if e.kind == Kind.DECISION_DROPPED]
    assert any(e.payload["reason"] == "cooldown" for e in dropped)


async def test_stuck_turn_is_hard_cancelled_after_teardown_timeout():
    bus = Bus(speech_cooldown_s=15.0, teardown_timeout_s=0.05)
    sub = bus.subscribe()
    stuck_started = asyncio.Event()

    async def handler(event, cancel, turn_id):
        if event.payload.get("priority") == 3:
            stuck_started.set()
            await asyncio.sleep(10)  # ignores the cancel token entirely

    task = asyncio.create_task(bus.run(handler))
    bus.publish(make_chat(priority=3))  # CHAT_ENGAGEMENT
    await asyncio.wait_for(stuck_started.wait(), timeout=1.0)

    bus.publish(make_manual())  # preempts, then must hard-cancel after 0.05s
    await asyncio.sleep(0.2)

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


def test_unsubscribe_stops_further_fan_out(bus: Bus):
    sub = bus.subscribe()
    bus.unsubscribe(sub)

    bus.publish(Event(kind=Kind.INPUT_AMBIENT, payload={"source": "timer"}))

    assert sub.empty()
