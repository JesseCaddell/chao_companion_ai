# Session state

Working notes for picking up where the last session left off. This is a progress
log, not a spec — see `chao-companion-design-v0.2.md` for design and `CLAUDE.md`
for standing conventions. Update this at the end of each session.

## Where we are

**Phase 0 is complete. Phase 1 is started** (brain + director + dashboard —
typed text → LLM → tags → emote fires, dashboard live, anticipation nudge).
Build order is design doc §17: 0 VTS spike → 1 brain + director + dashboard →
2 TTS + motion → 3 aliveness → 4 Twitch → 5 voice → 6 memory → 7 reflection.

`src/chao/events.py` and `src/chao/bus.py` are implemented — `Event`
dataclass, `Kind` string constants, and a `Bus` with two lanes: arbitrated
turns (`input.chat`/`input.voice`/`input.manual`, single in-flight, priority
preemption via cancel token, 15s speech cooldown) and un-arbitrated fan-out
for everything else (including `input.ambient`, deliberately, so aliveness
reactions stay unblocked). 9 tests passing, ruff clean. Added `pytest`,
`pytest-asyncio`, `ruff` as dev dependencies (weren't in `pyproject.toml`
before this session).

**The deferred Opus review of the event/bus design happened at the start of
this session** and found three real bugs beyond the five judgment calls
flagged for review, plus confirmed one of those five was itself a bug:

- Turn-triggering events (`input.chat`/`voice`/`manual`) were never fanned
  out to subscribers — only their derived `decision.*` events were, breaking
  invariant 4's "one stream, three consumers" and losing the raw input a
  prompt re-run would need. Fixed: `publish()` now fans out turn-triggering
  events too, minting `turn_id` at arrival (not selection) so drops
  correlate back to the input that produced them.
- §4.1 chat tiers 4-5 (`CHAT_BACKGROUND`) could still win an empty turn slot
  and produce a full spoken response, contradicting the "no full response"
  spec for those tiers. Fixed: `input.chat` scored `CHAT_BACKGROUND` now
  bypasses arbitration entirely, routed like `input.ambient`.
- Preemption teardown had a 2s grace period but never actually stopped a
  turn that ignored its cancel token past that point — it kept running
  concurrently with the new turn. Fixed: `_await_current_teardown` now
  hard-cancels via `task.cancel()` once the grace period elapses, using
  `asyncio.wait` (not `wait_for`) so the timeout can't be confused with
  cancelling the task itself.
- Cooldown was checked *after* preemption, so a candidate that would itself
  be dropped by cooldown could still tear down the currently running turn
  first. Fixed: cooldown check now runs before the busy/preemption check.
- Kill switch didn't drain the queue, so a backlog could flood through on
  `revive()`. Fixed.

Full outcomes (including the two confirmed-as-designed judgment calls —
ambient bypassing arbitration, cooldown exempting voice/manual) are written
up in `docs/event_bus_design.md`, which no longer carries "pending review"
framing. 13 tests passing (up from 9), ruff clean.

**Environment note for next session:** `uv run` (and pipes like `| tail` or
`| tee`) were extremely slow/unresponsive this session — turned out to be a
stale `.venv/.lock` left by an earlier force-killed `uv` process, not an
actual code hang, and it cost significant time to isolate (thank the
`advisor` catch for it). If `uv run pytest` seems to hang, check for stray
`python`/`uv` processes and a stale `.venv/.lock` before assuming the code
is broken — invoke pytest directly via `.venv/Scripts/python.exe -u -m
pytest` (no pipes) to get a fast, honest signal.

## What Phase 0 proved

Built `src/chao/outputs/vts.py` (thin VTS websocket client: connect, request,
token-cached auth) and `src/chao/tools/vts_probe.py` (standalone diagnostic,
`uv run python -m chao.tools.vts_probe`). Ran it live against the user's VTS
instance and confirmed both output channels end-to-end:

- **Discrete channel** — `ExpressionActivationRequest` (explicit `active`
  state) activates/deactivates a named expression cleanly. Use this, not
  `HotkeyTriggerRequest`, which toggles blindly and left `angry` stuck on
  after an early run.
- **Continuous channel** — `InjectParameterDataRequest` cannot write Live2D
  model parameters directly (VTS error 453). It only writes plugin-created
  custom "tracking" parameters, which then have to be bound *by hand, inside
  the VTS UI*, to a specific Live2D output — per-model, one-time, not
  scriptable via the API. Confirmed working once bound: creating a custom
  param, binding it to `happy` in VTS, and injecting into it drove `happy`
  in isolation (not the ball) — which is correct, since eyes and ball are
  separate parameters, matching the two-channel expression model in
  CLAUDE.md/design doc §6.

