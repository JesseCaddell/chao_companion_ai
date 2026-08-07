"""Tag protocol parser. See design doc §9.

The LLM emits sparse inline tags (`[happy]`, `[look:chat]`); this module
finds and strips them so clean text can go to TTS. Pure and stateless —
no I/O, easy to fuzz — matching the testing convention that tag parsing is
tested directly, no fakes needed.

Tolerant by design (CLAUDE.md): a bad tag must never crash a turn or leak
into TTS text. Two-stage matching accomplishes both — anything shaped like
`[word]` or `[word:word]` is treated as a tag attempt and stripped from the
output regardless of whether it's recognized (so a hallucinated
`[proud]` or `[laughs]` never gets read aloud), but only names/values in
the known vocabulary become a `ParsedTag` the director acts on. Anything
that isn't even bracket-shaped (an unclosed `[happy`, or multi-word
bracketed text) is left untouched rather than guessed at.

Call `parse_tags` only on text whose tags are already complete — a chunk
boundary that splits `[hap` / `py]` won't match either half, and both
fragments land in TTS text as literal characters. Buffering partial
`brain.token` chunks until tags (and ideally sentences) are complete is
the caller's job, not this module's; don't rely on leading/trailing
whitespace surviving between chunks either, since whitespace is collapsed
per call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

KNOWN_TAGS = frozenset(
    {"happy", "affection", "curious", "surprise", "confused", "sad", "angry", "thinking", "pause"}
)
KNOWN_LOOK_TARGETS = frozenset({"chat", "you"})

# Broad on purpose: matches any single bracketed word (or word:word) so
# unrecognized-but-tag-shaped tokens still get stripped from spoken text.
_TAG_PATTERN = re.compile(r"\[([a-zA-Z0-9_-]+)(?::([a-zA-Z0-9_-]+))?\]")

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True, slots=True)
class ParsedTag:
    name: str  # "happy", "look", "pause", ... (lowercased)
    value: str | None  # "chat" / "you" for [look:x]; None otherwise
    sentence_index: int  # which sentence (0-based, in the cleaned text) this tag applies to


def parse_tags(text: str) -> tuple[str, list[ParsedTag]]:
    """Extract known tags from `text`, returning `(cleaned_text, tags)`.

    §9's "at most one tag per sentence" and "tags at sentence start" are
    guidance for the LLM's output, not enforced here — this function
    extracts whatever tags are actually present, in order; it's the
    director's job to decide what to do if the model doesn't comply.
    """
    cleaned_parts: list[str] = []
    candidates: list[
        tuple[int, str, str | None]
    ] = []  # (offset in unstripped cleaned text, name, value)
    pos = 0
    cleaned_len = 0
    for match in _TAG_PATTERN.finditer(text):
        segment = text[pos : match.start()]
        cleaned_parts.append(segment)
        cleaned_len += len(segment)
        name = match.group(1).lower()
        value = match.group(2).lower() if match.group(2) else None
        candidates.append((cleaned_len, name, value))
        pos = match.end()
    cleaned_parts.append(text[pos:])
    unstripped_cleaned = "".join(cleaned_parts)
    cleaned_text = re.sub(r"\s+", " ", unstripped_cleaned).strip()

    # A trailing tag (nothing follows it, e.g. "Bye. [happy]") would
    # otherwise point one sentence past the end of split_sentences(cleaned) —
    # an IndexError waiting for the director. Clamp it to the last real
    # sentence instead: a tag with nothing after it applies to what it
    # trails, not to a sentence that doesn't exist.
    max_index = max(len(split_sentences(cleaned_text)) - 1, 0)
    tags = [
        ParsedTag(
            name=name,
            value=value,
            sentence_index=min(_sentence_index_at(unstripped_cleaned, offset), max_index),
        )
        for offset, name, value in candidates
        if _is_known(name, value)
    ]

    return cleaned_text, tags


def split_sentences(text: str) -> list[str]:
    """Split already-cleaned text on sentence boundaries. Shared with
    director.py so both use the same notion of "sentence" as `parse_tags`'
    `sentence_index`.
    """
    return [s for s in _SENTENCE_END.split(text) if s]


def _is_known(name: str, value: str | None) -> bool:
    if name == "look":
        return value in KNOWN_LOOK_TARGETS
    return value is None and name in KNOWN_TAGS


def _sentence_index_at(text: str, offset: int) -> int:
    """How many sentences in `text` have already ended before `offset` —
    equivalently, the 0-based index of the sentence starting at or after
    it, which is the sentence this tag precedes.
    """
    return len(_SENTENCE_END.findall(text[:offset]))
