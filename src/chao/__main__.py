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
import random
from collections.abc import Callable
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
from chao.director.idle_drift import IdleDrift, IdleDriftConfig, load_idle_drift_config
from chao.director.mood import Mood, MoodConfig, load_mood_config
from chao.director.motion import MotionConfig, load_motion_config
from chao.events import Event, Kind
from chao.outputs.audio import AudioPlayer, resolve_output_device
from chao.outputs.speech import Speaker, TTSConfig, load_tts_config
from chao.outputs.tts import PiperBackend
from chao.outputs.vts import (
    VTSAPIError,
    VTSClient,
    VTSEmoteSubscriber,
    VTSFlySubscriber,
    VTSIdleDriftPlayer,
    VTSMotionPlayer,
)

CONFIG_DIR = Path("config")
IDENTITY_PATH = CONFIG_DIR / "identity.md"
EMOTES_PATH = CONFIG_DIR / "emotes.yaml"
CHAO_CONFIG_PATH = CONFIG_DIR / "chao.yaml"

# Confirmed installed and working (see SESSION_STATE.md) — pulled via
# `ollama pull qwen3:8b`, CPU-only (OLLAMA_NUM_GPU=0), models stored on
# D: via the OLLAMA_MODELS system env var. Override with CHAO_OLLAMA_MODEL
# if needed.
DEFAULT_OLLAMA_MODEL = "qwen3:8b"


def build_pipeline(
    *, identity: str, emote_config: EmoteConfig, backend: LLMBackend
) -> tuple[Bus, TurnOrchestrator]:
    """All the wiring, dependency-injected — reused by main() with real
    backends and by tests with fakes. Callers that want TTS set
    `orchestrator.speaker` afterward (needs `bus.publish`, which doesn't
    exist until the `Bus()` constructed in here) -- same reasoning as
    `main()`'s `_build_speaker` call below.
    """
    bus = Bus()
    director = Director(emote_config=emote_config, publish=bus.publish)
    orchestrator = TurnOrchestrator(
        backend=backend, director=director, publish=bus.publish, identity=identity
    )
    return bus, orchestrator


def _build_speaker(
    tts_config: TTSConfig, motion_config: MotionConfig, publish: Callable[[Event], None]
) -> Speaker | None:
    """Degrades gracefully, same shape as `_run_vts_subscriber`'s "VTS
    unavailable" handling: a missing/unset voice file shouldn't crash the
    app, just leave the chao silent. `speaker.motion` starts unset --
    `_run_vts_subscriber` attaches a real `VTSMotionPlayer` once (if) VTS
    actually connects, since that's the earliest a `VTSClient` exists.
    """
    if not tts_config.voice_path:
        print("  (no tts.voice_path configured in config/chao.yaml, chao won't speak)")
        return None
    voice_path = Path(tts_config.voice_path)
    if not voice_path.exists():
        print(f"  (voice model not found at {voice_path}, chao won't speak)")
        return None
    backend = PiperBackend(voice_path)
    device = resolve_output_device(tts_config.output_device)
    player = AudioPlayer(device=device)
    return Speaker(backend=backend, player=player, publish=publish, motion_config=motion_config)


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


async def _run_vts_subscriber(
    bus: Bus,
    emote_config: EmoteConfig,
    fly_config: FlyConfig,
    speaker: Speaker | None,
    motion_config: MotionConfig,
    idle_drift_config: IdleDriftConfig,
) -> None:
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

    if speaker is not None:
        await _attach_motion(client, speaker, motion_config, bus.publish)

    # Idle drift (§10.1/§10.2) runs for the lifetime of this connection,
    # not per-turn like motion/emotes -- started here (advisor-reviewed:
    # share the one lock-serialized client rather than open a second
    # connection) and cancelled in the `finally` below alongside
    # client.close(). Independent of TTS -- the chao should idle-sway even
    # if `speaker` is None, unlike `_attach_motion` above.
    await _attach_idle_drift(client, idle_drift_config)
    idle_drift_cancel = asyncio.Event()
    idle_drift = IdleDrift(config=idle_drift_config, rng=random.Random())
    idle_drift_player = VTSIdleDriftPlayer(
        client=client,
        x_parameter_name=idle_drift_config.x_parameter_name,
        z_parameter_name=idle_drift_config.z_parameter_name,
        publish=bus.publish,
    )
    idle_drift_task = asyncio.create_task(
        idle_drift_player.run(idle_drift, cancel=idle_drift_cancel)
    )

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
        idle_drift_cancel.set()
        await idle_drift_task
        await client.close()


