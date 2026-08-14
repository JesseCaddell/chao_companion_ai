"""Minimal VTube Studio plugin API client: connection, request/response, and token auth.

Deliberately thin. What the client should look like long-term is determined by what
the phase 0 probe (chao.tools.vts_probe) discovers about the model's parameters --
premature structure here would just get thrown away.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import websockets

from chao.director.aliveness import FlyConfig
from chao.director.director import EmoteConfig
from chao.director.idle_drift import IdleDrift
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
        # Send-then-recv with no requestID correlation is only safe with
        # exactly one request in flight at a time. Every caller used to get
        # that for free because _run_vts_subscriber drove emote/fly
        # handling sequentially in one loop -- session 10's motion player
        # breaks that assumption (injection runs concurrently with speech,
        # and emotes can fire mid-sentence by design), so this lock is load
        # -bearing, not defensive. A ~30Hz injection loop measured against
        # the real API (SESSION_STATE.md session 10) has enough headroom
        # to absorb occasional contention from a concurrent emote/fly call.
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._ws = await websockets.connect(self.url)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def request(self, message_type: str, data: dict | None = None) -> dict:
        async with self._lock:
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

    **Global mutual exclusion, one active expression at a time (session 10
    part 13, user-reported live)**: `director.py`'s cooldown is tracked
    per-pool, and `aliveness.py`'s reactions (anticipation, chat_spike)
    don't share that state at all (by its own design) -- nothing previously
    stopped two *different* pools from both being active in VTS
    simultaneously, e.g. `curious` firing while `confused` is still
    holding. Confirmed live: two emotes visibly clashed, and since each
    pool's expression file bundles whatever it bundles (eyes included --
    this build doesn't actually split eyes/ball into independently
    addressable channels the way CLAUDE.md's expression model describes,
    despite one hotkey file per pool), the eyes doubled up the same way.
    Fixed by tracking the single currently-active expression file
    (`_active_expression`) and deactivating it first whenever a *different*
    file activates -- same-file re-activation (extending an already-active
    emote's hold) is unaffected.
    """

    client: VTSTransport
    emote_config: EmoteConfig
    publish: Callable[[Event], None] = _default_publish
    fade_time_s: float = 0.25
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    _deactivate_tasks: dict[str, asyncio.Task] = field(default_factory=dict, init=False)
    _active_expression: str | None = field(default=None, init=False)

    async def handle(self, event: Event) -> None:
        if event.kind != Kind.DIRECTOR_EMOTE:
            return
        expression_file = event.payload["hotkey_id"]
        pool = self.emote_config.pools.get(event.payload["pool"])
        duration_s = pool.duration_s if pool is not None else 0.0
        await self._activate(expression_file, duration_s)

    async def _activate(self, expression_file: str, duration_s: float) -> None:
        if self._active_expression is not None and self._active_expression != expression_file:
            await self._deactivate_now(self._active_expression)

        pending = self._deactivate_tasks.pop(expression_file, None)
        if pending is not None:
            pending.cancel()

        activated = await self._send(expression_file, active=True)
        if not activated:
            return
        self._active_expression = expression_file
        self._deactivate_tasks[expression_file] = asyncio.create_task(
            self._deactivate_after(expression_file, duration_s)
        )

    async def _deactivate_now(self, expression_file: str) -> None:
        pending = self._deactivate_tasks.pop(expression_file, None)
        if pending is not None:
            pending.cancel()
        await self._send(expression_file, active=False)
        if self._active_expression == expression_file:
            self._active_expression = None

    async def _deactivate_after(self, expression_file: str, duration_s: float) -> None:
        await self.sleep(duration_s)
        await self._send(expression_file, active=False)
        self._deactivate_tasks.pop(expression_file, None)
        if self._active_expression == expression_file:
            self._active_expression = None

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


