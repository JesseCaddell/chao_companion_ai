"""Physical kill switch (design doc §15): a global hotkey that cancels
whatever the chao is doing right now and blocks new turns until revived.
Built ahead of Twitch chat, deliberately -- it has to exist before any
adversarial input goes live, not after.

`bus.kill()` already does the real work here (sets the in-flight turn's
cancel token, drains the queue, blocks new turns) -- the cancel token is
the same one threaded into `AudioPlayer.play`'s 20ms poll loop and
`Speaker.speak`'s own cancel check (see brain/turn.py, outputs/audio.py),
so a kill silences audio already playing *and* stops the next queued
sentence from starting. This module is just the physical trigger.

pynput's `GlobalHotKeys` listener runs its callbacks on its own OS-level
thread, not the asyncio loop -- the same class of exception CLAUDE.md's
no-threads rule already carves out for `sounddevice`'s audio callback.
Calling `bus.kill()`/`bus.revive()` directly from that thread would touch
`asyncio.Event.set()` and `Queue.get_nowait()` off-loop, which isn't
thread-safe. Marshalled back onto the loop via `call_soon_threadsafe`
instead. If the loop is already closed (a hotkey press racing `main()`'s
own shutdown teardown), `call_soon_threadsafe` raises `RuntimeError` --
caught and the press is dropped, since there's nothing left to kill.

The listener is injectable (`start_listener`) so tests can fire the
kill/revive callbacks directly -- no real OS-level hook, no pynput import,
needed to run the suite.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import yaml

from chao.bus import Bus
from chao.events import Event, Kind


class _Listener(Protocol):
    def stop(self) -> None: ...


def _start_pynput_listener(hotkeys: dict[str, Callable[[], None]]) -> _Listener:
    """Deferred import, same reasoning as `PiperBackend`/`SoundDeviceSink`:
    a test injecting a fake listener shouldn't have to pay pynput's import
    cost or need a real keyboard hook available.
    """
    from pynput import keyboard

    listener = keyboard.GlobalHotKeys(hotkeys)
    listener.start()
    return listener


@dataclass(frozen=True, slots=True)
class KillSwitchConfig:
    kill_hotkey: str = "<ctrl>+<alt>+k"
    revive_hotkey: str = "<ctrl>+<alt>+r"


def load_kill_switch_config(path: Path) -> KillSwitchConfig:
    """Same load/assemble split as `load_motion_config`/`load_fly_config`.
    Reads the `kill_switch:` block out of `config/chao.yaml`.
    """
    data = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    raw = data.get("kill_switch") or {}
    defaults = KillSwitchConfig()
    return KillSwitchConfig(
        kill_hotkey=str(raw.get("kill_hotkey", defaults.kill_hotkey)),
        revive_hotkey=str(raw.get("revive_hotkey", defaults.revive_hotkey)),
    )


@dataclass
class KillSwitch:
    bus: Bus
    config: KillSwitchConfig = field(default_factory=KillSwitchConfig)
    start_listener: Callable[[dict[str, Callable[[], None]]], _Listener] = _start_pynput_listener

    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False)
    _listener: _Listener | None = field(default=None, init=False)

    def start(self) -> None:
        """Call from inside the running event loop -- captures it via
        `get_running_loop()` so the hotkey callbacks (fired from pynput's
        own thread) know which loop to marshal back onto. Must be called
        before either hotkey can do anything.
        """
        self._loop = asyncio.get_running_loop()
        self._listener = self.start_listener(
            {self.config.kill_hotkey: self._on_kill, self.config.revive_hotkey: self._on_revive}
        )

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()

    def _on_kill(self) -> None:
        self._call_threadsafe(self._do_kill)

    def _on_revive(self) -> None:
        self._call_threadsafe(self._do_revive)

    def _call_threadsafe(self, fn: Callable[[], None]) -> None:
        assert self._loop is not None, "KillSwitch.start() was never called"
        try:
            self._loop.call_soon_threadsafe(fn)
        except RuntimeError:
            pass

    def _do_kill(self) -> None:
        self.bus.kill()
        self.bus.publish(Event(kind=Kind.STATE_KILLED, payload={"killed": True}))

    def _do_revive(self) -> None:
        self.bus.revive()
        self.bus.publish(Event(kind=Kind.STATE_KILLED, payload={"killed": False}))
