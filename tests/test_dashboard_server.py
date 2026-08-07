from fastapi.testclient import TestClient

from chao.bus import Bus
from chao.dashboard.server import create_app
from chao.events import Event, Kind


def test_events_ws_relays_published_events():
    bus = Bus()
    app = create_app(bus)
    client = TestClient(app)

    with client.websocket_connect("/ws/events") as websocket:
        bus.publish(Event(kind=Kind.DIRECTOR_EMOTE, payload={"pool": "happy", "hotkey_id": "chao.happy"}))
        received = websocket.receive_json()

    assert received["kind"] == Kind.DIRECTOR_EMOTE
    assert received["payload"] == {"pool": "happy", "hotkey_id": "chao.happy"}


def test_disconnect_unsubscribes_from_bus():
    bus = Bus()
    app = create_app(bus)
    client = TestClient(app)

    with client.websocket_connect("/ws/events"):
        assert len(bus._subscribers) == 1

    assert len(bus._subscribers) == 0
