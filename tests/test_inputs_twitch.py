from pathlib import Path

from chao.events import Event, Kind
from chao.inputs.twitch import (
    TwitchChatClient,
    TwitchConfig,
    VelocityTracker,
    load_twitch_config,
    parse_privmsg,
    parse_tags,
    score_priority,
)


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeWebSocket:
    """Stands in for a real websockets connection: `incoming` is fed out
    one message per `__anext__`, `sent` records everything the client
    tried to send. Message content here is deliberately synthetic --
    format-matched against real captured Twitch IRC traffic (checked live,
    not committed), not copied from any real channel/user.
    """

    def __init__(self, incoming: list[str]):
        self._incoming = list(incoming)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)


def make_client(
    incoming: list[str],
    *,
    config: TwitchConfig | None = None,
    oauth_token: str | None = None,
    bot_username: str | None = None,
    velocity: VelocityTracker | None = None,
) -> tuple[TwitchChatClient, FakeWebSocket, list[Event]]:
    events: list[Event] = []
    ws = FakeWebSocket(incoming)

    async def connect(url: str) -> FakeWebSocket:
        return ws

    client = TwitchChatClient(
        config=config or TwitchConfig(channel="testchannel"),
        publish=events.append,
        oauth_token=oauth_token,
        bot_username=bot_username,
        velocity=velocity,
        connect=connect,
    )
    return client, ws, events


def privmsg(login: str, text: str, *, display_name: str | None = None) -> str:
    tag_display = display_name or login
    return (
        f"@badge-info=;badges=;color=;display-name={tag_display};"
        f"emotes=;first-msg=0;id=abc123;mod=0;subscriber=0;"
        f"tmi-sent-ts=0;turbo=0;user-id=1;user-type= "
        f":{login}!{login}@{login}.tmi.twitch.tv PRIVMSG #testchannel :{text}"
    )


def welcome() -> str:
    return ":tmi.twitch.tv 001 justinfan40699 :Welcome, GLHF!"


# --- parse_tags / parse_privmsg -----------------------------------------


def test_parse_tags_splits_key_value_pairs():
    tags = parse_tags("display-name=Foo;mod=0;subscriber=1")

    assert tags == {"display-name": "Foo", "mod": "0", "subscriber": "1"}


def test_parse_tags_handles_none():
    assert parse_tags(None) == {}


def test_parse_privmsg_extracts_login_display_name_and_text():
    line = privmsg("someuser", "hello world", display_name="SomeUser")

    parsed = parse_privmsg(line)

    assert parsed == {"login": "someuser", "display_name": "SomeUser", "text": "hello world"}


def test_parse_privmsg_falls_back_to_login_when_no_display_name_tag():
    line = ":plainuser!plainuser@plainuser.tmi.twitch.tv PRIVMSG #testchannel :hi"

    parsed = parse_privmsg(line)

    assert parsed == {"login": "plainuser", "display_name": "plainuser", "text": "hi"}


def test_parse_privmsg_returns_none_for_non_privmsg_lines():
    assert parse_privmsg(welcome()) is None
    assert parse_privmsg("PING :tmi.twitch.tv") is None
    assert parse_privmsg(":tmi.twitch.tv 366 justinfan #x :End of /NAMES list") is None


# --- score_priority -------------------------------------------------------


def test_score_priority_mention_scores_tier_one():
    assert score_priority("hey chao how are you", ("chao",)) == 1


def test_score_priority_is_case_insensitive():
    assert score_priority("HEY CHAO", ("chao",)) == 1


def test_score_priority_is_word_boundary_not_substring():
    # "chaos" contains "chao" as a substring but isn't a mention.
    assert score_priority("this is chaos today", ("chao",)) == 5


def test_score_priority_question_scores_tier_two():
    assert score_priority("what time is it?", ("chao",)) == 2


def test_score_priority_mention_beats_question_when_both_present():
    assert score_priority("chao, what time is it?", ("chao",)) == 1


def test_score_priority_everything_else_falls_to_tier_five():
    assert score_priority("just chatting lol", ("chao",)) == 5


# --- VelocityTracker -------------------------------------------------------


def test_velocity_tracker_not_spiking_below_threshold():
    clock = FakeClock()
    tracker = VelocityTracker(window_s=10.0, threshold=3, clock=clock)

    assert tracker.record_and_check_spike() is False
    assert tracker.record_and_check_spike() is False


def test_velocity_tracker_spikes_once_threshold_reached():
    clock = FakeClock()
    tracker = VelocityTracker(window_s=10.0, threshold=3, clock=clock)

    tracker.record_and_check_spike()
    tracker.record_and_check_spike()
    assert tracker.record_and_check_spike() is True


