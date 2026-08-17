"""Raw Twitch IRC chat client (design doc §4.1). Connects over websockets
to Twitch's IRC-over-websocket gateway, parses PRIVMSG lines, scores each
message's priority per §4.1's table, and publishes Kind.INPUT_CHAT events.

Raw IRC, not `twitchio` -- `twitchio` 2.x/3.x use incompatible auth flows
with no way to pin which one `uv add twitchio` would resolve to; raw IRC
needs no new dependency (`websockets` is already here for VTS) and matches
this project's established "read the raw protocol" precedent.

Anonymous read (`justinfanNNNNN`, no password) is the default -- this
build is chat *input* only, no send-back, so no bot account/OAuth token
is required. Set `TWITCH_OAUTH_TOKEN`/`TWITCH_BOT_USERNAME` in the
environment to switch to an authenticated connection if/when send-back is
ever built.

Handshake order was verified against a real, busy channel (session 10
part 10) before this was written, not assumed: CAP REQ, then PASS/NICK,
THEN wait for the server's 001 welcome line before JOINing -- joining
before 001 can be silently ignored. The PRIVMSG regex below was likewise
checked against real captured traffic, not hand-written blind.

Session 11 hardening: Twitch's own `RECONNECT` notice is now handled here
(`run()` ends the connection cleanly instead of silently dropping the
line and reading a now-dead socket). Reconnect-with-backoff after *any*
drop -- this notice, a network blip, or an outright connection failure --
lives in `__main__.py`'s `_run_twitch`, one layer up, so it can use the
same print-and-continue degrade shape as everywhere else in that file
instead of importing that concern in here.
"""

from __future__ import annotations

import random
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import websockets
import yaml

from chao.events import Event, Kind

IRC_URL = "wss://irc-ws.chat.twitch.tv:443"

# Verified against real Twitch IRC traffic (session 10 part 10), not
# hand-written blind -- both the tag block and the login/PRIVMSG shape
# match what a live, busy channel actually sends.
_PRIVMSG_RE = re.compile(r"^(?:@(?P<tags>\S+) )?:(?P<login>[^!]+)!\S+ PRIVMSG #\S+ :(?P<text>.*)$")

# Exact wire format per Twitch's IRC docs -- unlike PRIVMSG this has no
# variable fields to parse, so a straight equality check (same style as
# the handshake's `" 001 " in line` check) is enough.
_RECONNECT_LINE = ":tmi.twitch.tv RECONNECT"


