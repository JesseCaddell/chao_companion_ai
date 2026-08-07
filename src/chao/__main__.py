"""Entry point: wires backends + Director + TurnOrchestrator into a Bus and
runs it against typed stdin input. `uv run python -m chao`.

This is the first live-runnable slice of phase 1 — enough to prove the
pipeline built this session actually works end-to-end, not a finished
app. No dashboard, no VTS output, no Twitch/voice input yet; typed lines
become `input.manual` events and the console prints the response as it
streams.

`build_pipeline` holds all the wiring and takes its dependencies as
arguments, so it's testable with fakes; `main()` is where the real I/O
(env vars, config files, stdin) lives, and only runs under
`if __name__ == "__main__"`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from chao.brain.backend import CircuitBreakerBackend, LLMBackend
from chao.brain.cloud import AnthropicBackend
from chao.brain.local import OllamaBackend
from chao.brain.turn import TurnOrchestrator
from chao.bus import Bus
from chao.director.director import Director, EmoteConfig, load_emote_config
from chao.events import Event, Kind

CONFIG_DIR = Path("config")
IDENTITY_PATH = CONFIG_DIR / "identity.md"
EMOTES_PATH = CONFIG_DIR / "emotes.yaml"

# No Ollama model is confirmed installed yet (see SESSION_STATE.md) — this
# is a placeholder so OllamaBackend can be constructed at all. Override
# with the env var once a real model is pulled; until then the circuit
# breaker's fallback path will itself fail if cloud ever needs it.
DEFAULT_OLLAMA_MODEL = "llama3.1:8b-instruct-q4_K_M"


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


async def _print_events(sub: asyncio.Queue[Event]) -> None:
    """Stand-in for the dashboard/JSONL log (neither built yet) — just
    enough console visibility to prove a turn actually happened. Tokens
    print inline to simulate streaming; everything else gets a line.
    """
    while True:
        event = await sub.get()
        if event.kind == Kind.BRAIN_TOKEN:
            print(event.payload["text"], end="", flush=True)
        elif event.kind == Kind.BRAIN_COMPLETE:
            print(f"\n  (latency: {event.payload['latency_ms']:.0f}ms)")
        elif event.kind == Kind.DIRECTOR_EMOTE:
            print(f"  [emote: {event.payload['pool']} -> {event.payload['hotkey_id']}]")
        elif event.kind == Kind.DECISION_DROPPED:
            print(f"  (dropped: {event.payload.get('reason')})")
        elif event.kind == Kind.ERROR:
            print(f"  !! error: {event.payload.get('message')}")


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
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set — the cloud backend can't run. "
            "(The Ollama fallback is a placeholder and isn't installed yet either.)"
        )

    identity = IDENTITY_PATH.read_text() if IDENTITY_PATH.exists() else ""
    emote_config = load_emote_config(EMOTES_PATH)

    cloud = AnthropicBackend()  # reads ANTHROPIC_API_KEY from the environment
    local = OllamaBackend(os.environ.get("CHAO_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL))
    backend = CircuitBreakerBackend(cloud, local)

    bus, orchestrator = build_pipeline(
        identity=identity, emote_config=emote_config, backend=backend
    )

    console_task = asyncio.create_task(_print_events(bus.subscribe()))
    run_task = asyncio.create_task(bus.run(orchestrator))

    print("chao is listening. Type a message and press enter. 'quit' or Ctrl+D to exit.")
    try:
        await _read_stdin_into_bus(bus)
    finally:
        # kill() sets the in-flight turn's cancel token so _run_turn can
        # exit cleanly instead of being cancelled mid-await, which would
        # otherwise surface as a "Task was destroyed but it is pending"
        # warning on quit.
        bus.kill()
        run_task.cancel()
        console_task.cancel()
        await asyncio.gather(run_task, console_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ngoodbye")
