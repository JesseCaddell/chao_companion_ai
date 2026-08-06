"""Event dataclass and the kind vocabulary. See design doc §13.

Every component emits Event objects to the bus. The bus, dashboard,
SQLite writer, and JSONL session log are all consumers of that one stream.
Logging that isn't an Event is invisible to all three (CLAUDE.md invariant 4).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from time import monotonic
from typing import Any


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_id)
    ts: float = field(default_factory=monotonic)
    turn_id: str | None = None


class Kind:
    """String constants, not an Enum: kind must round-trip through JSON
    (websocket to the dashboard, JSONL session log) without a custom encoder,
    and `event.kind == Kind.BRAIN_TOKEN` should work the same as comparing
    against a literal string in tests and fakes.
    """

    INPUT_CHAT = "input.chat"
    INPUT_VOICE = "input.voice"
    INPUT_AMBIENT = "input.ambient"
    INPUT_MANUAL = "input.manual"  # typed text, dashboard-injected — phase 1 stand-in

    DECISION_SELECTED = "decision.selected"
    DECISION_DROPPED = "decision.dropped"

    BRAIN_REQUEST = "brain.request"
    BRAIN_TOKEN = "brain.token"
    BRAIN_COMPLETE = "brain.complete"

    DIRECTOR_TAG = "director.tag"
    DIRECTOR_EMOTE = "director.emote"
    DIRECTOR_MOOD = "director.mood"

    OUTPUT_SPEECH_START = "output.speech_start"
    OUTPUT_SPEECH_END = "output.speech_end"

    VTS_PARAM = "vts.param"
    STATE_FLY = "state.fly"

    ERROR = "error"
