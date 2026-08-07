# Session state

Working notes for picking up where the last session left off. This is a progress
log, not a spec — see `chao-companion-design-v0.2.md` for design and `CLAUDE.md`
for standing conventions. Update this at the end of each session.

## Stopping point (end of session 5, 2026-08-07)

The dashboard (design doc §11) exists and is verified working end-to-end,
scoped deliberately to just the event feed panel (§11.2 item 3) — the
other eight panels render data nothing in the backend produces yet
(retrieval, mood.py, parameter injection), so building UI for them now
would be UI over nothing. `__main__.py`'s `_print_events` console stand-in
is gone, replaced by an in-process FastAPI/uvicorn server plus a Vite+React
frontend (`src/chao/dashboard/web/`), exactly per §11.4's stack. A real
turn (typed input → Anthropic → tags → emotes) was pushed through the
actual websocket and its full event sequence confirmed on the wire via a
scripted client. This part is committed (`99d667a`).

**Presentation layer changed after that commit, same session:** the user
asked, after seeing the plan, to present the dashboard in a native window
via `pywebview` instead of a Chrome tab — Chrome's baseline per-tab/process
overhead is unwelcome given CLAUDE.md invariant 1 (zero VRAM, the user
games on this machine; RAM overhead isn't the invariant's literal scope
but is in the same spirit). Design doc §11.4 and §18's tech table updated
first, then built: `chao.dashboard.server` now also serves the built
frontend (`web/dist/`) as static files, and a new `chao.dashboard.window`
entry point opens it in a native window via WebView2 (Windows' built-in
Chromium-based runtime — reuses what's already on the machine rather than
adding a separate browser instance). See "Native window presentation"
below for what was and wasn't verified — no Chrome extension was available
either time this session, so the actual rendered UI has still never been
seen by anything with eyes, only proven correct at the HTTP/websocket
wire level and via the native window process staying alive with no errors.

Also this session, before any of the above: the C→D directory move (done
between sessions, outside a coding session) had silently broken the
venv's editable install, which would have made every test fail with a
confusing `ModuleNotFoundError` the moment anyone tried to run them —
caught and fixed. Ollama got installed and `qwen3:8b` pulled and wired in
as the local fallback, confirmed CPU-only and working, though slow (as
expected).

Nothing is broken or mid-edit; 95 tests passing, ruff clean. The dashboard
skeleton is committed; **the native-window presentation-layer change is
on disk, tested, and ready, but not yet committed** as of this write-up —
the user hasn't asked for that commit yet. The only loose end from before
is `neutral`, still deliberately shelved.

**Pick up here next session:** next actions list at the bottom, roughly in
priority order. `aliveness.py` is probably the most natural next step now
that the dashboard exists to watch it work, but phase 2 kickoff (TTS model
selection) is equally reasonable if that's the priority instead.

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

## config/emotes.yaml confirmed against live VTS (session 3, cont'd)

Queried the running VTS instance directly (`ExpressionStateRequest` +
`HotkeysInCurrentModelRequest`) and replaced the placeholder `chao.*`
names with the real expression file names. All 7 tag-mapped pools plus
`fly` have a matching `<name>.exp3.json` file and a 1:1 hotkey:

- Confirmed: `happy`, `heart`, `question` (curious pool), `surprise`,
  `confused`, `sad`, `angry`, `fly` — all exist. `happy.exp3.json`
  visually verified firing live via `ExpressionActivationRequest`.
