# CLAUDE.md

Project context for Claude Code. Full spec: `chao-companion-design-v0.2.md`.

## GitHub account check

This machine has two GitHub identities: `jesse-github` (Jesse Caddell, owns this repo) and
`midgar-github` (a separate account). **Before any push, verify `git remote -v` shows
`git@jesse-github:...`, not `midgar-github` or an `https://` URL.** HTTPS remotes can silently
authenticate via cached Windows credentials for the wrong account. If the remote is ever wrong,
fix it with:

```bash
git remote set-url origin git@jesse-github:JesseCaddell/chao_companion_ai.git
```

## What this is

A headless Python brain that drives a Live2D chao character in VTube Studio. It listens to
the streamer's mic and Twitch chat, responds with synthesized speech, expresses emotion
through VTS hotkeys and injected parameters, and accumulates memory across sessions.

**We are not building a renderer.** VTube Studio is the renderer in every mode. OBS captures
the VTS window when live; during local testing you just watch that same window. If a task
seems to call for drawing the character, the task is wrong.

## Current status

**Phase 0.** Connect to VTS, authenticate, dump `Live2DParameterListRequest`, trigger a
hotkey, inject a custom parameter. Nothing else is in scope until that dump exists — it
determines whether the ball has independent position parameters, which gates §8.2.

Build order (design doc §17): 0 VTS spike → 1 brain + director + dashboard → 2 TTS + motion
→ 3 aliveness → 4 Twitch → 5 voice → 6 memory → 7 reflection.

## Hard invariants

Violating any of these is a design regression, not a style choice.

1. **Zero VRAM.** The user games on the same machine. Nothing loads onto the GPU — not
   whisper, not TTS, not embeddings. `device="cpu"` everywhere, always.
2. **No mouth.** The chao has no mouth. No visemes, no phoneme alignment, no Rhubarb, no
   `MouthOpen`. Speech drives body bob, head nod, and the ball spring instead.
3. **The LLM never emits parameter values.** It emits sparse semantic tags (`[happy]`,
   `[curious]`). The director owns all physical interpretation. If you find yourself
   prompting the model for numbers, stop.
4. **Structured events, never `print()`.** Every component emits `Event` objects to the bus.
   The dashboard, SQLite writer, and JSONL session log are all consumers of that one stream.
   Logging that isn't an `Event` is invisible to all three.
5. **Everything below the bus is cancellable.** Any in-flight turn must abort cleanly on a
   higher-priority event or the kill switch. Pass the `asyncio.Event` cancel token through;
   never write an uninterruptible await chain.
6. **Chat text never enters the system prompt.** It goes in the user turn, inside explicit
   delimiters, labelled untrusted. Twitch is adversarial input.
7. **`config/identity.md` is read-only to code.** Core traits never drift. Only the
   reflection job writes personality state, and only to the `beliefs` table.

## Architecture

```
inputs (twitch / voice / ambient)
  → bus.py            priority queue, turn arbiter, cancellation
  → brain/            prompt assembly, pluggable LLM backend
  → director/         tag parsing, mood state, emote scheduling, aliveness
  → outputs/          TTS → virtual cable, VTS websocket, dashboard
```

`src/chao/` mirrors this exactly. See design doc §14 for the full tree.

## Conventions

- Python 3.11+, asyncio throughout. No threads except where a library forces it (audio callbacks).
- `uv` for dependency management. Type hints on everything public. `ruff` + `ruff format`.
- Dataclasses over dicts for anything crossing a module boundary. `Event.payload` is the
  deliberate exception.
- Config in YAML under `config/`, never hardcoded. Thresholds, cooldowns, and tuning values
  especially — those get changed by feel, at runtime, often.
- All timing uses `time.monotonic()`. `turn_id` threads a full interaction together and is
  what makes the latency waterfall and prompt re-run work; propagate it everywhere.

## Component notes

**bus.py** — single async loop, priority queue. Emits `brain.request` the instant a turn
starts so the aliveness layer can react before audio exists.

**brain/** — stateless per call. Prompt sections in fixed order: identity, personality,
memory, recent context, current event. Keep the first two byte-stable so prompt caching and
llama.cpp's KV slot cache both hit. Cloud is the live default; local CPU for dev and
reflection; circuit-break to local if a cloud call exceeds ~2s.

**director/tags.py** — the parser must be tolerant. Malformed or unknown tags are dropped
silently. A bad tag must never crash a turn or leak into TTS text.

**director/director.py** — schedules emotes against the **audio playback clock**, not token
arrival. Tokens arrive well before the audio they correspond to.

**director/motion.py** — the ball runs through a damped spring so it lags the head
(`stiffness ≈ 120`, `damping ≈ 14`, 60Hz). This is the highest-value single feature in the
project. Always on, during speech and idle and emotes alike.

**director/aliveness.py** — runs independently of the LLM at three rates: 60Hz micro, ~1Hz
meso, ~1/min macro. Idle drift uses smoothed noise, never sine waves — a detectable loop is
worse than no motion. Target roughly 10 non-verbal reactions per spoken response.

**outputs/vts.py** — websocket on `ws://localhost:8001`, one-time token auth, token cached to
disk. Two channels: `HotkeyTriggerRequest` for discrete emotes, `InjectParameterDataRequest`
for continuous state. Hotkeys are transient punctuation; parameters carry sustained state.
Never sustain an authored emote beyond ~4s.

**dashboard/** — a pure subscriber. All authoritative state lives in the brain process.
Commands travel back over the same websocket as explicit messages. The dashboard crashing
must never affect the chao mid-stream.

## Expression model

Two independently addressable channels:

- **Eyes** carry sustained mood — happy (creased), sad, angry (slanted), confused (`>.<`)
- **Ball** carries beat-level cognition — heart, question mark, exclamation, corkscrew

Mood is a point in 2D valence/arousal space that decays toward a personality baseline. Tags
nudge the point; the point drives parameters.

Two special cases:

- **Heart is gated on affinity**, not just valence. It is a relationship signal, and it's how
  long-term growth becomes visible on screen. See design doc §6.3.
- **Fly is a state machine** with hysteresis (on above 0.70 arousal, off below 0.40, 5s
  minimum dwell). Lands on `[sad]` and during low-energy macro state regardless.

## Commands

```bash
uv sync                                  # install
uv run python -m chao                    # run the brain
uv run python -m chao.tools.vts_probe    # phase 0 diagnostic dump
uv run python -m chao.memory.reflection   # offline reflection job
uv run pytest                            # tests
uv run ruff check --fix && uv run ruff format
cd src/chao/dashboard/web && npm run dev  # dashboard frontend
```

Requires VTube Studio running with the plugin API enabled, and VB-Audio Virtual Cable
installed for audio routing.

## Testing

- The VTS client, LLM backends, and TTS all get fakes. Nothing in `tests/` should need VTS
  running or spend API credits.
- Emote selection, the fly state machine, mood decay, tag parsing, and the ball spring are
  all pure functions of state — test them directly, no I/O.
- Latency assertions belong in tests. The budget in design doc §12 is a contract.

## Working style

Primary model is Sonnet; Opus is available as an advisor. Escalate to Opus before acting when:

- Changing the `Event` schema or the bus contract — everything downstream depends on both
- Designing prompts, the identity trait sheet, or the reflection logic
- Anything that would alter a hard invariant above
- Retrieval scoring or the affinity function, where the failure mode is subtle and slow

Otherwise proceed directly. Prefer small commits scoped to one phase deliverable. When a
design doc section conflicts with something here, this file is the newer source of truth —
flag the discrepancy rather than silently picking one.