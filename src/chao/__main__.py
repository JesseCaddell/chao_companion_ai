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
from dataclasses import replace
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from chao.brain.backend import CircuitBreakerBackend, LLMBackend
from chao.brain.cloud import AnthropicBackend
from chao.brain.local import OllamaBackend, load_ollama_config
from chao.brain.turn import TurnOrchestrator
from chao.bus import Bus
from chao.dashboard.server import DASHBOARD_HOST, DASHBOARD_PORT, DashboardCommands, create_app
from chao.director.aliveness import Aliveness, Fly, FlyConfig, load_fly_config
from chao.director.director import Director, EmoteConfig, load_emote_config
from chao.director.idle_drift import IdleDrift, IdleDriftConfig, load_idle_drift_config
from chao.director.mood import Mood, load_mood_config
from chao.director.motion import MotionConfig, load_motion_config
from chao.events import Event, Kind
from chao.inputs.free_speech import MIN_INTERVAL_S as FREE_SPEECH_MIN_INTERVAL_S
from chao.inputs.free_speech import PROMPT_TEXT as FREE_SPEECH_PROMPT_TEXT
from chao.inputs.free_speech import FreeSpeech
from chao.inputs.killswitch import KillSwitch, load_kill_switch_config
from chao.inputs.twitch import TwitchChatClient, TwitchConfig, load_twitch_config
from chao.inputs.voice import VoiceConfig, VoiceInput, load_voice_config
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


def _select_backend(choice: str, cloud: LLMBackend, local: LLMBackend) -> LLMBackend:
    """`CHAO_LLM_BACKEND` env var: "cloud" or "local" pins one backend
    directly for the whole run (no `CircuitBreakerBackend` involved, so a
    pinned choice can't silently swap under you mid-test -- the point when
    deliberately testing one side). Anything else, including unset,
    defaults to the normal auto-fallback behavior.
    """
    if choice == "local":
        return local
    if choice == "cloud":
        return cloud
    return CircuitBreakerBackend(cloud, local)


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
    backend = PiperBackend(voice_path, length_scale=tts_config.length_scale)
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


