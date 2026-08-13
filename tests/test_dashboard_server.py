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
        bus.publish(
            Event(kind=Kind.DIRECTOR_EMOTE, payload={"pool": "happy", "hotkey_id": "chao.happy"})
        )
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


def test_subtitles_route_serves_the_overlay_page():
    client = TestClient(create_app(Bus()))

    response = client.get("/subtitles")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "output.speech_start" in response.text


def test_subtitles_route_relays_speech_start_over_the_same_ws():
    """Not a browser test (no JS execution here), but pins the contract the
    page's script depends on: /subtitles and /ws/events share one bus, so a
    speech_start published anywhere shows up on the same socket the overlay
    connects to.
    """
    bus = Bus()
    client = TestClient(create_app(bus))

    with client.websocket_connect("/ws/events") as websocket:
        bus.publish(
            Event(
                kind=Kind.OUTPUT_SPEECH_START,
                turn_id="t1",
                payload={"sentence": "Hello!", "duration_ms": 500.0},
            )
        )
        received = websocket.receive_json()

    assert received["kind"] == Kind.OUTPUT_SPEECH_START
    assert received["payload"]["sentence"] == "Hello!"