def parse_tags(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    tags: dict[str, str] = {}
    for pair in raw.split(";"):
        key, _, value = pair.partition("=")
        tags[key] = value
    return tags


def parse_privmsg(line: str) -> dict[str, str] | None:
    """Returns None for any line that isn't a chat message -- PING, the
    welcome sequence, JOIN/NAMES confirmations, etc. all fail this match
    and are handled (or ignored) by the caller instead.
    """
    match = _PRIVMSG_RE.match(line)
    if match is None:
        return None
    tags = parse_tags(match.group("tags"))
    return {
        "login": match.group("login"),
        "display_name": tags.get("display-name") or match.group("login"),
        "text": match.group("text"),
    }


def score_priority(text: str, chao_names: tuple[str, ...]) -> int:
    """Static per-message scoring, design doc §4.1 tiers 1/2/5. Tier 3
    (high-affinity viewer) needs the affinity system (phase 6, not built
    yet) to score at all -- folded into tier 5 until then, a documented
    gap rather than a guess. Tier 4 (velocity spike) isn't a property of
    a single message -- see VelocityTracker below; the caller applies it.

    Word-boundary matched, not substring -- `"chao" in "chaos"` would
    otherwise false-positive on ordinary English words containing the name.
    """
    lowered = text.lower()
    if any(re.search(rf"\b{re.escape(name)}\b", lowered) for name in chao_names):
        return 1  # mention
    if "?" in text:
        return 2  # question
    return 5  # everything else (tier 3/4 not distinguishable from text alone)


@dataclass
class VelocityTracker:
    """§4.1 tier 4: a rolling count of messages in the last `window_s`.
    Stateful and per-connection, unlike `score_priority` -- "is chat
    currently busy" isn't a property of one message. Untestable against
    real traffic on a quiet/no-viewer channel; stated here rather than
    left as a knob that looks verified.
    """

    window_s: float
    threshold: int
    clock: Callable[[], float] = time.monotonic
    _timestamps: deque[float] = field(default_factory=deque, init=False)

    def record_and_check_spike(self) -> bool:
        now = self.clock()
        self._timestamps.append(now)
        while self._timestamps and now - self._timestamps[0] > self.window_s:
            self._timestamps.popleft()
        return len(self._timestamps) >= self.threshold


@dataclass(frozen=True, slots=True)
class TwitchConfig:
    channel: str = ""
    chao_names: tuple[str, ...] = ("chao",)
    velocity_window_s: float = 10.0
    velocity_threshold: int = 8


def load_twitch_config(path: Path) -> TwitchConfig:
    """Same load/assemble split as every other `*_config` in this project.
    Reads the `twitch:` block out of `config/chao.yaml`. `channel: ""`
    (the default) disables chat input entirely -- same degrade-not-crash
    shape as `tts.voice_path` unset.
    """
    data = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    raw = data.get("twitch") or {}
    defaults = TwitchConfig()
    names = raw.get("chao_names", list(defaults.chao_names))
    return TwitchConfig(
        channel=str(raw.get("channel", defaults.channel)),
        chao_names=tuple(str(n).lower() for n in names),
        velocity_window_s=float(raw.get("velocity_window_s", defaults.velocity_window_s)),
        velocity_threshold=int(raw.get("velocity_threshold", defaults.velocity_threshold)),
    )


class _ReconnectRequested(Exception):
    """Internal signal only, never escapes `run()`. Twitch sends a bare
    `:tmi.twitch.tv RECONNECT` line shortly before it cycles the server
    out from under a connection (planned maintenance, load rebalancing) --
    it's not an error, just an early warning to reconnect proactively
    instead of waiting to notice the socket is dead. Raised from
    `_handle_line` (deep inside the read loop) and caught in `run()`,
    since that's the only place that can end the loop and close cleanly.
    """


class _WebSocketLike(Protocol):
    """Just the shape this client actually calls -- lets tests inject a
    fake instead of a real websocket connection, same pattern as
    `outputs/vts.py`'s `VTSTransport`.
    """

    async def send(self, message: str) -> None: ...
    async def close(self) -> None: ...
    def __aiter__(self) -> AsyncIterator[str]: ...


class TwitchChatClient:
    def __init__(
        self,
        *,
        config: TwitchConfig,
        publish: Callable[[Event], None],
        url: str = IRC_URL,
        oauth_token: str | None = None,
        bot_username: str | None = None,
        velocity: VelocityTracker | None = None,
        rng: random.Random | None = None,
        connect: Callable[[str], Awaitable[_WebSocketLike]] = websockets.connect,
    ) -> None:
        self.config = config
        self.publish = publish
        self.url = url
        self.oauth_token = oauth_token
        self.bot_username = bot_username
        self.velocity = velocity or VelocityTracker(
            config.velocity_window_s, config.velocity_threshold
        )
        self.rng = rng or random.Random()
        self._connect = connect
        self._ws: _WebSocketLike | None = None

    async def run(self) -> None:
        """Connects, completes the handshake, then publishes
        `Kind.INPUT_CHAT` for every PRIVMSG until the connection drops,
        Twitch sends a `RECONNECT` notice, or this task is cancelled. No
        retry loop of its own -- see module docstring, that lives one
        layer up in `__main__.py`. A `RECONNECT` notice ends this method
        the same way a clean connection close would (no exception); a
        real drop (network failure) still raises, same as before.
        """
        self._ws = await self._connect(self.url)
        try:
            await self._handshake()
            async for raw in self._ws:
                for line in raw.split("\r\n"):
                    if line:
                        await self._handle_line(line)
        except _ReconnectRequested:
            pass
        finally:
            await self._ws.close()

    async def _handshake(self) -> None:
        assert self._ws is not None
        await self._ws.send("CAP REQ :twitch.tv/tags\r\n")
        if self.oauth_token and self.bot_username:
            await self._ws.send(f"PASS {self.oauth_token}\r\n")
            await self._ws.send(f"NICK {self.bot_username}\r\n")
        else:
            nick = f"justinfan{self.rng.randint(10000, 99999)}"
            await self._ws.send(f"NICK {nick}\r\n")

        # Twitch can silently ignore a JOIN sent before it's finished the
        # connection-registration sequence (verified live) -- wait for 001
        # (RPL_WELCOME) before joining. Falls through and joins anyway if
        # the connection ends before 001 ever arrives, rather than hanging
        # forever.
        async for raw in self._ws:
            if any(" 001 " in line for line in raw.split("\r\n")):
                break
        await self._ws.send(f"JOIN #{self.config.channel}\r\n")

    async def _handle_line(self, line: str) -> None:
        assert self._ws is not None
        if line.startswith("PING"):
            await self._ws.send(line.replace("PING", "PONG", 1) + "\r\n")
            return
        if line == _RECONNECT_LINE:
            raise _ReconnectRequested()

        parsed = parse_privmsg(line)
        if parsed is None:
            return

        is_spike = self.velocity.record_and_check_spike()
        priority = score_priority(parsed["text"], self.config.chao_names)
        if priority == 5 and is_spike:
            priority = 4

        self.publish(
            Event(
                kind=Kind.INPUT_CHAT,
                payload={
                    "login": parsed["login"],
                    "display_name": parsed["display_name"],
                    "text": parsed["text"],
                    "priority": priority,
                },
            )
        )