- **`neutral` genuinely does not exist in VTS** — not just unverified,
  actually missing. Nothing currently fires it (no tag maps to
  `"neutral"` in `director.py`'s `_TAG_TO_POOL`), so `hotkeys: []` is
  safe as a placeholder. **User still needs to author a default-state
  expression in VTS** before neutral can be wired to anything — that's
  on the user, not blocked on code.

Per CLAUDE.md's phase-0 finding, the config's `hotkeys:` key holds
expression FILE names (for `ExpressionActivationRequest`), not literal
VTS hotkey labels — a 1:1 hotkey exists per expression too, but firing
should go through the expression file, not `HotkeyTriggerRequest`.

**Follow-up (session 4):** user authored a `neutral` hotkey in VTS, but
it came in as VTS's built-in **`RemoveAllExpressions`** type, not
`ToggleExpression` — there's still no `neutral.exp3.json` file, and
`RemoveAllExpressions` has no file to activate at all. This wasn't
deliberate: the user tried to build a real neutral *expression* (a file
with explicit parameter values to reset everything to baseline) but
couldn't tell what values to set without knowing the model's full
parameter list/rest state — VTS doesn't offer an easy "capture current
pose as an expression" shortcut for this. `RemoveAllExpressions` is what
VTS gave by default instead, and works conceptually (clears whatever's
active) but needs a different code path (`HotkeyTriggerRequest` by
hotkey ID, not `ExpressionActivationRequest` by file) than every other
pool. Deliberately shelved — this needs either the exact rest-state
parameter values (a modeling task, not a VTS-UI task) or a code special
case for `RemoveAllExpressions`, and neither is a priority right now
since nothing maps to `neutral` yet (`_TAG_TO_POOL` doesn't reference
it). `config/emotes.yaml`'s `neutral: { hotkeys: [] }` stays correct as
a placeholder.

## outputs/vts.py — VTS emote subscriber (session 4)

`VTSEmoteSubscriber.handle(event)` turns `director.emote` into a real
`ExpressionActivationRequest(active=True)`, then schedules a background
task to send `active=False` after `duration_s` (pulled from
`emote_config`, since the event payload only has pool/hotkey_id/reason) —
enforces CLAUDE.md's "never sustain beyond ~4s" itself rather than
trusting the caller. Deactivation is a background task specifically so
`handle()` returns immediately and doesn't block the consumer loop for
the full hold of an unrelated expression. If the same expression fires
again before its hold expires, the pending deactivate is cancelled and
rescheduled (extends the hold) instead of a stale deactivate cutting the
new activation short — the one genuinely tricky case, covered by its own
test. VTS failures (either direction) are caught, logged, and published
as `Kind.ERROR` rather than crashing the subscriber or the app.

Wired into `__main__.py` as a third background task (alongside the
console printer and the bus runner). Connects/authenticates once at
startup; degrades gracefully — prints a message, keeps the chat loop
running — if VTS isn't up or auth is rejected, rather than crashing the
whole process over an optional output.

**Verified live, visually confirmed by the user** — but it took three
attempts: the first two runs (through the full `__main__.py` pipeline)
produced no visible reaction, which looked like a real bug. Root cause
turned out to be entirely mundane: the user didn't have the VTS window
in view at the moment the 2-2.5s expression fired. An isolated
diagnostic script with a 5s countdown and a 6s hold (long enough to
switch windows and still catch it) confirmed the subscriber works
correctly end-to-end. Worth remembering: a "nothing happened" report
after a fire-and-auto-clear action needs "were you looking at the right
moment" ruled out before assuming the code is broken.

90 tests passing (up from 83 — 7 new in `test_outputs_vts.py`), ruff
clean.

## Project plan addition: TTS, subtitles, personality test segment (session 4)

User request, not yet implemented — logged in design doc §17/§18/§19 so
it isn't lost before phase 2 starts:

- **TTS** was already phase 2's headline deliverable (`Piper (CPU) —
  re-evaluate options at phase 2`, §18) — still needs an actual model
  selected when phase 2 starts, not just "Piper" as a placeholder.
- **Subtitle overlay for the stream** is new — a viewer-facing surface,
  explicitly separate from the dashboard (which CLAUDE.md already
  establishes as streamer-only debug tooling). Likely a lightweight
  browser-source page driven by `brain.token`/`output.speech_start`/`end`,
  design TBD at phase 2. Added as its own row in §18's tech table and
  open question 7.
- **A dedicated personality/voice testing segment** — a way to iterate on
  the chao's character and TTS voice without a live audience. Likely an
  extension of `__main__.py`'s typed-input loop (already proven this
  session) once TTS exists, rather than a new tool — noted as open
  question 6, to actually decide at phase 2, not before.

## Project moved from C: to D:, Ollama installed, qwen3:8b pulled (session 5)

The project directory moved from C: to D: (now `D:\PycharmProjects\chao_companion_ai`,
no space — it had previously lived at a path with a space in it, `D:\Pycharm
Projects\...`, at least once along the way). **The move silently broke the venv's
editable install**: `.venv/Lib/site-packages/_editable_impl_chao.pth` still pointed at
the old `D:\Pycharm Projects\chao_companion_ai\src` path, so `import chao` failed and
all 11 test modules errored at collection with `ModuleNotFoundError: No module named
'chao'`. Fixed with `uv sync` (regenerates the `.pth` to the current path) — 90 tests
passing again, confirmed clean. **If tests ever fail to collect with that error again
after moving/renaming the project directory, this is why — run `uv sync` first before
assuming real code is broken.**

Ollama itself installed and confirmed working:
- System env vars `OLLAMA_MODELS=D:\.ollama\models` and `OLLAMA_NUM_GPU=0` were set by
  the user (Windows System Properties, machine scope) so models live on D: and inference
  stays CPU-only per invariant 1. Verified via registry
  (`[Environment]::GetEnvironmentVariable(..., "Machine")`) rather than trusting the
  already-open shell's env, since a shell started before the vars were created won't see
  them. The Ollama app/server processes were already running from before the vars were
  set, so they were restarted (`Stop-Process` + relaunch `ollama app.exe`) to pick the
  new env up.
- `ollama pull qwen3:8b` (5.2 GB) succeeded and landed correctly on
  `D:\.ollama\models\blobs` (confirmed via `ollama list` and directly inspecting the
  folder; the default `C:\Users\jesse\.ollama\models` stayed empty).
- **Live smoke test via `ollama run qwen3:8b`, CPU-only:** first call took ~2 minutes
  total — 57s cold load (disk → RAM, one-time per keep-alive window) + ~24s prompt eval
  + ~41s generation eval, at **0.89 tokens/s prompt eval / 2.89 tokens/s generation**.
  Slow, but this is the expected cost of invariant 1 (zero VRAM, CPU-only) on an 8B
  model, not a bug. Most of the generated tokens were Qwen3's default "thinking" mode
  reasoning through a one-sentence request before the real answer — `ollama run`'s CLI
  visibly separates `Thinking...` / `...done thinking.` from the actual reply.
  **Confirmed directly against the raw API this session** (`curl .../api/chat` with
  `stream: true`): each streamed chunk has separate `message.thinking` and
  `message.content` keys — `content` stays `""` while reasoning is in progress and only
  populates for the real reply. `brain/local.py`'s `OllamaBackend` already only reads
  `chunk["message"]["content"]`, so thinking tokens are correctly excluded from what
  reaches `Director`/TTS with no code change needed — verified, not just inferred from
  the CLI's rendering. Still worth deciding whether to pass `"think": false` in
  `OllamaBackend`'s payload to cut latency — chao's responses are short/tag-driven,
  unlikely to need multi-step reasoning, and thinking mode roughly doubles wall-clock
  time on top of an already-slow CPU path. Not changed this session — flagging as a
  decision, not making it unilaterally, since it trades latency for potential answer
  quality.
- `__main__.py`'s `DEFAULT_OLLAMA_MODEL` updated from the untested placeholder
  (`llama3.1:8b-instruct-q4_K_M`) to `qwen3:8b`; `.env.example`'s `CHAO_OLLAMA_MODEL`
  comment updated to match. The circuit breaker's local fallback path is now pointed at
  a real, working model, though its first-call latency (~2 min cold) is far past the
  5.0s `first_token_timeout_s` used for the *cloud* primary — that timeout only gates
  cloud's first token, not local's, so this doesn't break anything, but a fallback to
  local after a cloud failure will feel very slow to a live user. Not addressed this
  session (`OllamaBackend`'s `keep_alive` isn't configured — repeat calls within
  Ollama's default keep-alive window should skip the 57s reload).

Nothing else changed. Still not run: an actual end-to-end pipeline turn that falls
through to the local backend (item below).

## Dashboard skeleton (session 5, cont'd)

Built per design doc §11, scoped to the event feed panel only (§11.2 item
3) — see the top-of-file rationale for why the other eight panels wait.

**`src/chao/bus.py`** got one small addition first, needed before a
websocket-backed subscriber could exist safely: `Bus.subscribe()` had no
matching `unsubscribe()`, so every dashboard connection (a browser refresh
during frontend dev, a reconnect) would permanently leak a queue into
`_subscribers` — never removed, still paid for on every future
`_fan_out()`. Added `unsubscribe()` (a `list.remove`, one call site, no new
abstraction) and made `_fan_out` iterate `list(self._subscribers)` so a
subscriber unsubscribing itself mid-fan-out can't mutate the list being
iterated. Flagged to the user first since bus.py touches CLAUDE.md's
"escalate before changing the bus contract" rule — advisor input was that
this specific change is additive/mechanical (symmetric teardown for an
existing lifecycle method), not the kind of arbitration-semantics change
the rule is really guarding against, so it went ahead without a separate
Opus pass. New test: `test_unsubscribe_stops_further_fan_out`. 91 tests
passing at that point.

**`src/chao/dashboard/server.py`** — `create_app(bus) -> FastAPI`,
dependency-injected like `build_pipeline`. One route, `/ws/events`:
accepts the websocket, calls `bus.subscribe()`, relays every `Event` as
JSON (`dataclasses.asdict`) until disconnect, then `bus.unsubscribe()`s in
a `finally`. Checked what actually goes into `Event.payload` across
`turn.py`/`director.py`/`vts.py`/`bus.py` before trusting `send_json` on
it — all primitives (str/int/float/None/list[str]/dict[str,int]) today, so
no custom encoder needed; worth re-checking if a future payload field ever
holds something richer. CORS wide open (`allow_origins=["*"]`) since the
Vite dev server runs on a different port and this never leaves localhost.
New tests in `tests/test_dashboard_server.py`, using
`fastapi.testclient.TestClient`'s websocket support: one confirms a
published event round-trips over the wire, one confirms disconnect
actually calls `unsubscribe` (reaches into `bus._subscribers` directly —
acceptable for a whitebox test of exactly the leak this session set out to
fix). 93 tests passing, ruff clean.

**`src/chao/dashboard/web/`** — scaffolded fresh via `npm create
vite@latest . -- --template react-ts` (the directory only had a
`.gitkeep` before), per design doc §11.4's stack decision. Demo
boilerplate (counter, react/vite logos, marketing-page CSS) stripped
entirely. `App.tsx` is one component: opens a `WebSocket` to
`ws://127.0.0.1:8765/ws/events`, shows a connected/disconnected indicator,
accumulates `brain.token` chunks per `turn_id` into a live streaming
bubble (folded into a normal feed entry on that turn's `brain.complete`),
and renders everything else as a reverse-chronological list capped to 200
entries with kind-specific one-line summaries (latency for
`brain.complete`, pool→hotkey for `director.emote`, etc.). No router, no
state library, no env-var configuration for the WS URL — none of that is
earned yet for a one-screen skeleton. `npm run build` (tsc -b + vite
build) passes clean, confirming the TypeScript is sound even without a
browser to load it in.

**Wired into `__main__.py`:** `_print_events` (which printed everything —
tokens, latency, emotes, drops, errors) is gone. Replaced by two things:
`_run_dashboard_server`, which runs `uvicorn.Server(...).serve()` as an
`asyncio.Task` in the *same* process (not `uvicorn.run`, which would call
its own `asyncio.run` and fight the loop `main()` already owns — this
matters because the dashboard's websocket handler calls `bus.subscribe()`,
an in-process `asyncio.Queue`, not a network call); and `_print_errors`, a
~10-line stderr backstop that only surfaces `Kind.ERROR` — kept
deliberately, per advisor review, so a VTS auth failure or turn crash
isn't silently invisible on a run where nobody has the dashboard open
(first run of the day, uvicorn failed to bind, etc.). Startup now prints
the dashboard's websocket URL and the `npm run dev` command to launch the
frontend. Also fixed two stale bits of `__main__.py` while in there: the
`ANTHROPIC_API_KEY` error message still said Ollama "isn't installed
yet" (it is now), and `DEFAULT_OLLAMA_MODEL` still pointed at the
never-installed placeholder from session 3 (now `qwen3:8b`, matching the
Ollama work earlier this session).

**Verification, and its limits.** Ran the real app (`uv run python -m
chao`, via a named pipe so stdin could stay open across a background
task) and the real Vite dev server side by side, then connected a small
scripted Python client (`websockets.connect(...)`) directly to
`/ws/events` instead of a browser — the Chrome extension used for browser
automation wasn't connected in this environment (`tabs_context_mcp`
failed: "Browser extension is not connected"). Typed a real message into
the running app's stdin and confirmed the *exact* event sequence arrived
over the actual websocket: `input.manual` → `decision.selected` →
`brain.request` → `brain.token` (×2, real Anthropic streaming) →
`director.tag`/`director.emote` (×2, `happy` then `curious`, tags the
model chose unprompted) → `brain.complete` with real latency. This proves
the backend, the bus change, and the websocket wire format are all
correct — it's the same event stream `App.tsx` is built to parse. **What
it does not prove:** that the React component actually renders this
correctly in a real browser (CSS layout, the streaming-bubble/entry-list
split, the connected/disconnected indicator). That's a genuine gap, not
glossed over — see "Native window presentation" below for the follow-up
attempt with the (now-changed) production presentation layer.

## Native window presentation (session 5, cont'd)

After the dashboard skeleton above was committed, the user asked to swap
the presentation layer from a Chrome tab to a native OS window via
`pywebview`, specifically to avoid a full separate Chrome process's
baseline memory overhead — CLAUDE.md invariant 1 is literally about VRAM,
not RAM, but the underlying reason for it (this machine is also used for
gaming, background overhead is worth avoiding) applies in spirit. Design
doc §11.4 and §18's tech table updated to document the decision *before*
building it, per the user's explicit ask to update the plan first.

**What changed:**
- `chao.dashboard.server`: added `WEB_DIST = Path(__file__).parent / "web"
  / "dist"` and, after the websocket route, `app.mount("/", StaticFiles(...),
  ...)` when that directory exists — skipped gracefully if `npm run build`
  hasn't been run, so the `npm run dev` workflow is unaffected. Also moved
  `DASHBOARD_HOST`/`DASHBOARD_PORT` here from `__main__.py`, since they're
  properties of the dashboard server, not the brain — `__main__.py` now
  imports them rather than defining them, which also let the new
  `chao.dashboard.window` import them without an inverted dependency (a
  leaf module importing the composition root).
- `chao.dashboard.window` (new): `webview.create_window(...)` +
  `webview.start()`, pointed at the dashboard server's URL. Deliberately a
  separate process/entry point from `chao.__main__`, not a thread inside
  it — `webview.start()` blocks and needs the OS main thread for its GUI
  loop, which conflicts with `__main__.py`'s asyncio loop already owning
  that thread in the brain process. Run alongside `uv run python -m chao`,
  same relationship a browser tab would have.
- `pyproject.toml`: added `pywebview` (pulled in `pythonnet`/`clr-loader`
  for the Windows EdgeChromium/WebView2 backend).
- `CLAUDE.md`'s Commands section: added the window-launch command and
  `npm run build`; reworded the `npm run dev` line to make clear it's for
  frontend iteration, not normal use, now that the server self-serves the
  built frontend.
- New tests in `test_dashboard_server.py`: static files serve correctly
  when `WEB_DIST` (monkeypatched) points at a real directory with an
  `index.html`, and the mount is absent (404 on `/`) when it doesn't. 95
  tests passing, ruff clean.

**Verification, and its limits (round two).** Built the frontend for real
(`npm run build`), ran the actual app, and confirmed via `curl` that
`GET /` and `GET /assets/*.js` both return 200 with the correct built
HTML/JS — the static-serving code path is genuinely exercised, not just
unit-tested against a fake directory. Then ran
`python -m chao.dashboard.window` for real: no exception, no traceback,
and `msedgewebview2.exe` (six processes — WebView2's normal
browser/renderer/GPU-process split, the same architecture as Chrome)
stayed alive and running, consistent with the runtime having actually
loaded and rendered the page rather than failing silently. **Chrome
extension still wasn't available this session** (same limitation as the
dashboard skeleton work), so — same as before — nobody has visually
confirmed the rendered UI actually looks right inside the window. Process
health plus a confirmed-correct HTTP response is real evidence something
is working, but it is not the same as having seen it.