@dataclass
class VTSFlySubscriber:
    """Turns `state.fly` events into `MoveModelRequest` calls (horizontal
    screen positioning, session 9) plus an `ExpressionActivationRequest`
    toggle for the `fly` visual itself.

    A live probe (SESSION_STATE.md session 9) confirmed `MoveModelRequest`
    interpolates smoothly over `timeInSeconds` rather than snapping — so
    this is genuinely "send one request per destination," no tween loop,
    no motion.py involved.

    The `fly` expression is handled differently from `VTSEmoteSubscriber`'s
    pool emotes: it's a sustained toggle (§6.1 lists it as its own `state`
    channel, not a transient eyes/ball emote), so it's activated with no
    scheduled auto-deactivation and only turned off when `flying` goes
    False — CLAUDE.md's "never sustain an authored emote beyond ~4s" is
    about the transient pool emotes, not this persistent state toggle.

    Caches the model's baseline position (Y/rotation/size, and the resting
    X) from a single `CurrentModelRequest` the first time a `state.fly`
    event arrives — not at construction, so a client that hasn't connected
    yet (or a model that hasn't loaded) doesn't fail subscriber setup, only
    the first fly transition. `MoveModelRequest` takes the full position,
    not a delta, so Y/rotation/size are re-sent unchanged on every move.
    """

    client: VTSTransport
    fly_config: FlyConfig
    publish: Callable[[Event], None] = _default_publish

    _baseline: dict | None = field(default=None, init=False)

    async def handle(self, event: Event) -> None:
        if event.kind != Kind.STATE_FLY:
            return

        flying = bool(event.payload.get("flying"))
        target_x = event.payload.get("target_x")
        await self._move(flying=flying, target_x=target_x)
        await self._toggle_expression(active=flying)

    async def _move(self, *, flying: bool, target_x: float | None) -> None:
        baseline = await self._ensure_baseline()
        if baseline is None:
            return

        position_x = target_x if (flying and target_x is not None) else baseline["positionX"]
        duration = self.fly_config.move_duration_s if flying else self.fly_config.land_duration_s

        try:
            await self.client.request(
                "MoveModelRequest",
                {
                    "timeInSeconds": duration,
                    "valuesAreRelativeToModel": False,
                    "positionX": position_x,
                    "positionY": baseline["positionY"],
                    "rotation": baseline["rotation"],
                    "size": baseline["size"],
                },
            )
        except Exception:
            logger.exception(
                "VTS MoveModelRequest failed (flying=%s, target_x=%s)", flying, target_x
            )
            self.publish(
                Event(
                    kind=Kind.ERROR,
                    payload={"component": "vts", "message": "move model failed"},
                )
            )

    async def _ensure_baseline(self) -> dict | None:
        if self._baseline is not None:
            return self._baseline
        try:
            resp = await self.client.request("CurrentModelRequest")
        except Exception:
            logger.exception("VTS CurrentModelRequest failed, can't establish fly baseline")
            self.publish(
                Event(
                    kind=Kind.ERROR,
                    payload={"component": "vts", "message": "fly baseline lookup failed"},
                )
            )
            return None
        self._baseline = resp["data"]["modelPosition"]
        return self._baseline

    async def _toggle_expression(self, *, active: bool) -> None:
        try:
            await self.client.request(
                "ExpressionActivationRequest",
                {"expressionFile": self.fly_config.hotkey, "active": active, "fadeTime": 0.25},
            )
        except Exception:
            logger.exception("VTS fly expression toggle failed (active=%s)", active)
            self.publish(
                Event(
                    kind=Kind.ERROR,
                    payload={"component": "vts", "message": "fly expression toggle failed"},
                )
            )


@dataclass
class VTSMotionPlayer:
    """Plays a precomputed envelope (`director/motion.py`'s
    `extract_envelope` output) into VTS at a fixed frame rate, concurrently
    with audio playback -- see `outputs/speech.py`'s `Speaker` for how the
    two are started together against the same cancel token (design doc
    §8.1's "replay against the audio clock").

    Cancellable the same way `AudioPlayer` is: checks `cancel` between
    frames rather than sleeping through the whole clip uninterruptibly
    (CLAUDE.md invariant 5). A per-call failure (VTS unavailable, request
    error) is reported and stops this clip's motion -- it must never
    propagate and kill the sibling audio task; `Speaker` relies on that.
    """

    client: VTSTransport
    parameter_name: str
    publish: Callable[[Event], None] = _default_publish
    # Published to the dashboard at a fraction of the injection rate --
    # design doc §13 explicitly specs vts.param as "sampled, not every
    # frame." Every frame at 30Hz would be pure bus noise nothing consumes
    # yet (no sparkline panel exists), same reasoning session 9 used to
    # gate Mood.tick()'s publish rate.
    publish_every_n_frames: int = 6
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    # time.monotonic per CLAUDE.md ("all timing uses time.monotonic()").
    # Injectable so tests can pace deterministically without real delays --
    # same shape as `sleep` above.
    clock: Callable[[], float] = time.monotonic

    async def play(self, envelope: list[float], fps: float, *, cancel: asyncio.Event) -> None:
        if not envelope or cancel.is_set():
            return
        frame_dt = 1.0 / fps if fps > 0 else 0.0

        # Paced to a deadline schedule, not a fixed per-frame sleep -- a
        # real InjectParameterDataRequest round trip costs ~17ms
        # (SESSION_STATE.md session 10 measurement) and sleeping a full
        # frame_dt *after* that call, as an earlier version of this code
        # did, stretches the clip to well past the audio's real duration
        # (~50ms/frame at 30fps instead of ~33ms -- a ~1.5x overrun caught
        # by advisor review before this ever ran live). Sleeping only the
        # remainder of each frame's deadline keeps the clip's total wall-
        # clock length matched to `len(envelope) * frame_dt`, which is the
        # whole point of "replay against the audio clock" (design doc
        # §8.1).
        start = self.clock()
        for i, value in enumerate(envelope):
            if cancel.is_set():
                break
            if not await self._inject(value):
                break  # failure already reported inside _inject
            if i % self.publish_every_n_frames == 0:
                self.publish(
                    Event(
                        kind=Kind.VTS_PARAM, payload={"name": self.parameter_name, "value": value}
                    )
                )
            deadline = start + (i + 1) * frame_dt
            remaining = deadline - self.clock()
            if remaining > 0:
                await self.sleep(remaining)

        # Ramp to rest explicitly, on every exit path (finished, cancelled,
        # or a mid-clip injection failure above) -- VTS drops an un-resent
        # tracking parameter on its own after ~1s, but with an undefined
        # decay shape. An explicit 0.0 is a controlled return to neutral,
        # not a wait-and-see. If the client is genuinely down, this fails
        # too and reports its own (harmless, redundant) error via _inject.
        await self._inject(0.0)

    async def _inject(self, value: float) -> bool:
        try:
            await self.client.request(
                "InjectParameterDataRequest",
                {
                    "faceFound": True,
                    "mode": "set",
                    "parameterValues": [{"id": self.parameter_name, "value": value, "weight": 1}],
                },
            )
        except Exception:
            logger.exception("VTS motion injection failed (parameter=%s)", self.parameter_name)
            self.publish(
                Event(
                    kind=Kind.ERROR,
                    payload={"component": "vts", "message": "motion injection failed"},
                )
            )
            return False
        return True