**Design doc §19 open question 1, originally resolved "no" this phase, was
overturned by the rigger two sessions later — see below.** At the time:
every position-shaped candidate parameter (`xp`, `xp2`, `xp3`, `yp3`, `yp4`)
was slider-tested directly in VTS with no visible motion, and a live
head-drag/webcam test showed no inherent lag on the ball or its aura either.
Logged as the first entry in `docs/rigging_check_list.md`.

The rig's confirmed parameter groups, for reference:
- Eyes/mood: `happy`, `sad`, `angry`, `confused`
- Ball icon content: `heart`, `questionm`, `surprise`, `swirl`, `emote`
- Ball aura visibility: `bubble`, `bubble2`, `hidebub`
- Ball position (added later, see below): `xp4` (X), `yp5` (Y), `yp6`
  (bubble scale)
- Untested and likely irrelevant: `Param`–`Param5` (auto-generated names,
  never slider-tested — low priority, flagged by advisor as a minor gap in
  full enumeration but not expected to change the conclusion)

## Rigger added a ball physics group (session 3)

The artist added a dedicated physics group to the model: `xp4`/`yp5` (ball
X/Y) and `yp6` (bubble scale, −1..1 shrink/grow, unrelated to position).
User confirmed live in VTS that the group has **its own native lag** —
§8.2's damped spring is now baked into the rig, not something we drive in
code. Design doc §8.2/§8.3, `docs/rigging_check_list.md` item 1, and
CLAUDE.md's `director/motion.py` note are all updated to reflect this.

**Fully closed.** User verified directly: with face tracking off, moving
the model around in VTS makes the ball visibly lag on its own. The physics
group reacts to model movement regardless of what's driving it, so no
plugin-side binding is needed. `docs/rigging_check_list.md` item 1 is
resolved — nothing left to verify before phase 2 on this front.

§8.3's "faster/slower spring response by mood" is no longer achievable —
stiffness/damping are now baked into the model's `.physics3.json` at rig
time, not exposed by the VTS API. Amplitude and posture-offset modulation
are unaffected. Doc updated to say so rather than leave it stale.

## brain/ — LLM backend protocol and prompt assembly (session 3)

Built against design doc §4.3/§5, in two commits:

**`brain/backend.py`** — the `LLMBackend` Protocol exactly as §5 specifies
(`stream(system, messages, cancel) -> AsyncIterator[str]`), plus
`CircuitBreakerBackend` (§5.3): races the primary's first token against a
timeout (default 2s); on timeout or any exception *before* the first
token, falls back to a second backend for the whole turn. Once the primary
has yielded anything, no fallback is possible — later errors just
propagate, since switching backends after text is already out would
duplicate it.

**`brain/prompt.py`** — `assemble_prompt` is a pure function (no file/DB
I/O — that's the caller's job) returning a `Prompt` with `stable`
(identity+personality) and `dynamic` (memory) kept as **separate fields**,
not pre-joined into one string. This was the one thing worth getting
exactly right: `stable` must stay byte-identical across turns that don't
change personality state, or cloud prompt caching and llama.cpp's KV slot
cache (CLAUDE.md: "keep the first two byte-stable") silently stop working.
Tested directly (`test_stable_block_is_byte_identical_regardless_of_memory_or_context`).
Chat-sourced input is wrapped in `<message source=... trust=...>` and
labelled `untrusted` regardless of what `config/identity.md` says
(invariant 6) — voice/manual/ambient get `trusted`. Token budgeting is
`len(text)//4` (approximate, named `approx_tokens` so it's never mistaken
for exact); memory is capped to a budget, recent context trims from the
middle when over budget, never the top (§5.2).

**`brain/cloud.py`** (`AnthropicBackend`) and **`brain/local.py`**
(`OllamaBackend`) — the two concrete backends, added in a second commit
once `anthropic`/`httpx` were added as dependencies. Both take an
injectable `client` so tests fake the SDK/HTTP shape instead of hitting
the network or spending credits. Both check the cancel token *between*
chunks, not mid-await — good enough for the common case; a backend that
stops yielding entirely relies on bus.py's hard-cancel escalation instead
of perfect cooperative cancellation.

**Decisions made with the user this session:** cloud backend is Anthropic
(`claude-haiku-4-5-20251001` default, overridable via constructor — not
yet wired to `config/chao.yaml`, which is still empty); local backend is
Ollama rather than raw `llama-server` (friendlier to run, still CPU-only,
still zero-VRAM). `config/identity.md` stays a stub for now — real
character/trait content is a separate pass with an Opus review when the
user's ready, per CLAUDE.md's prompt-design escalation rule.