async def _attach_motion(
    client: VTSClient,
    speaker: Speaker,
    motion_config: MotionConfig,
    publish: Callable[[Event], None],
) -> None:
    """§8.1's envelope-driven motion needs a custom VTS tracking parameter
    to inject into. The *binding* (parameter -> ParamAngleY) is a one-time
    manual step done in the VTS UI (docs/rigging_check_list.md item 4) and
    survives model saves, but the *parameter itself* is created via a
    plugin API call, so it's created fresh (idempotently -- VTS rejects a
    duplicate name, which is treated as "already exists, fine") on every
    startup rather than assumed to persist across a VTS restart.

    If `motion_config.parameter_name` doesn't match whatever's actually
    bound in the VTS UI, injection will send real, successful requests
    into a parameter nothing is listening to -- no error, no motion,
    nothing to notice by. That's a VTS-side setup mismatch, not something
    this function can detect from here.
    """
    try:
        await client.request(
            "ParameterCreationRequest",
            {
                "parameterName": motion_config.parameter_name,
                "explanation": "chao §8.1 envelope-driven motion",
                "min": motion_config.param_min,
                "max": motion_config.param_max,
                "defaultValue": 0,
            },
        )
    except VTSAPIError as e:
        # Expected steady-state case is "already exists" from a previous
        # run, but a bare `except: pass` here would also swallow a genuine
        # API failure -- exactly the silent-injects-into-nothing failure
        # mode this function's own docstring warns about. Print the real
        # error_id/message so a first live run can tell the two apart; VTS
        # hasn't handed us the duplicate-name error's specific ID yet to
        # narrow this catch further.
        print(f"  (ParameterCreationRequest for {motion_config.parameter_name}: {e})")
    except (OSError, RuntimeError) as e:
        print(f"  (couldn't set up motion parameter, chao won't move to speech: {e})")
        return
    speaker.motion = VTSMotionPlayer(
        client=client, parameter_name=motion_config.parameter_name, publish=publish
    )


async def _attach_idle_drift(client: VTSClient, idle_drift_config: IdleDriftConfig) -> None:
    """Same "recreate on every startup, tolerate already-exists" reasoning
    as `_attach_motion` above, for idle drift's two parameters
    (`ChaoHeadTurn`/`ChaoHeadTilt`) -- the parameters are plugin-created
    and may not survive a VTS restart even though their bindings
    (docs/rigging_check_list.md item 5) do.

    Unlike `_attach_motion`, there's nothing to skip attaching on failure
    -- idle drift isn't gated behind another component existing first
    (`speaker` can be `None`; idle drift runs regardless). A connection
    failure here degrades the same way a failure inside
    `VTSIdleDriftPlayer.run` itself already does: the first real injection
    attempt fails, it reports a `Kind.ERROR` and stops, and the chao just
    doesn't idle-sway for that VTS session -- no separate abort path
    needed.
    """
    for name in (idle_drift_config.x_parameter_name, idle_drift_config.z_parameter_name):
        try:
            await client.request(
                "ParameterCreationRequest",
                {
                    "parameterName": name,
                    "explanation": "chao idle drift (design doc §10.1/§10.2)",
                    "min": idle_drift_config.param_min,
                    "max": idle_drift_config.param_max,
                    "defaultValue": 0,
                },
            )
        except VTSAPIError as e:
            print(f"  (ParameterCreationRequest for {name}: {e})")
        except (OSError, RuntimeError) as e:
            print(f"  (couldn't set up idle drift parameter {name}: {e})")


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
    tts_config = load_tts_config(CHAO_CONFIG_PATH)
    motion_config = load_motion_config(CHAO_CONFIG_PATH)
    idle_drift_config = load_idle_drift_config(CHAO_CONFIG_PATH)

    cloud = AnthropicBackend()  # reads ANTHROPIC_API_KEY from the environment
    local = OllamaBackend(os.environ.get("CHAO_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL))
    backend = CircuitBreakerBackend(cloud, local)

    bus, orchestrator = build_pipeline(
        identity=identity, emote_config=emote_config, backend=backend
    )
    speaker = _build_speaker(tts_config, motion_config, bus.publish)
    orchestrator.speaker = speaker

    error_task = asyncio.create_task(_print_errors(bus.subscribe()))
    dashboard_task = asyncio.create_task(_run_dashboard_server(bus))
    vts_task = asyncio.create_task(
        _run_vts_subscriber(
            bus, emote_config, fly_config, speaker, motion_config, idle_drift_config
        )
    )
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
