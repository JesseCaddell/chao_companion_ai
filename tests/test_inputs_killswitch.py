import asyncio
from pathlib import Path

import pytest

from chao.bus import Bus
from chao.events import Event, Kind
from chao.inputs.killswitch import KillSwitch, KillSwitchConfig, load_kill_switch_config


class FakeListener:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakeListenerFactory:
    """Stands in for `_start_pynput_listener`: captures the hotkey->callback
    map instead of hooking any real OS-level keyboard, so tests can invoke
    the callbacks directly -- simulating a hotkey press without pynput.
    """

    def __init__(self) -> None:
        self.hotkeys: dict[str, object] | None = None
        self.listener = FakeListener()

    def __call__(self, hotkeys: dict) -> FakeListener:
        self.hotkeys = hotkeys
        return self.listener


def make_manual() -> Event:
    return Event(kind=Kind.INPUT_MANUAL, payload={"text": "hi"})


async def drain(queue: asyncio.Queue) -> list[Event]:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


@pytest.fixture
def bus() -> Bus:
    return Bus()


async def test_start_binds_both_configured_hotkeys(bus: Bus):
    factory = FakeListenerFactory()
    config = KillSwitchConfig(kill_hotkey="<ctrl>+<alt>+k", revive_hotkey="<ctrl>+<alt>+r")
    switch = KillSwitch(bus=bus, config=config, start_listener=factory)

    switch.start()

    assert factory.hotkeys is not None
    assert set(factory.hotkeys) == {"<ctrl>+<alt>+k", "<ctrl>+<alt>+r"}


async def test_kill_hotkey_blocks_the_next_turn(bus: Bus):
    factory = FakeListenerFactory()
    switch = KillSwitch(bus=bus, start_listener=factory)
    switch.start()
    sub = bus.subscribe()

    async def handler(event, cancel, turn_id):
        return

    task = asyncio.create_task(bus.run(handler))

    # Simulates pynput's own thread invoking the bound callback -- the
    # real callback marshals onto this loop via call_soon_threadsafe, so a
    # tick is needed before the effect is observable.
    factory.hotkeys["<ctrl>+<alt>+k"]()
    await asyncio.sleep(0)

    bus.publish(make_manual())
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    events = await drain(sub)
    assert any(e.kind == Kind.DECISION_DROPPED and e.payload["reason"] == "killed" for e in events)


async def test_kill_hotkey_publishes_state_killed_true(bus: Bus):
    factory = FakeListenerFactory()
    switch = KillSwitch(bus=bus, start_listener=factory)
    switch.start()
    sub = bus.subscribe()

    factory.hotkeys["<ctrl>+<alt>+k"]()
    await asyncio.sleep(0)

    events = await drain(sub)
    killed_events = [e for e in events if e.kind == Kind.STATE_KILLED]
    assert len(killed_events) == 1
    assert killed_events[0].payload == {"killed": True}


async def test_revive_hotkey_unblocks_turns_and_publishes_state_killed_false(bus: Bus):
    factory = FakeListenerFactory()
    switch = KillSwitch(bus=bus, start_listener=factory)
    switch.start()
    sub = bus.subscribe()

    selected: list[Event] = []

    async def handler(event, cancel, turn_id):
        selected.append(event)

    task = asyncio.create_task(bus.run(handler))

    factory.hotkeys["<ctrl>+<alt>+k"]()
    await asyncio.sleep(0)
    factory.hotkeys["<ctrl>+<alt>+r"]()
    await asyncio.sleep(0)
    await drain(sub)  # clear the kill/revive-phase events

    bus.publish(make_manual())
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    events = await drain(sub)
    assert not any(
        e.kind == Kind.DECISION_DROPPED and e.payload["reason"] == "killed" for e in events
    )
    assert len(selected) == 1  # the turn actually ran -- revive genuinely unblocked it


async def test_stop_stops_the_listener(bus: Bus):
    factory = FakeListenerFactory()
    switch = KillSwitch(bus=bus, start_listener=factory)
    switch.start()

    switch.stop()

    assert factory.listener.stopped is True


async def test_stop_before_start_does_not_raise(bus: Bus):
    switch = KillSwitch(bus=bus, start_listener=FakeListenerFactory())
    switch.stop()  # no-op: _listener is still None


def test_call_threadsafe_drops_the_press_when_loop_is_closed(bus: Bus):
    class ClosedLoop:
        def call_soon_threadsafe(self, fn):
            raise RuntimeError("event loop is closed")

    switch = KillSwitch(bus=bus, start_listener=FakeListenerFactory())
    switch._loop = ClosedLoop()

    switch._on_kill()  # must not raise -- the press is just dropped


def test_call_threadsafe_asserts_if_start_was_never_called(bus: Bus):
    switch = KillSwitch(bus=bus, start_listener=FakeListenerFactory())
    with pytest.raises(AssertionError):
        switch._on_kill()


def test_load_kill_switch_config_reads_the_kill_switch_block(tmp_path: Path):
    config_path = tmp_path / "chao.yaml"
    config_path.write_text(
        "kill_switch:\n  kill_hotkey: '<ctrl>+<shift>+x'\n  revive_hotkey: '<ctrl>+<shift>+y'\n"
    )

    config = load_kill_switch_config(config_path)

    assert config.kill_hotkey == "<ctrl>+<shift>+x"
    assert config.revive_hotkey == "<ctrl>+<shift>+y"


def test_load_kill_switch_config_defaults_when_file_missing(tmp_path: Path):
    config = load_kill_switch_config(tmp_path / "does_not_exist.yaml")

    assert config == KillSwitchConfig()


def test_load_kill_switch_config_defaults_when_block_absent(tmp_path: Path):
    config_path = tmp_path / "chao.yaml"
    config_path.write_text("tts:\n  voice_path: foo.onnx\n")

    config = load_kill_switch_config(config_path)

    assert config == KillSwitchConfig()