**Not built yet, deliberately deferred:** the turn orchestrator that
actually satisfies `bus.py`'s `TurnHandler` shape (assembling a `Prompt`,
publishing `brain.request`/`brain.token`/`brain.complete`, calling a
backend). That belongs in `brain/__init__.py` or similar, once
`director/tags.py` exists to consume the token stream — building it
before there's anything to hand tokens to would be premature. Anthropic
prompt caching's `cache_control` breakpoints are also deferred — `Prompt`
keeps the `stable`/`dynamic` seam so that's addable later without a
refactor, just not implemented now.

30 tests passing (up from 13), ruff clean.

## director/tags.py — tolerant tag parser (session 3, cont'd)

`parse_tags(text) -> (cleaned_text, list[ParsedTag])`, pure and stateless
per §9. Two-stage matching: anything bracket-shaped (`[word]` or
`[word:word]`) is stripped from output regardless of recognition, so a
hallucinated `[proud]` or `[laughs]` never reaches TTS; only names/values
in `KNOWN_TAGS`/`KNOWN_LOOK_TARGETS` become a `ParsedTag`. Unclosed
brackets and multi-word bracketed asides are left as literal text —
deliberately not guessed at. Also exports `split_sentences`, reused by
whatever in director.py buffers to sentence boundaries.

Advisor review caught a real bug before commit: a trailing tag (nothing
after it, e.g. a sign-off `[happy]`) produced a `sentence_index` one past
the end of `split_sentences(cleaned)` — an `IndexError` waiting for
director.py, which will schedule emotes by indexing into the sentence
list. Fixed by clamping to the last real sentence index. Tests added for
both the trailing-tag case and tag-only input (empty cleaned text).

**Important precondition for director.py, documented in tags.py's
docstring but not enforced by it:** `parse_tags` must only be called on
text whose tags are already complete. `brain.token` arrives as chunks —
`[hap` / `py]` split across a chunk boundary won't match either half, and
both fragments would leak into TTS as literal text. Buffering partial
tokens until tags (and ideally sentence boundaries) are complete is
director.py's job. Whitespace is collapsed per call too, so don't rely on
leading/trailing whitespace surviving between chunks.

48 tests passing (up from 30), ruff clean.

## director/director.py — tag stream to emote decisions (session 3, cont'd)

`Director` buffers `brain.token` chunks to sentence boundaries
(`process_chunk`/`end_turn`), parses tags per completed sentence via
`tags.parse_tags`, maps recognized tags to an emote pool (§6.2 —
happy/affection→happy, curious/thinking→curious, surprise/confused/sad/angry
→ themselves; `pause`/`look:*` produce `director.tag` telemetry but never
an emote) and fires it via `director.emote`, gated by a per-pool cooldown
and "never the same hotkey twice consecutively" (only meaningful with 2+
hotkeys per pool — current config has 1 each, so it always repeats, which
is correct). Publishes events only — no VTS calls. `publish` is just
`bus.publish` (sync for non-turn-triggering kinds), so Director itself
needs no `async` at all.

Deliberately out of scope this pass: mood valence/arousal (`mood.py`,
still its own empty stub — no `director.mood` published), the `heart`
pool (§6.3, affinity-gated, needs memory/phase 6), fly (`aliveness.py`
owns `state.fly`). Firing is immediate — no TTS exists yet to schedule
against. The single `self.publish(...)` call in `_maybe_fire_emote` is the
seam where CLAUDE.md's "schedule against the audio playback clock, not
token arrival" gets wired in during phase 2.

`config/emotes.yaml` populated with the design doc's §14 example (pools +
fly + heart_gate + reactions) — **hotkey names (`chao.happy` etc.) are
provisional**, copied from the doc, not yet confirmed against this
model's actual VTS hotkey list. Confirm before outputs/vts.py wires
`director.emote` up to real `ExpressionActivationRequest` calls. Added
`pyyaml` to load it; only the `pools` section is read so far.

Advisor review caught two real bugs before commit:
- `end_turn()` cleared `_turn_id` *before* publishing the final flush's
  events, so the LLM's last sentence — the one most likely to lack
  trailing punctuation and therefore go through the flush path — always
  published with `turn_id=None`, breaking the latency-waterfall/prompt-
  re-run use of that field. Fixed by clearing after.