@dataclass
class VTSIdleDriftPlayer:
    """Thin injection loop over `director/idle_drift.py`'s `IdleDrift`
    state machine -- calls `idle_drift.tick()` each frame and injects
    whatever it returns, for as long as `cancel` isn't set. All the
    interesting behavior (hold/ease phases, target selection, easing
    curve) lives in `IdleDrift`, kept pure/testable without I/O; this
    class is only the VTS-facing shell, same split as `VTSMotionPlayer`
    vs. `director/motion.py`'s `extract_envelope`.

    Unlike `VTSMotionPlayer`, which plays one finite clip per sentence,
    this runs for the lifetime of the VTS connection (started once in
    `_run_vts_subscriber`, right alongside `_attach_motion`, per advisor
    review: share the one lock-serialized client rather than open a second
    connection).

    Both axes (x=turn, z=tilt) are injected in a single
    InjectParameterDataRequest per frame, not two separate calls -- halves
    the connection load per tick versus one call per axis, which matters
    since this is an *always-on* second writer sharing motion.py's
    lock-serialized connection budget (see MotionConfig.fps's comment).

    `faceFound: True`, matching `VTSMotionPlayer._inject` deliberately --
    advisor flagged that two concurrent writers on the same connection
    disagreeing on that field (vts_probe.py's throwaway diagnostic script
    uses `False`) is a real last-writer-wins hazard worth avoiding
    explicitly rather than leaving to chance.
    """

    client: VTSTransport
    x_parameter_name: str
    z_parameter_name: str
    publish: Callable[[Event], None] = _default_publish
    publish_every_n_frames: int = 6
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic

    async def run(self, idle_drift: IdleDrift, *, cancel: asyncio.Event) -> None:
        if cancel.is_set():
            return
        fps = idle_drift.config.fps
        frame_dt = 1.0 / fps if fps > 0 else 0.0

        start = self.clock()
        i = 0
        while not cancel.is_set():
            x_value, z_value = idle_drift.tick()
            if not await self._inject_both(x_value, z_value):
                break  # failure already reported inside _inject_both
            if i % self.publish_every_n_frames == 0:
                self.publish(
                    Event(
                        kind=Kind.VTS_PARAM,
                        payload={"name": self.x_parameter_name, "value": x_value},
                    )
                )
                self.publish(
                    Event(
                        kind=Kind.VTS_PARAM,
                        payload={"name": self.z_parameter_name, "value": z_value},
                    )
                )
            i += 1
            deadline = start + i * frame_dt
            remaining = deadline - self.clock()
            if remaining > 0:
                await self.sleep(remaining)

        # Ramp both axes to rest on every exit path, same reasoning as
        # VTSMotionPlayer.play's trailing ramp -- an idle drift task that
        # gets cancelled at shutdown mid-drift shouldn't leave the head
        # parked off-center relying on VTS's own undefined-decay auto-drop.
        await self._inject_both(0.0, 0.0)

    async def _inject_both(self, x_value: float, z_value: float) -> bool:
        try:
            await self.client.request(
                "InjectParameterDataRequest",
                {
                    "faceFound": True,
                    "mode": "set",
                    "parameterValues": [
                        {"id": self.x_parameter_name, "value": x_value, "weight": 1},
                        {"id": self.z_parameter_name, "value": z_value, "weight": 1},
                    ],
                },
            )
        except Exception:
            logger.exception(
                "VTS idle drift injection failed (parameters=%s, %s)",
                self.x_parameter_name,
                self.z_parameter_name,
            )
            self.publish(
                Event(
                    kind=Kind.ERROR,
                    payload={"component": "vts", "message": "idle drift injection failed"},
                )
            )
            return False
        return True