## Next actions

1. **Visually confirm the dashboard actually renders correctly** — in the
   `pywebview` window now, not a Chrome tab (that's genuinely obsolete as
   of this session). Two ways to get there: get the Chrome extension
   connected (user needs to install it + sign in, see conversation) and
   at least verify the same HTML/CSS in a tab as a proxy, or find another
   way to inspect the native window's actual rendered content. Nothing
   about the wire protocol or the HTTP layer is in question anymore —
   only "does the CSS layout look right" is still unverified, and it's
   been unverified for two rounds now. Cheap to close out, worth doing
   before building anything else on top of it.
2. Aliveness — `aliveness.py` is still an empty stub, and it's arguably
   the single highest-value remaining piece of phase 1. Two things live
   here: the meso/macro idle-drift layers (CLAUDE.md's `director/aliveness.py`
   note — 60Hz micro / ~1Hz meso / ~1/min macro, smoothed noise not sine
   waves), and the §10.5 anticipation nudge specifically — something
   subscribing to `brain.request` to pop the question-mark ball before
   audio exists, now that the dashboard exists to actually watch it fire.
3. `neutral` is shelved, not urgent — nothing fires it yet. When it
   resurfaces: either figure out the model's rest-state parameter values
   well enough to author a real `neutral.exp3.json` (may need the
   rigger's help, per the follow-up note above), or accept
   `RemoveAllExpressions` as the permanent design and give
   `VTSEmoteSubscriber` a special case for `HotkeyTriggerRequest`.
4. ~~If/when Ollama gets installed, pull a real model~~ — done this session
   (`qwen3:8b`, CPU-only, confirmed working, thinking-token exclusion
   verified against the raw API). Follow-ups still outstanding: (a) whether
   to pass `"think": false` to cut latency — **user decision: defer until
   chao is rigged and live, so the effect on real interactions can be felt
   rather than guessed at**, not a blocker for anything else; (b) run one
   real end-to-end turn through the local fallback path (not just a
   standalone `ollama run`) to see the whole chain behave, ideally with
   `keep_alive` considered so it's not paying the 57s cold-load tax every
   time.
5. Optionally close the minor gap: slider-test `Param`–`Param5` in VTS (low
   priority, quick, not expected to change any conclusion).
6. Phase 2 kickoff, when ready: pick a TTS model (Piper is the current
   placeholder, §18), and scope the subtitle overlay + personality/voice
   test segment noted above — none of the three are started yet.
7. Once the dashboard's event feed panel has been used for a bit and its
   rough edges are known, consider the next panel per §11.2's priority
   order (retrieval trace and mood plot both still need their underlying
   features built first, so realistically this is a phase-6+ concern).