- The sentence-buffering "safe prefix" had an extra guard that held back
  the *entire remaining buffer* whenever the about-to-be-released prefix
  contained an unmatched `[`. Reachability analysis: a well-formed tag's
  contents can't contain `.!?` or whitespace, so a real tag can never
  straddle a sentence-terminator split point — the guard could only ever
  fire on a genuinely malformed stray `[` in prose, and in that case it
  silently stalled *all further sentence streaming for the rest of the
  turn* (nothing would release until `end_turn()`), quietly defeating
  §12's sentence-streaming latency budget for a case tags.py already
  tolerates fine on its own (leaves it as literal text). Removed. Sentence
  boundary regex de-duplicated: `tags.SENTENCE_BOUNDARY` is now public and
  imported by director.py instead of a second copy — the two had to stay
  identical for `sentence_index` to line up, so keeping one copy removes a
  whole class of drift bug.

68 tests passing (up from 48), ruff clean.

## brain/turn.py — the turn orchestrator (session 3, cont'd)

`TurnOrchestrator` is the piece that finally satisfies `bus.py`'s
`TurnHandler` — instances implement `__call__(event, cancel, turn_id)`
matching that signature directly, so `Bus.run(orchestrator)` just works
(proven with a real `Bus` in `test_works_as_a_real_bus_turn_handler`, not
just direct calls). Per turn: derive a `CurrentEvent` from the triggering
input (`input.chat`→source="chat"+speaker from display_name/login,
`input.voice`→"voice", else "manual"), `assemble_prompt`, publish
`brain.request` immediately after (as early as possible, for §10.5's
anticipation nudge once aliveness.py exists to consume it), stream the
backend publishing `brain.token` per **raw** chunk (tags included — that's
the faithful record) while feeding each chunk to `Director.process_chunk`,
then `Director.end_turn()` to flush the remainder, then `brain.complete`
with `full_text`/`latency_ms`/`usage` (`usage: None` always — not exposed
by the `LLMBackend` protocol yet, a documented gap not a bug).

Recent conversation history is a simple in-process bounded `deque[Turn]`
(default cap 40, must stay even — see comment on `history_limit`), not
persisted; cross-session memory is still phase 6. `personality`/`memory`
are static constructor strings for now, pending `mood.py` and
`memory/store.py`. History reuses `prompt.messages[-1].content` (the
already-wrapped/labelled current event) for the "user" turn rather than
re-wrapping raw text, so untrusted-chat labelling survives into future
turns' recent context instead of being lost.

Advisor review caught one real bug before commit, plus a real test gap:
- A reply that's empty after tag-stripping (backend yields nothing, or
  the model emits only a tag, e.g. `"[happy]"` → cleaned `""`) was still
  appended to history as an empty-content `Message`. The Anthropic API
  rejects empty message content with a 400; `CircuitBreakerBackend` reads
  that as a pre-first-token failure and falls back to local — for every
  turn from then on, since the poisoned empty Turn stays in history. The
  session doesn't recover without a restart. Fixed: the history append
  (not `brain.complete`, which still reports the real `full_text`,
  possibly `""`) is skipped when the cleaned reply is empty.
- Every test called the orchestrator directly; nothing proved the
  headline claim (works as a real `TurnHandler`). Added
  `test_works_as_a_real_bus_turn_handler` against an actual `Bus.run()`.

81 tests passing (up from 68), ruff clean. **Phase 1's core pipeline is
now wired end-to-end**: typed/chat/voice input → `Bus` arbitration →
`TurnOrchestrator` → backend → `Director` → tag/emote events. What's
missing before it's runnable live is `__main__.py` (wiring real backends,
loading `config/identity.md`/`emotes.yaml`, calling `Bus.run`) and a VTS
subscriber for `director.emote` — everything else in the chain is real
code, not a stub.

## `__main__.py` — live entry point (session 3, cont'd)

`build_pipeline(*, identity, emote_config, backend) -> (Bus, TurnOrchestrator)`
holds the dependency-injected wiring (tested with fakes in
`test_main.py`); `main()` owns the real I/O — reads
`config/identity.md`/`config/emotes.yaml`, constructs
`CircuitBreakerBackend(AnthropicBackend(), OllamaBackend(...))`, and runs
`Bus.run(orchestrator)` against typed stdin. Typed lines become
`input.manual` events; a `_print_events` subscriber stands in for the
not-yet-built dashboard/JSONL log, printing streamed tokens, latency,
emote firings, and drops. This is `uv run python -m chao` — the first
live-runnable slice of phase 1, proven by direct construction against
the real config files (not just fakes): imports, `AnthropicBackend()`,
`OllamaBackend(...)`, `CircuitBreakerBackend`, and
`load_emote_config(EMOTES_PATH)` all succeed, and the emote pools load
correctly from the real `config/emotes.yaml`.

