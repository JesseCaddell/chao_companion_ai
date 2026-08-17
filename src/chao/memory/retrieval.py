"""Per-turn read side of phase 6 memory (design doc §4.4) -- the thing
that makes `brain/turn.py`'s `TurnOrchestrator.memory` non-empty.

Two independent outputs, deliberately kept apart (advisor's review of the
phase 6 design pass, session 12): a short viewer-familiarity line, safe to
fold into the trusted system prompt because it's built only from `login`
(Twitch-constrained to `[a-z0-9_]`) and integers -- and wrapped, untrusted
episode snippets, which must never land in the system prompt and instead
go into `recent_context` as ordinary (wrapped) chat turns, the same trust
boundary live chat already crosses via `prompt.wrap_untrusted`.
"""

from __future__ import annotations

from chao.brain.prompt import Turn, wrap_untrusted
from chao.memory.store import MemoryStore

DEFAULT_EPISODE_LIMIT = 3

_FAMILIARITY_TIERS: tuple[tuple[int, str], ...] = (
    (1, "new here"),
    (4, "someone chao's chatted with a few times"),
    (14, "a familiar face"),
)
_FAMILIARITY_REGULAR = "a regular chao knows well"


def _familiarity_label(days_seen: int) -> str:
    for threshold, label in _FAMILIARITY_TIERS:
        if days_seen <= threshold:
            return label
    return _FAMILIARITY_REGULAR


# Session 12 part 4: affinity is a distinct axis from familiarity above --
# not "how often," but "how well interactions with them have gone," built
# only from score_priority's already-computed tiers (see store.py's module
# docstring for why the update rule stops there for now). This clause is
# additive text only; it must never gate anything -- heart already fires
# on [affection] unconditionally (session 11) at any affinity value,
# including the default 0.0 a brand-new viewer starts at.
_AFFINITY_FOND_THRESHOLD = 0.4
_AFFINITY_WARM_THRESHOLD = 0.15


def _affinity_clause(affinity: float) -> str:
    if affinity >= _AFFINITY_FOND_THRESHOLD:
        return " Chao's especially fond of them."
    if affinity >= _AFFINITY_WARM_THRESHOLD:
        return " Chao enjoys talking with them."
    return ""


async def build_memory(
    store: MemoryStore, *, login: str | None, episode_limit: int = DEFAULT_EPISODE_LIMIT
) -> tuple[str, list[Turn]]:
    """Returns (familiarity_line, retrieved_turns). Empty/empty for a
    first-time chatter (nothing written yet -- `touch_viewer` creates the
    row for *next* time) or a non-chat turn (voice/manual/ambient carry no
    login to look up against -- design doc §4.4's "exact viewer lookup"
    is chat-specific in this slice).
    """
    if login is None:
        return "", []

    summary = await store.viewer_summary(login)
    if summary is None:
        return "", []

    memory = (
        f"{login} is {_familiarity_label(summary.days_seen)} -- "
        f"{summary.interactions} messages across {summary.days_seen} day(s)."
        f"{_affinity_clause(summary.affinity)}"
    )

    rows = await store.recent_episodes(summary.id, episode_limit)
    turns: list[Turn] = []
    for row in reversed(rows):  # oldest first, chronological like real history
        turns.append(
            Turn(role="user", text=wrap_untrusted(row.content, source="chat", speaker=login))
        )
        if row.chao_response:
            turns.append(Turn(role="assistant", text=row.chao_response))

    return memory, turns
