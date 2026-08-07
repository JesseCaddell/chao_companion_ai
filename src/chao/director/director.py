"""Turns a stream of brain.token chunks into director.tag / director.emote
events. See design doc §4.5, §6.2, §9.

Scope for this pass, deliberately narrow:

- Buffers streamed text to sentence boundaries and parses tags per
  completed sentence (never mid-tag — see tags.py's streaming
  precondition), returning cleaned text ready for TTS.
- Maps recognized tags to an emote pool (§6.2) and fires it, subject to a
  per-pool cooldown and "never the same hotkey twice consecutively."
- Publishes events; it does not call VTS itself. `director.emote` is the
  director's output — a future outputs/vts.py subscriber turns it into a
  real ExpressionActivationRequest, once real hotkey names are confirmed.

Not in this pass: mood valence/arousal state (mood.py, its own task —
no `director.mood` published here), the heart pool (affinity-gated per
§6.3, needs memory, phase 6), fly (aliveness.py owns `state.fly`).

Firing is immediate once a pool is chosen — there's no TTS yet to
schedule against. CLAUDE.md's "schedule against the audio playback clock,
not token arrival" becomes real once outputs/tts.py exists in phase 2;
the single `self.publish(...)` call in `_maybe_fire_emote` is the seam
where that delay gets inserted later. Building a scheduler now, with no
playback clock to test it against, would be speculative.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from chao.director import tags as tags_module
from chao.director.tags import SENTENCE_BOUNDARY, ParsedTag
from chao.events import Event, Kind

# §6.2. `pause` and `look:*` are attention/motion signals, not emotes —
# left out on purpose. `heart` isn't reachable via this table at all
# (§6.3: affinity-gated, not a direct tag mapping).
_TAG_TO_POOL: dict[str, str] = {
    "happy": "happy",
    "affection": "happy",
    "curious": "curious",
    "thinking": "curious",
    "surprise": "surprise",
    "confused": "confused",
    "sad": "sad",
    "angry": "angry",
}


@dataclass(frozen=True, slots=True)
class EmotePool:
    hotkeys: list[str]
    cooldown_s: float
    duration_s: float


@dataclass(frozen=True, slots=True)
class EmoteConfig:
    pools: dict[str, EmotePool]


def load_emote_config(path: Path) -> EmoteConfig:
    """I/O lives here, not on Director — keeps Director testable without
    the filesystem, matching brain/prompt.py's load/assemble split.
    """
    data = yaml.safe_load(path.read_text()) or {}
    pools = {
        name: EmotePool(
            hotkeys=list(raw.get("hotkeys", [])),
            cooldown_s=float(raw.get("cooldown_s", 0.0)),
            duration_s=float(raw.get("duration_s", 0.0)),
        )
        for name, raw in (data.get("pools") or {}).items()
    }
    return EmoteConfig(pools=pools)


@dataclass
class Director:
    emote_config: EmoteConfig
    publish: Callable[[Event], None]
    clock: Callable[[], float] = time.monotonic  # cooldown bookkeeping, not playback timing
    rng: random.Random = field(default_factory=random.Random)

    _turn_id: str | None = field(default=None, init=False)
    _buffer: str = field(default="", init=False)
    _sentence_index: int = field(default=0, init=False)
    _last_fired: dict[str, float] = field(default_factory=dict, init=False)
    _last_hotkey: dict[str, str] = field(default_factory=dict, init=False)

    def begin_turn(self, turn_id: str) -> None:
        self._turn_id = turn_id
        self._buffer = ""
        self._sentence_index = 0

    def process_chunk(self, text: str) -> list[str]:
        """Feed a brain.token chunk in. Returns any sentences that became
        safe to hand to TTS (cleaned of tags), in order.
        """
        self._buffer += text
        ready, self._buffer = _split_safe_prefix(self._buffer)
        if not ready:
            return []
        return [self._handle_sentence(s) for s in tags_module.split_sentences(ready)]

    def end_turn(self) -> str | None:
        """Flush whatever's left, regardless of trailing punctuation — the
        LLM's last sentence often lacks one (e.g. max_tokens truncation),
        and that text must not be silently dropped.
        """
        remaining = self._buffer.strip()
        self._buffer = ""
        result = self._handle_sentence(remaining) if remaining else None
        self._turn_id = None  # cleared after handling, so its events still carry turn_id
        return result

    def _handle_sentence(self, sentence_text: str) -> str:
        cleaned, parsed_tags = tags_module.parse_tags(sentence_text)
        index = self._sentence_index
        self._sentence_index += 1
        for tag in parsed_tags:
            self.publish(
                Event(
                    kind=Kind.DIRECTOR_TAG,
                    turn_id=self._turn_id,
                    payload={"tag": _tag_key(tag), "sentence_index": index},
                )
            )
            self._maybe_fire_emote(tag)
        return cleaned

    def _maybe_fire_emote(self, tag: ParsedTag) -> None:
        pool_name = _TAG_TO_POOL.get(_tag_key(tag))
        if pool_name is None:
            return
        pool = self.emote_config.pools.get(pool_name)
        if pool is None or not pool.hotkeys:
            return

        now = self.clock()
        last_fired = self._last_fired.get(pool_name)
        if last_fired is not None and now - last_fired < pool.cooldown_s:
            return

        hotkey = _choose_hotkey(pool, self._last_hotkey.get(pool_name), self.rng)
        self._last_fired[pool_name] = now
        self._last_hotkey[pool_name] = hotkey
        self.publish(
            Event(
                kind=Kind.DIRECTOR_EMOTE,
                turn_id=self._turn_id,
                payload={"pool": pool_name, "hotkey_id": hotkey, "reason": _tag_key(tag)},
            )
        )


def _tag_key(tag: ParsedTag) -> str:
    return f"{tag.name}:{tag.value}" if tag.value else tag.name


def _choose_hotkey(pool: EmotePool, last: str | None, rng: random.Random) -> str:
    candidates = pool.hotkeys
    if len(candidates) > 1 and last is not None:
        candidates = [h for h in candidates if h != last] or pool.hotkeys
    return rng.choice(candidates)


def _split_safe_prefix(buffer: str) -> tuple[str, str]:
    """Everything up to and including the last sentence terminator.

    No separate "unclosed bracket" guard is needed: a well-formed tag's
    contents can't contain `.!?` or whitespace (tags.py's pattern), so a
    valid tag can never straddle a split point found this way — the split
    always lands either entirely before or entirely after it. A stray,
    genuinely malformed `[` in prose is tags.py's problem, not this
    function's: it gets left as literal text by parse_tags for whichever
    sentence it lands in. An earlier version of this function also held
    back the whole buffer whenever the prefix looked unbalanced, which
    seemed safer but wasn't — a single stray `[` anywhere in a turn would
    silently stall all further streaming until end_turn(), defeating the
    sentence-streaming latency budget (§12) for a case tags.py already
    handles fine on its own.
    """
    matches = list(SENTENCE_BOUNDARY.finditer(buffer))
    if not matches:
        return "", buffer
    split_at = matches[-1].end()
    return buffer[:split_at], buffer[split_at:]
