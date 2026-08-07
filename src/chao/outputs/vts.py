"""Minimal VTube Studio plugin API client: connection, request/response, and token auth.

Deliberately thin. What the client should look like long-term is determined by what
the phase 0 probe (chao.tools.vts_probe) discovers about the model's parameters --
premature structure here would just get thrown away.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import websockets

from chao.director.director import EmoteConfig
from chao.events import Event, Kind

logger = logging.getLogger(__name__)

API_NAME = "VTubeStudioPublicAPI"
API_VERSION = "1.0"
PLUGIN_NAME = "Chao Companion"
PLUGIN_DEVELOPER = "Jesse Caddell"

TOKEN_PATH = Path("data") / "vts_token.txt"


class VTSAPIError(RuntimeError):
    def __init__(self, error_id: int, message: str):
        super().__init__(f"VTS APIError {error_id}: {message}")
        self.error_id = error_id
        self.message = message


class VTSClient:
    def __init__(self, url: str = "ws://localhost:8001"):
        self.url = url
        self._ws: websockets.ClientConnection | None = None
        self._req_id = 0

    async def connect(self) -> None:
        self._ws = await websockets.connect(self.url)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def request(self, message_type: str, data: dict | None = None) -> dict:
        self._req_id += 1
        payload = {
            "apiName": API_NAME,
            "apiVersion": API_VERSION,
            "requestID": f"chao-{self._req_id}",
            "messageType": message_type,
            "data": data or {},
        }
        await self._ws.send(json.dumps(payload))
        response = json.loads(await self._ws.recv())
        if response.get("messageType") == "APIError":
            err = response["data"]
            raise VTSAPIError(err["errorID"], err["message"])
        return response

    async def authenticate(self) -> None:
        """Two-step token auth. First run pops an in-app Allow dialog in VTS --
        if the probe seems to hang, check VTS isn't waiting on another monitor."""
        token = TOKEN_PATH.read_text().strip() if TOKEN_PATH.exists() else None

        if token is None:
            resp = await self.request(
                "AuthenticationTokenRequest",
                {"pluginName": PLUGIN_NAME, "pluginDeveloper": PLUGIN_DEVELOPER},
            )
            token = resp["data"]["authenticationToken"]
            TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_PATH.write_text(token)

        resp = await self.request(
            "AuthenticationRequest",
            {
                "pluginName": PLUGIN_NAME,
                "pluginDeveloper": PLUGIN_DEVELOPER,
                "authenticationToken": token,
            },
        )
        if not resp["data"]["authenticated"]:
            # Cached token was rejected (e.g. VTS restarted or user revoked it).
            TOKEN_PATH.unlink(missing_ok=True)
            raise RuntimeError(
                f"Authentication rejected: {resp['data']['reason']}. "
                "Cached token cleared -- run again to re-authenticate."
            )


class VTSTransport(Protocol):
    """Just the shape VTSEmoteSubscriber actually calls -- lets tests inject
    a fake instead of a real VTSClient, matching brain/backend.py's
    LLMBackend Protocol pattern.
    """

    async def request(self, message_type: str, data: dict | None = None) -> dict: ...


def _default_publish(_event: Event) -> None:
    return None


@dataclass
class VTSEmoteSubscriber:
    """Turns `director.emote` events into real ExpressionActivationRequest
    calls (CLAUDE.md: discrete channel, explicit active state -- not
    HotkeyTriggerRequest, which toggles blindly). `hotkey_id` in the event
    payload is actually the expression FILE name (see config/emotes.yaml's
    header) despite the field name; that's what gets sent as
    `expressionFile`.

    Firing is immediate (no TTS yet to schedule against -- same seam noted
    in director.py). `duration_s` comes from `emote_config`, not the event,
    since Director only publishes pool/hotkey_id/reason. CLAUDE.md: "never
    sustain an authored emote beyond ~4s" -- enforced here by scheduling a
    deactivation after `duration_s`, not by the caller.

    Deactivation runs as a background task so `handle()` returns immediately
    and doesn't block the consumer loop from processing the next emote
    (which may target a different, unrelated expression file) for the
    `duration_s` of this one. If a second activation for the *same*
    expression file arrives before the first's deactivation has fired, the
    pending deactivate is cancelled and rescheduled -- extends the hold
    instead of a stale deactivate cutting the new activation short. In
    practice every configured pool has `cooldown_s >= duration_s`, so this
    shouldn't happen with a single hotkey per pool, but it's cheap to get
    right regardless of config changes.
    """

    client: VTSTransport
    emote_config: EmoteConfig
    publish: Callable[[Event], None] = _default_publish
    fade_time_s: float = 0.25
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    _deactivate_tasks: dict[str, asyncio.Task] = field(default_factory=dict, init=False)

    async def handle(self, event: Event) -> None:
        if event.kind != Kind.DIRECTOR_EMOTE:
            return
        expression_file = event.payload["hotkey_id"]
        pool = self.emote_config.pools.get(event.payload["pool"])
        duration_s = pool.duration_s if pool is not None else 0.0
        await self._activate(expression_file, duration_s)

    async def _activate(self, expression_file: str, duration_s: float) -> None:
        pending = self._deactivate_tasks.pop(expression_file, None)
        if pending is not None:
            pending.cancel()

        activated = await self._send(expression_file, active=True)
        if not activated:
            return
        self._deactivate_tasks[expression_file] = asyncio.create_task(
            self._deactivate_after(expression_file, duration_s)
        )

    async def _deactivate_after(self, expression_file: str, duration_s: float) -> None:
        await self.sleep(duration_s)
        await self._send(expression_file, active=False)
        self._deactivate_tasks.pop(expression_file, None)

    async def _send(self, expression_file: str, *, active: bool) -> bool:
        try:
            await self.client.request(
                "ExpressionActivationRequest",
                {"expressionFile": expression_file, "active": active, "fadeTime": self.fade_time_s},
            )
        except Exception:
            logger.exception("VTS request failed for %s (active=%s)", expression_file, active)
            self.publish(
                Event(
                    kind=Kind.ERROR,
                    payload={
                        "component": "vts",
                        "message": f"expression activation failed: {expression_file}",
                    },
                )
            )
            return False
        return True
