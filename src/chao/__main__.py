"""Entry point: wires backends + Director + TurnOrchestrator into a Bus and
runs it against typed stdin input. `uv run python -m chao`.

This is the first live-runnable slice of phase 1 — enough to prove the
pipeline built this session actually works end-to-end, not a finished
app. No Twitch/voice input yet; typed lines become `input.manual` events,
the dashboard (§11 — event feed panel only so far) renders the response as
it streams, and VTS shows the matching expression if it's running
(degrades gracefully if either isn't up).

`build_pipeline` holds all the wiring and takes its dependencies as
arguments, so it's testable with fakes; `main()` is where the real I/O
(env vars, config files, stdin, VTS connection, dashboard server) lives,
and only runs under `if __name__ == "__main__"`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from chao.brain.backend import CircuitBreakerBackend, LLMBackend
from chao.brain.cloud import AnthropicBackend
from chao.brain.local import OllamaBackend
from chao.brain.turn import TurnOrchestrator
from chao.bus import Bus
from chao.dashboard.server import DASHBOARD_HOST, DASHBOARD_PORT, create_app
from chao.director.aliveness import Aliveness, Fly, FlyConfig, load_fly_config
from chao.director.director import Director, EmoteConfig, load_emote_config
from chao.director.mood import Mood, MoodConfig, load_mood_config
from chao.events import Event, Kind
from chao.outputs.vts import VTSClient, VTSEmoteSubscriber, VTSFlySubscriber

CONFIG_DIR = Path("config")
IDENTITY_PATH = CONFIG_DIR / "identity.md"
EMOTES_PATH = CONFIG_DIR / "emotes.yaml"

# Confirmed installed and working (see SESSION_STATE.md) — pulled via
# `ollama pull qwen3:8b`, CPU-only (OLLAMA_NUM_GPU=0), models stored on
# D: via the OLLAMA_MODELS system env var. Override with CHAO_OLLAMA_MODEL
# if needed.
DEFAULT_OLLAMA_MODEL = "qwen3:8b"


def build_pipeline(
    *, identity: str, emote_config: EmoteConfig, backend: LLMBackend
) -> tuple[Bus, TurnOrchestrator]:
    """All the wiring, dependency-injected — reused by main() with real
    backends and by tests with fakes.
    """
    bus = Bus()
    director = Director(emote_config=emote_config, publish=bus.publish)
    orchestrator = TurnOrchestrator(
        backend=backend, director=director, publish=bus.publish, identity=identity
    )
    return bus, orchestrator


async def _print_errors(sub: asyncio.Queue[Event]) -> None:
    """The dashboard (§11) is the real event viewer now — this is just a
    stderr backstop so a VTS auth failure or turn crash isn't silent when
    nobody has the dashboard open (e.g. the very first run, or if uvicorn
    failed to bind). Errors only; token/latency/emote detail lives in the
    dashboard's event feed.
    """
    while True:
        event = await sub.get()
        if event.kind == Kind.ERROR:
            print(f"  !! error: {event.payload.get('message')}")


async def _run_dashboard_server(bus: Bus) -> None:
    """Runs uvicorn in-process (not `uvicorn.run`, which calls `asyncio.run`
    and would fight the loop `main()` already owns) since the dashboard's
    websocket subscribes to the same in-memory `Bus` the rest of the app
    uses — it isn't a separate process or network service.
    """
    config = uvicorn.Config(
        create_app(bus), host=DASHBOARD_HOST, port=DASHBOARD_PORT, log_level="warning"
    )
    server = uvicorn.Server(config)
    await server.serve()


async def _run_vts_subscriber(bus: Bus, emote_config: EmoteConfig, fly_config: FlyConfig) -> None:
    """Connects to VTS and turns `director.emote`/`state.fly` events into
    real VTS API calls, over the one shared connection (both subscribers
    dispatch off the same event stream and no-op on kinds they don't own).
    Degrades gracefully, not fatally, if VTS isn't running or rejects
    auth — the chat loop is still useful without it, it just won't show
    anything on the model.

    Subscribes to the bus *before* the connect/authenticate round trip, not
    after. `Bus.subscribe()` has no replay for late subscribers -- with the
    subscribe call after those awaits, a fast first turn (input typed/piped
    right at startup) could fire the §10.5 anticipation nudge before this
    coroutine finished its real network handshake with VTS, silently
    dropping it with no error anywhere, since nothing actually failed.
    Confirmed live: two runs missed the nudge with a clean exit and no VTS
    error, isolated to this ordering by a direct ExpressionActivationRequest
    test that worked fine outside the app. Subscribing first means any
    event published during the handshake just queues up normally instead of
    vanishing.
    """
    sub = bus.subscribe()
    client = VTSClient()
    try:
        await client.connect()
        await client.authenticate()
    except (OSError, RuntimeError) as e:
        print(f"  (VTS unavailable, emotes won't display: {e})")
        return

    emote_subscriber = VTSEmoteSubscriber(
        client=client, emote_config=emote_config, publish=bus.publish
    )
    fly_subscriber = VTSFlySubscriber(client=client, fly_config=fly_config, publish=bus.publish)
    try:
        while True:
            event = await sub.get()
            await emote_subscriber.handle(event)
            await fly_subscriber.handle(event)
    finally:
        await client.close()


async def _run_aliveness(bus: Bus, emote_config: EmoteConfig) -> None:
    """Runs the aliveness layer's anticipation nudge (design doc §10.5) —
    the only piece of aliveness.py built so far; see its docstring for
    what's deliberately deferred and why. No I/O of its own (unlike the VTS
    subscriber), just a bus subscriber loop.
    """
    aliveness = Aliveness(emote_config=emote_config, publish=bus.publish)
    sub = bus.subscribe()
    while True:
        event = await sub.get()
        aliveness.handle(event)


async def _run_mood(bus: Bus, mood_config: MoodConfig) -> None:
    """Runs mood.py's tag-driven valence/arousal tracking (design doc §6,
    §10), plus session 9's minimal timescale loop: `sub.get()` is wrapped
    in `asyncio.wait_for` with `tick_interval_s` as the timeout, so a quiet
    stretch with no events calls `mood.tick()` instead of blocking forever.
    One task, one loop -- simpler than a second task sharing the same
    `Mood` instance, and safe: `Mood`'s methods are synchronous (no
    internal awaits), so there's no interleaving to worry about even if
    there were two callers.
    """
    mood = Mood(config=mood_config, publish=bus.publish)
    sub = bus.subscribe()
    while True:
        try:
            event = await asyncio.wait_for(sub.get(), timeout=mood_config.tick_interval_s)
        except TimeoutError:
            mood.tick()
            continue
        mood.handle(event)


async def _run_fly(bus: Bus, fly_config: FlyConfig) -> None:
    """Runs aliveness.py's Fly state machine (design doc §6.4, session 9's
    horizontal-positioning addition). Same bus-subscriber shape as
    `_run_aliveness`/`_run_mood` -- independent of both, reacting to the
    `director.mood`/`director.tag` events those publish.
    """
    fly = Fly(config=fly_config, publish=bus.publish)
    sub = bus.subscribe()
    while True:
        event = await sub.get()
        fly.handle(event)


async def _read_stdin_into_bus(bus: Bus) -> None:
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, input, "you> ")
        except EOFError:
            return
        text = line.strip()
        if text.lower() in {"quit", "exit"}:
            return
        if text:
            bus.publish(Event(kind=Kind.INPUT_MANUAL, payload={"text": text}))


async def main() -> None:
    load_dotenv()  # loads .env into os.environ if present; no-op otherwise

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set — the cloud backend can't run.")

    identity = IDENTITY_PATH.read_text() if IDENTITY_PATH.exists() else ""
    emote_config = load_emote_config(EMOTES_PATH)
    mood_config = load_mood_config(EMOTES_PATH)
    fly_config = load_fly_config(EMOTES_PATH)

    cloud = AnthropicBackend()  # reads ANTHROPIC_API_KEY from the environment
    local = OllamaBackend(os.environ.get("CHAO_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL))
    backend = CircuitBreakerBackend(cloud, local)

    bus, orchestrator = build_pipeline(
        identity=identity, emote_config=emote_config, backend=backend
    )

    error_task = asyncio.create_task(_print_errors(bus.subscribe()))
    dashboard_task = asyncio.create_task(_run_dashboard_server(bus))
    vts_task = asyncio.create_task(_run_vts_subscriber(bus, emote_config, fly_config))
    aliveness_task = asyncio.create_task(_run_aliveness(bus, emote_config))
    mood_task = asyncio.create_task(_run_mood(bus, mood_config))
    fly_task = asyncio.create_task(_run_fly(bus, fly_config))
    run_task = asyncio.create_task(bus.run(orchestrator))

    print(
        "chao is listening. Type a message and press enter. 'quit' or Ctrl+D to exit.\n"
        f"Dashboard: http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/ "
        "(open it with `uv run python -m chao.dashboard.window`, "
        "or `cd src/chao/dashboard/web && npm run dev` while iterating on the frontend)"
    )
    try:
        await _read_stdin_into_bus(bus)
    finally:
        # kill() sets the in-flight turn's cancel token so _run_turn can
        # exit cleanly instead of being cancelled mid-await, which would
        # otherwise surface as a "Task was destroyed but it is pending"
        # warning on quit.
        bus.kill()
        run_task.cancel()
        error_task.cancel()
        dashboard_task.cancel()
        vts_task.cancel()
        aliveness_task.cancel()
        mood_task.cancel()
        fly_task.cancel()
        await asyncio.gather(
            run_task,
            error_task,
            dashboard_task,
            vts_task,
            aliveness_task,
            mood_task,
            fly_task,
            return_exceptions=True,
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ngoodbye")