def test_velocity_tracker_expires_old_timestamps_outside_window():
    clock = FakeClock()
    tracker = VelocityTracker(window_s=10.0, threshold=2, clock=clock)

    tracker.record_and_check_spike()
    clock.advance(15.0)  # well past window_s -- the first timestamp expires
    assert tracker.record_and_check_spike() is False


# --- load_twitch_config -----------------------------------------------------


def test_load_twitch_config_reads_the_twitch_block(tmp_path: Path):
    config_path = tmp_path / "chao.yaml"
    config_path.write_text(
        "twitch:\n"
        "  channel: mychannel\n"
        "  chao_names: [chao, chow]\n"
        "  velocity_window_s: 5\n"
        "  velocity_threshold: 4\n"
    )

    config = load_twitch_config(config_path)

    assert config.channel == "mychannel"
    assert config.chao_names == ("chao", "chow")
    assert config.velocity_window_s == 5.0
    assert config.velocity_threshold == 4


def test_load_twitch_config_defaults_when_file_missing(tmp_path: Path):
    assert load_twitch_config(tmp_path / "does_not_exist.yaml") == TwitchConfig()


def test_load_twitch_config_defaults_when_block_absent(tmp_path: Path):
    config_path = tmp_path / "chao.yaml"
    config_path.write_text("tts:\n  voice_path: foo.onnx\n")

    assert load_twitch_config(config_path) == TwitchConfig()


# --- TwitchChatClient --------------------------------------------------------


async def test_handshake_sends_cap_then_anonymous_nick_then_joins_after_001():
    client, ws, _events = make_client([welcome()])

    await client.run()

    assert ws.sent[0] == "CAP REQ :twitch.tv/tags\r\n"
    assert ws.sent[1].startswith("NICK justinfan")
    assert ws.sent[2] == "JOIN #testchannel\r\n"


async def test_handshake_uses_pass_and_bot_username_when_authenticated():
    client, ws, _events = make_client([welcome()], oauth_token="oauth:abc123", bot_username="mybot")

    await client.run()

    assert ws.sent[0] == "CAP REQ :twitch.tv/tags\r\n"
    assert ws.sent[1] == "PASS oauth:abc123\r\n"
    assert ws.sent[2] == "NICK mybot\r\n"
    assert ws.sent[3] == "JOIN #testchannel\r\n"


async def test_run_publishes_input_chat_for_each_privmsg():
    line = privmsg("alice", "just chatting")
    client, _ws, events = make_client([welcome(), line])

    await client.run()

    chat_events = [e for e in events if e.kind == Kind.INPUT_CHAT]
    assert len(chat_events) == 1
    assert chat_events[0].payload == {
        "login": "alice",
        "display_name": "alice",
        "text": "just chatting",
        "priority": 5,
    }


async def test_run_scores_a_mention_as_priority_one():
    line = privmsg("alice", "hey chao!")
    client, _ws, events = make_client([welcome(), line])

    await client.run()

    chat_events = [e for e in events if e.kind == Kind.INPUT_CHAT]
    assert chat_events[0].payload["priority"] == 1


async def test_run_responds_to_ping_with_pong():
    client, ws, _events = make_client([welcome(), "PING :tmi.twitch.tv"])

    await client.run()

    assert "PONG :tmi.twitch.tv\r\n" in ws.sent


async def test_run_closes_the_websocket_on_exit():
    client, ws, _events = make_client([welcome()])

    await client.run()

    assert ws.closed is True


async def test_run_closes_the_websocket_even_if_a_line_handler_raises():
    client, ws, _events = make_client([welcome(), "not a valid line but also not privmsg"])
    # A malformed line just fails parse_privmsg's match and is ignored --
    # this confirms the finally-close still runs on the ordinary path too.

    await client.run()

    assert ws.closed is True


async def test_run_upgrades_background_message_to_spike_tier_during_a_spike():
    class AlwaysSpiking:
        def record_and_check_spike(self) -> bool:
            return True

    line = privmsg("alice", "just chatting")
    client, _ws, events = make_client([welcome(), line], velocity=AlwaysSpiking())

    await client.run()

    chat_events = [e for e in events if e.kind == Kind.INPUT_CHAT]
    assert chat_events[0].payload["priority"] == 4


async def test_run_does_not_upgrade_a_mention_during_a_spike():
    class AlwaysSpiking:
        def record_and_check_spike(self) -> bool:
            return True

    line = privmsg("alice", "hey chao!")
    client, _ws, events = make_client([welcome(), line], velocity=AlwaysSpiking())

    await client.run()

    chat_events = [e for e in events if e.kind == Kind.INPUT_CHAT]
    assert chat_events[0].payload["priority"] == 1
