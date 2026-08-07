from fastapi.testclient import TestClient

from chao.bus import Bus
from chao.dashboard import server as server_module
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


def test_serves_built_frontend_when_dist_present(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html>chao dashboard</html>")
    monkeypatch.setattr(server_module, "WEB_DIST", tmp_path)

    client = TestClient(create_app(Bus()))
    response = client.get("/")

    assert response.status_code == 200
    assert "chao dashboard" in response.text


def test_no_static_mount_when_dist_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "WEB_DIST", tmp_path / "does-not-exist")

    client = TestClient(create_app(Bus()))
    response = client.get("/")

    assert response.status_code == 404
