"""Prompt assembly and token budgeting. See design doc §4.3, §5.2.

Pure functions of already-resolved text — no file or DB I/O here, so this
stays testable without fakes for VTS, an LLM, or SQLite. Loading
config/identity.md, retrieving memory, etc. is the caller's job.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from chao.brain.backend import Message, Role

EventSource = Literal["voice", "chat", "manual", "ambient"]

# Chat is the one source CLAUDE.md invariant 6 calls adversarial; everything
# else came from the streamer or the system itself.
_UNTRUSTED_SOURCES = frozenset({"chat"})


def approx_tokens(text: str) -> int:
    """Cheap ~4-chars-per-token estimate for budgeting, not exact
    tokenization — tokenizers are backend-specific and budgeting only needs
    to be in the right ballpark (§5.2).
    """
    return len(text) // 4


@dataclass(frozen=True, slots=True)
class Turn:
    role: Role
    text: str


@dataclass(frozen=True, slots=True)
class CurrentEvent:
    text: str
    source: EventSource
    speaker: str | None = None  # chat display name, if applicable


@dataclass(frozen=True, slots=True)
class Prompt:
    """`stable` (identity + personality) must stay byte-identical across
    turns that don't change personality state — that's what lets cloud
    prompt caching and llama.cpp's KV slot cache actually hit (CLAUDE.md:
    "Keep the first two byte-stable"). Never fold per-turn content (memory,
    context, the current event) into it; `dynamic` and `messages` are where
    that goes instead.
    """

    stable: str
    dynamic: str  # retrieved memory; "" if none
    messages: list[Message]

    @property
    def system(self) -> str:
        if not self.dynamic:
            return self.stable
        return f"{self.stable}\n\n{self.dynamic}"

    @property
    def token_counts(self) -> dict[str, int]:
        """For the `brain.request` event payload (design doc §13)."""
        return {
            "stable": approx_tokens(self.stable),
            "dynamic": approx_tokens(self.dynamic),
            "messages": sum(approx_tokens(m.content) for m in self.messages),
        }


def assemble_prompt(
    *,
    identity: str,
    personality: str,
    memory: str,
    recent_context: Sequence[Turn],
    current_event: CurrentEvent,
    memory_token_budget: int = 800,  # §4.4
    context_token_budget: int = 1500,
) -> Prompt:
    stable = "\n\n".join(p for p in (identity.strip(), personality.strip()) if p)
    dynamic = _cap_memory(memory.strip(), memory_token_budget)

    messages = [
        Message(role=t.role, content=t.text)
        for t in _trim_middle(recent_context, context_token_budget)
    ]
    messages.append(Message(role="user", content=_wrap_event(current_event)))

    return Prompt(stable=stable, dynamic=dynamic, messages=messages)


def _wrap_event(event: CurrentEvent) -> str:
    """CLAUDE.md invariant 6: chat text never enters the system prompt, and
    is explicitly labelled untrusted so the model doesn't treat it as
    instructions, regardless of what config/identity.md does or doesn't say.
    """
    trust = "untrusted" if event.source in _UNTRUSTED_SOURCES else "trusted"
    speaker_attr = f' speaker="{event.speaker}"' if event.speaker else ""
    return (
        f'<message source="{event.source}" trust="{trust}"{speaker_attr}>\n{event.text}\n</message>'
    )


def _cap_memory(memory: str, budget: int) -> str:
    if approx_tokens(memory) <= budget:
        return memory
    # Retrieval is expected to return results already ranked by relevance,
    # so capping from the end keeps the highest-ranked material.
    return memory[: max(budget, 0) * 4].rstrip()


def _trim_middle(turns: Sequence[Turn], budget: int) -> list[Turn]:
    """§5.2: trim conversation history from the middle, never the top —
    keeps the earliest turns and the most recent ones, dropping whatever's
    in between first when a session runs long.
    """
    if sum(approx_tokens(t.text) for t in turns) <= budget:
        return list(turns)

    kept: set[int] = set()
    used = 0
    lo, hi = 0, len(turns) - 1
    take_from_lo = True
    while lo <= hi:
        idx = lo if take_from_lo else hi
        cost = approx_tokens(turns[idx].text)
        if used + cost > budget:
            break
        kept.add(idx)
        used += cost
        if take_from_lo:
            lo += 1
        else:
            hi -= 1
        take_from_lo = not take_from_lo

    return [turns[i] for i in sorted(kept)]
