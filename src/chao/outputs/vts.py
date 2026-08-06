"""Minimal VTube Studio plugin API client: connection, request/response, and token auth.

Deliberately thin. What the client should look like long-term is determined by what
the phase 0 probe (chao.tools.vts_probe) discovers about the model's parameters --
premature structure here would just get thrown away.
"""

from __future__ import annotations

import json
from pathlib import Path

import websockets

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