async def _run_dashboard_server(bus: Bus, commands: DashboardCommands | None = None) -> None:
    """Runs uvicorn in-process (not `uvicorn.run`, which calls `asyncio.run`
    and would fight the loop `main()` already owns) since the dashboard's
    websocket subscribes to the same in-memory `Bus` the rest of the app
    uses — it isn't a separate process or network service.
    """
    config = uvicorn.Config(
        create_app(bus, commands),
        host=DASHBOARD_HOST,
        port=DASHBOARD_PORT,
        log_level="warning",
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


async def _run_mood(bus: Bus, mood: Mood) -> None:
    """Runs mood.py's tag-driven valence/arousal tracking (design doc §6,
    §10), plus session 9's minimal timescale loop: `sub.get()` is wrapped
    in `asyncio.wait_for` with `tick_interval_s` as the timeout, so a quiet
    stretch with no events calls `mood.tick()` instead of blocking forever.
    One task, one loop -- simpler than a second task sharing the same
    `Mood` instance, and safe: `Mood`'s methods are synchronous (no
    internal awaits), so there's no interleaving to worry about even if
    there were two callers.

    Session 11: takes a pre-constructed `Mood`, not a `MoodConfig`, same
    shape as `_run_free_speech` taking a `FreeSpeech` -- `main()` now needs
    the same `Mood` instance for the dashboard's "force a mood" override
    command, so it has to be constructed at `main()`'s level, not buried
    inside this function.
    """
    sub = bus.subscribe()
    while True:
        try:
            event = await asyncio.wait_for(sub.get(), timeout=mood.config.tick_interval_s)
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


async def _run_twitch(bus: Bus, twitch_config: TwitchConfig) -> None:
    """Connects to real Twitch IRC and publishes `input.chat` events.
    Degrades gracefully (prints and returns), same shape as
    `_run_vts_subscriber`'s "VTS unavailable" handling, for: no channel
    configured, a connection failure, or the connection dropping later
    (no reconnect loop -- see inputs/twitch.py's docstring).

    `TWITCH_CHANNEL` in the environment overrides `twitch.channel` in
    `config/chao.yaml` -- the channel is instance-specific (whose stream
    this is), not a tuning value, so it belongs with the other per-user
    secrets in `.env` rather than the committed yaml. Anonymous read is
    still the default (session 10 part 10's live check needed no
    credentials at all); `TWITCH_ACCESS_TOKEN`/`TWITCH_BOT_USERNAME`
    switch to an authenticated connection if set. Twitch's OAuth token
    endpoint hands back a bare access token with no `oauth:` prefix --
    IRC's PASS command needs that prefix, so it's added here if missing
    rather than requiring the user to store it pre-formatted.
    """
    channel = os.environ.get("TWITCH_CHANNEL") or twitch_config.channel
    if not channel:
        print(
            "  (no Twitch channel configured -- set TWITCH_CHANNEL in .env or "
            "twitch.channel in config/chao.yaml, chat input disabled)"
        )
        return
    if channel != twitch_config.channel:
        twitch_config = replace(twitch_config, channel=channel)

    access_token = os.environ.get("TWITCH_ACCESS_TOKEN")
    oauth_token = (
        access_token
        if access_token is None or access_token.startswith("oauth:")
        else f"oauth:{access_token}"
    )
    client = TwitchChatClient(
        config=twitch_config,
        publish=bus.publish,
        oauth_token=oauth_token,
        bot_username=os.environ.get("TWITCH_BOT_USERNAME"),
    )
    try:
        await client.run()
    except (OSError, RuntimeError) as e:
        print(f"  (Twitch chat unavailable: {e})")


async def _run_voice(bus: Bus, voice_config: VoiceConfig) -> None:
    """Connects the mic and publishes `input.voice` events. Degrades
    gracefully (prints and returns), same shape as `_run_vts_subscriber`'s
    "VTS unavailable" handling, for: voice disabled in config, or a stream
    open/device failure.

    Also watches `output.speech_start`/`output.speech_end` to drive
    `VoiceInput`'s echo guard (see inputs/voice.py's module docstring) --
    a second concurrent loop over the same bus subscription, since
    `VoiceInput.run()` itself only knows about mic audio, not the bus.
    """
    if not voice_config.enabled:
        print("  (voice.enabled is false in config/chao.yaml, mic input disabled)")
        return
    voice_input = VoiceInput(config=voice_config, publish=bus.publish)
    sub = bus.subscribe()

    async def watch_speech_state() -> None:
        while True:
            event = await sub.get()
            if event.kind == Kind.OUTPUT_SPEECH_START:
                voice_input.on_speech_start()
            elif event.kind == Kind.OUTPUT_SPEECH_END:
                voice_input.on_speech_end()

    watch_task = asyncio.create_task(watch_speech_state())
    try:
        await voice_input.run()
    except (OSError, RuntimeError) as e:
        print(f"  (voice input unavailable: {e})")
    finally:
        watch_task.cancel()


async def _run_speech_cooldown_tracker(bus: Bus) -> None:
    """Session 11 finding: `Bus.mark_speech_end` existed and was fully
    tested (test_bus.py calls it directly) but nothing in the running app
    ever called it -- the 15s post-speech cooldown has never actually been
    active. Small standalone subscriber, always running (not gated on
    voice/twitch/anything else being enabled), same shape as `_run_voice`'s
    `watch_speech_state` loop.
    """
    sub = bus.subscribe()
    while True:
        event = await sub.get()
        if event.kind == Kind.OUTPUT_SPEECH_END:
            bus.mark_speech_end(event.ts)


async def _run_free_speech(bus: Bus, free_speech: FreeSpeech) -> None:
    """Session 11: polls `FreeSpeech.tick()` at 1Hz and publishes
    `input.ambient` when it fires -- the lowest-priority arbitrated input
    (see bus.py), so it never preempts a real conversation and is itself
    gated by the same speech cooldown `_run_speech_cooldown_tracker` wires
    up. `free_speech.enabled`/`interval_s` are mutated live by the
    dashboard's command channel (dashboard/server.py); this loop just
    reads whatever the current values are on each tick.
    """
    while True:
        await asyncio.sleep(1.0)
        if free_speech.tick():
            bus.publish(Event(kind=Kind.INPUT_AMBIENT, payload={"text": FREE_SPEECH_PROMPT_TEXT}))


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

    llm_backend_choice = os.environ.get("CHAO_LLM_BACKEND", "auto").strip().lower()
    if llm_backend_choice != "local" and not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set — the cloud backend can't run.")

    identity = IDENTITY_PATH.read_text() if IDENTITY_PATH.exists() else ""
    emote_config = load_emote_config(EMOTES_PATH)
    mood_config = load_mood_config(EMOTES_PATH)
    fly_config = load_fly_config(EMOTES_PATH)
    tts_config = load_tts_config(CHAO_CONFIG_PATH)
    motion_config = load_motion_config(CHAO_CONFIG_PATH)
    idle_drift_config = load_idle_drift_config(CHAO_CONFIG_PATH)
    kill_switch_config = load_kill_switch_config(CHAO_CONFIG_PATH)
    twitch_config = load_twitch_config(CHAO_CONFIG_PATH)
    voice_config = load_voice_config(CHAO_CONFIG_PATH)
    ollama_config = load_ollama_config(CHAO_CONFIG_PATH)

    cloud = AnthropicBackend()  # reads ANTHROPIC_API_KEY from the environment
    local = OllamaBackend(
        os.environ.get("CHAO_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
        num_thread=ollama_config.num_thread,
        think=ollama_config.think,
    )
    backend = _select_backend(llm_backend_choice, cloud, local)

    bus, orchestrator = build_pipeline(
        identity=identity, emote_config=emote_config, backend=backend
    )
    speaker = _build_speaker(tts_config, motion_config, bus.publish)
    orchestrator.speaker = speaker

    # Session 11: dashboard-driven backend toggle. Reassigning
    # orchestrator.backend is safe mid-turn -- TurnOrchestrator.__call__
    # reads self.backend exactly once, at the `async for chunk in
    # self.backend.stream(...)` line, which binds to whatever backend
    # instance was current at that moment; a later reassignment only
    # affects the *next* turn, never a generator already in flight.
    def _set_backend(choice: str) -> None:
        orchestrator.backend = _select_backend(choice, cloud, local)

    # Session 11: free speech. Starts disabled, config default only (never
    # persisted to disk) -- an autonomous-speech feature that survives a
    # restart and starts talking unprompted is a surprise, not a
    # convenience, per the user's own explicit call.
    free_speech = FreeSpeech()

    def _set_free_speech(enabled: bool, interval_s: float) -> None:
        free_speech.enabled = enabled
        free_speech.interval_s = max(interval_s, FREE_SPEECH_MIN_INTERVAL_S)

    # Session 11 part 6: the rest of design doc §11.2 item 8 (the
    # "override panel"). `mood` is constructed here, not inside
    # `_run_mood`, so `_force_mood` below can reach the same instance
    # `_run_mood`'s loop is ticking/decaying -- same promotion `free_speech`
    # already got for the same reason.
    mood = Mood(config=mood_config, publish=bus.publish)

    def _fire_emote(pool_name: str) -> None:
        pool = emote_config.pools.get(pool_name)
        if pool is None or not pool.hotkeys:
            return
        bus.publish(
            Event(
                kind=Kind.DIRECTOR_EMOTE,
                payload={
                    "pool": pool_name,
                    "hotkey_id": random.choice(pool.hotkeys),
                    "reason": "manual",
                },
            )
        )

    def _force_mood(valence: float, arousal: float) -> None:
        mood.valence = max(-1.0, min(1.0, valence))
        mood.arousal = max(-1.0, min(1.0, arousal))
        bus.publish(
            Event(
                kind=Kind.DIRECTOR_MOOD,
                payload={
                    "valence": mood.valence,
                    "arousal": mood.arousal,
                    "baseline_valence": mood_config.baseline_valence,
                    "baseline_arousal": mood_config.baseline_arousal,
                    "source": "manual",
                },
            )
        )

    dashboard_commands = DashboardCommands(
        set_backend=_set_backend,
        set_free_speech=_set_free_speech,
        fire_emote=_fire_emote,
        force_mood=_force_mood,
    )

    # Started before any input source (stdin, later Twitch) so the kill
    # switch is live from the first possible moment, not an afterthought
    # wired in after other tasks. start() must run inside the loop it's
    # marshalling onto, so it's called here in main(), not from a
    # separately-created task.
    kill_switch = KillSwitch(bus=bus, config=kill_switch_config)
    kill_switch.start()

    error_task = asyncio.create_task(_print_errors(bus.subscribe()))
    dashboard_task = asyncio.create_task(_run_dashboard_server(bus, dashboard_commands))
    vts_task = asyncio.create_task(
        _run_vts_subscriber(
            bus, emote_config, fly_config, speaker, motion_config, idle_drift_config
        )
    )
    aliveness_task = asyncio.create_task(_run_aliveness(bus, emote_config))
    mood_task = asyncio.create_task(_run_mood(bus, mood))
    fly_task = asyncio.create_task(_run_fly(bus, fly_config))
    twitch_task = asyncio.create_task(_run_twitch(bus, twitch_config))
    voice_task = asyncio.create_task(_run_voice(bus, voice_config))
    speech_cooldown_task = asyncio.create_task(_run_speech_cooldown_tracker(bus))
    free_speech_task = asyncio.create_task(_run_free_speech(bus, free_speech))
    run_task = asyncio.create_task(bus.run(orchestrator))

    print(
        "chao is listening. Type a message and press enter. 'quit' or Ctrl+D to exit.\n"
        f"Dashboard: http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/ "
        "(open it with `uv run python -m chao.dashboard.window`, "
        "or `cd src/chao/dashboard/web && npm run dev` while iterating on the frontend)\n"
        f"Kill switch: {kill_switch_config.kill_hotkey} to silence immediately, "
        f"{kill_switch_config.revive_hotkey} to resume."
    )
    try:
        await _read_stdin_into_bus(bus)
    finally:
        kill_switch.stop()
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
        twitch_task.cancel()
        voice_task.cancel()
        speech_cooldown_task.cancel()
        free_speech_task.cancel()
        await asyncio.gather(
            run_task,
            error_task,
            dashboard_task,
            vts_task,
            aliveness_task,
            mood_task,
            fly_task,
            twitch_task,
            voice_task,
            speech_cooldown_task,
            free_speech_task,
            return_exceptions=True,
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ngoodbye")
