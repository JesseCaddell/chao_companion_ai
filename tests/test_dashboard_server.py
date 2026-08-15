import time

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


def test_set_backend_command_calls_the_setter():
    calls: list[str] = []
    bus = Bus()
    client = TestClient(create_app(bus, set_backend=calls.append))

    with client.websocket_connect("/ws/events") as websocket:
        websocket.send_json({"type": "set_backend", "backend": "cloud"})
        time.sleep(0.05)

    assert calls == ["cloud"]


def test_set_backend_ignores_unrecognized_backend_value():
    calls: list[str] = []
    bus = Bus()
    client = TestClient(create_app(bus, set_backend=calls.append))

    with client.websocket_connect("/ws/events") as websocket:
        websocket.send_json({"type": "set_backend", "backend": "gpt5"})
        time.sleep(0.05)

    assert calls == []


def test_set_free_speech_command_calls_the_setter():
    calls: list[tuple[bool, float]] = []
    bus = Bus()
    client = TestClient(create_app(bus, set_free_speech=lambda e, i: calls.append((e, i))))

    with client.websocket_connect("/ws/events") as websocket:
        websocket.send_json({"type": "set_free_speech", "enabled": True, "interval_s": 45})
        time.sleep(0.05)

    assert calls == [(True, 45.0)]


def test_malformed_free_speech_interval_does_not_crash_or_call_setter():
    calls: list[tuple[bool, float]] = []
    bus = Bus()
    client = TestClient(create_app(bus, set_free_speech=lambda e, i: calls.append((e, i))))

    with client.websocket_connect("/ws/events") as websocket:
        websocket.send_json({"type": "set_free_speech", "enabled": True, "interval_s": "soon"})
        # Connection must still be alive -- a real event still relays.
        bus.publish(Event(kind=Kind.DIRECTOR_TAG, payload={"tag": "happy"}))
        received = websocket.receive_json()

    assert calls == []
    assert received["kind"] == Kind.DIRECTOR_TAG


def test_commands_are_ignored_when_no_setter_was_provided():
    """create_app(bus) with no setters (every pre-session-11 call site)
    must not crash on an incoming command -- it just has nothing to do.
    """
    bus = Bus()
    client = TestClient(create_app(bus))

    with client.websocket_connect("/ws/events") as websocket:
        websocket.send_json({"type": "set_backend", "backend": "cloud"})
        bus.publish(Event(kind=Kind.DIRECTOR_TAG, payload={"tag": "happy"}))
        received = websocket.receive_json()

    assert received["kind"] == Kind.DIRECTOR_TAG


def test_unrecognized_command_type_is_ignored():
    calls: list[str] = []
    bus = Bus()
    client = TestClient(create_app(bus, set_backend=calls.append))

    with client.websocket_connect("/ws/events") as websocket:
        websocket.send_json({"type": "something_else"})
        bus.publish(Event(kind=Kind.DIRECTOR_TAG, payload={"tag": "happy"}))
        received = websocket.receive_json()

    assert calls == []
    assert received["kind"] == Kind.DIRECTOR_TAG
