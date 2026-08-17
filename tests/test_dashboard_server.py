import time

from fastapi.testclient import TestClient

from chao.bus import Bus
from chao.dashboard import server as server_module
from chao.dashboard.server import DashboardCommands, create_app
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
    client = TestClient(create_app(bus, DashboardCommands(set_backend=calls.append)))

    with client.websocket_connect("/ws/events") as websocket:
        websocket.send_json({"type": "set_backend", "backend": "cloud"})
        time.sleep(0.05)

    assert calls == ["cloud"]


def test_set_backend_ignores_unrecognized_backend_value():
    calls: list[str] = []
    bus = Bus()
    client = TestClient(create_app(bus, DashboardCommands(set_backend=calls.append)))

    with client.websocket_connect("/ws/events") as websocket:
        websocket.send_json({"type": "set_backend", "backend": "gpt5"})
        time.sleep(0.05)

    assert calls == []


def test_set_free_speech_command_calls_the_setter():
    calls: list[tuple[bool, float]] = []
    bus = Bus()
    commands = DashboardCommands(set_free_speech=lambda e, i: calls.append((e, i)))
    client = TestClient(create_app(bus, commands))

    with client.websocket_connect("/ws/events") as websocket:
        websocket.send_json({"type": "set_free_speech", "enabled": True, "interval_s": 45})
        time.sleep(0.05)

    assert calls == [(True, 45.0)]


def test_malformed_free_speech_interval_does_not_crash_or_call_setter():
    calls: list[tuple[bool, float]] = []
    bus = Bus()
    commands = DashboardCommands(set_free_speech=lambda e, i: calls.append((e, i)))
    client = TestClient(create_app(bus, commands))

    with client.websocket_connect("/ws/events") as websocket:
        websocket.send_json({"type": "set_free_speech", "enabled": True, "interval_s": "soon"})
        # Connection must still be alive -- a real event still relays.
        bus.publish(Event(kind=Kind.DIRECTOR_TAG, payload={"tag": "happy"}))
        received = websocket.receive_json()

    assert calls == []
    assert received["kind"] == Kind.DIRECTOR_TAG


def test_commands_are_ignored_when_no_commands_object_was_provided():
    """create_app(bus) with no DashboardCommands (every pre-session-11 call
    site) must not crash on an incoming command -- it just has nothing to
    do.
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
    client = TestClient(create_app(bus, DashboardCommands(set_backend=calls.append)))

    with client.websocket_connect("/ws/events") as websocket:
        websocket.send_json({"type": "something_else"})
        bus.publish(Event(kind=Kind.DIRECTOR_TAG, payload={"tag": "happy"}))
        received = websocket.receive_json()

    assert calls == []
    assert received["kind"] == Kind.DIRECTOR_TAG


def test_fire_emote_command_calls_the_setter():
    calls: list[str] = []
    bus = Bus()
    client = TestClient(create_app(bus, DashboardCommands(fire_emote=calls.append)))

    with client.websocket_connect("/ws/events") as websocket:
        websocket.send_json({"type": "fire_emote", "pool": "happy_eyes"})
        time.sleep(0.05)

    assert calls == ["happy_eyes"]


def test_fire_emote_ignores_missing_pool():
    calls: list[str] = []
    bus = Bus()
    client = TestClient(create_app(bus, DashboardCommands(fire_emote=calls.append)))

    with client.websocket_connect("/ws/events") as websocket:
        websocket.send_json({"type": "fire_emote"})
        time.sleep(0.05)

    assert calls == []


def test_force_mood_command_calls_the_setter():
    calls: list[tuple[float, float]] = []
    bus = Bus()
    commands = DashboardCommands(force_mood=lambda v, a: calls.append((v, a)))
    client = TestClient(create_app(bus, commands))

    with client.websocket_connect("/ws/events") as websocket:
        websocket.send_json({"type": "force_mood", "valence": 0.8, "arousal": -0.2})
        time.sleep(0.05)

    assert calls == [(0.8, -0.2)]


def test_force_mood_malformed_value_does_not_crash_or_call_setter():
    calls: list[tuple[float, float]] = []
    bus = Bus()
    commands = DashboardCommands(force_mood=lambda v, a: calls.append((v, a)))
    client = TestClient(create_app(bus, commands))

    with client.websocket_connect("/ws/events") as websocket:
        websocket.send_json({"type": "force_mood", "valence": "high", "arousal": 0.5})
        bus.publish(Event(kind=Kind.DIRECTOR_TAG, payload={"tag": "happy"}))
        received = websocket.receive_json()

    assert calls == []
    assert received["kind"] == Kind.DIRECTOR_TAG


def test_kill_command_calls_bus_kill_directly():
    """kill/revive don't go through DashboardCommands -- bus.kill()/
    bus.revive() are already public, and _handle_command already has bus
    in scope.
    """
    bus = Bus()
    client = TestClient(create_app(bus))

    with client.websocket_connect("/ws/events") as websocket:
        websocket.send_json({"type": "kill"})
        time.sleep(0.05)

    assert bus._killed.is_set()


def test_revive_command_calls_bus_revive_directly():
    bus = Bus()
    bus.kill()
    client = TestClient(create_app(bus))

    with client.websocket_connect("/ws/events") as websocket:
        websocket.send_json({"type": "revive"})
        time.sleep(0.05)

    assert not bus._killed.is_set()