Populated `config/identity.md` with an operational placeholder
(temperament + the tag vocabulary from `director/tags.py`, explicitly
labelled a stub pending the real Opus-reviewed character sheet) — an
advisor review flagged that with the file empty, the prompt gave the
model no reason to ever emit a tag, so the live smoke test would look
like a working chat loop with a silently dead emote pipeline. The two
other findings from that review were fixed before commit: `main()` now
fails fast with a clear message if `ANTHROPIC_API_KEY` isn't set
(previously the error surfaced three layers deep as a generic
circuit-breaker fallback failure, after the user had already typed);
shutdown now calls `bus.kill()` before cancelling the run/console tasks
so the in-flight turn's cancel token is set and it exits cleanly instead
of being hard-cancelled mid-await.

**Ollama is not installed on this machine yet** — the local fallback
path uses a placeholder model name (`llama3.1:8b-instruct-q4_K_M`,
override via `CHAO_OLLAMA_MODEL`) and is untested against a real server.
If cloud fails and it falls back, expect a connection error, not a
response. `config/chao.yaml` is still empty/unread — the Anthropic model
choice stays a `cloud.py` constant, not yet configurable.

83 tests passing (up from 81), ruff clean. Not yet run live against the
real Anthropic API in this session (needs `ANTHROPIC_API_KEY` set and
spends credits) — wiring is verified, live behavior isn't.

## `.env` support + first live run (session 3, cont'd)

Added `python-dotenv`; `main()` calls `load_dotenv()` before the API key
check, so a local `.env` (gitignored, template in `.env.example`) is
picked up automatically — no more setting the env var by hand per shell
session.

**Ran the full pipeline live for the first time this session** — typed
input → real Anthropic call → tags → Director → emotes, end to end.
First attempt crashed: `CircuitBreakerBackend`'s `first_token_timeout_s`
default (2.0s, from CLAUDE.md's "~2s" note) was tripping on a *working*
call — a cold Anthropic connection (fresh TLS handshake, no keep-alive)
measured ~2.25s to first token, just over the threshold, so it fell back
to Ollama, which isn't installed, and the turn crashed with a connection
error. Confirmed by calling `AnthropicBackend` directly outside the
breaker (worked fine, ~2.25s to first token). Bumped the default to
5.0s — verified against `test_brain_backend.py` (all tests pass explicit
values, unaffected) and a second live run, which completed cleanly in
~3.1s with both `[happy]` and `[curious]` firing real `director.emote`
events (`chao.happy`, `chao.question`).

This is the first real evidence the identity.md placeholder's tag
instructions work — the model used tags correctly, once per sentence,
at sentence start, unprompted for specifics beyond the placeholder's
example.

83 tests passing (unchanged — no new tests needed: `load_dotenv()` is
I/O, not unit-tested, and `test_brain_backend.py` already covers the
breaker's timeout behavior via explicit values), ruff clean.

## Standing decisions made this session

- Git remote confirmed on `jesse-github` SSH alias (see CLAUDE.md's GitHub
  account check).
- Commit hygiene: batch related changes, one commit per stopping point (now
  in CLAUDE.md).
- Ball spring is real, deliberately designed (§8.2: subtle lag behind head
  motion, ~1/3s settle, not the ball detaching or roaming) — not being
  dropped, just deferred pending a rigging fix.

## Next actions

1. The dashboard skeleton (FastAPI + websocket subscriber) so turns are
   visible without reading console output — `_print_events` in `__main__.py`
   is a throwaway stand-in, not meant to survive.
2. A subscriber that turns `director.emote` into real VTS
   `ExpressionActivationRequest` calls (outputs/vts.py currently only has
   the thin transport client from phase 0). Blocked on action 3.
3. Before that: confirm `config/emotes.yaml`'s hotkey names (`chao.happy`
   etc.) against this model's actual VTS hotkey list — they're copied from
   the design doc's example, unverified.
4. Anticipation nudge (§10.5) needs something subscribing to
   `brain.request` to pop the question-mark ball before audio exists —
   that's aliveness.py's job, not yet built.
5. If/when Ollama gets installed, pull a real model and set
   `CHAO_OLLAMA_MODEL` (or update `DEFAULT_OLLAMA_MODEL` in `__main__.py`) —
   the fallback path is currently untested against a real server.
6. Optionally close the minor gap: slider-test `Param`–`Param5` in VTS (low
   priority, quick, not expected to change any conclusion).
