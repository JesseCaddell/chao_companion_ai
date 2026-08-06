import dataclasses

import pytest

from chao.events import Event, Kind, new_id


def test_event_defaults_are_populated():
    event = Event(kind=Kind.INPUT_MANUAL, payload={"text": "hi"})

    assert event.kind == "input.manual"
    assert event.payload == {"text": "hi"}
    assert event.turn_id is None
    assert event.id
    assert event.ts > 0


def test_event_ids_are_unique():
    assert new_id() != new_id()


def test_event_is_frozen():
    event = Event(kind=Kind.ERROR)
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.kind = Kind.BRAIN_TOKEN  # type: ignore[misc]
