"""Phase 0 diagnostic (design doc §17, §19 open question 1).

Findings from earlier runs of this probe, kept here so they aren't lost (also
written up in the design doc / CLAUDE.md):

- InjectParameterDataRequest CANNOT write Live2D model parameters directly (error
  453). It only writes plugin-created custom "tracking" parameters. To actually
  drive a Live2D output, a custom parameter must be manually bound to that output
  inside the VTS UI (per-model, one-time, not scriptable via the API).
- ExpressionActivationRequest sets an expression's active state explicitly
  (`active: true/false`), unlike HotkeyTriggerRequest which toggles blindly and
  left `angry` stuck on after an earlier run. This is the discrete channel now,
  not hotkeys.
- The ball has NO independent position parameter. Every position-shaped candidate
  (xp, xp2, xp3, yp3, yp4) was slider-tested directly in VTS and produced no
  visible motion; a live head-drag/webcam test also showed no lag on the ball or
  its bubble. `bubble`'s earlier value drift across dumps (2.619, then 0.549) is
  idle flame-flicker animation on the aura sprite, not positional physics.
  bubble/bubble2/hidebub/emote/swirl/heart/questionm/surprise all confirmed as
  the ball's icon-content and aura-visibility controls -- matches CLAUDE.md's
  "ball carries beat-level cognition" model exactly. §8.2's damped-spring ball
  motion has no parameter to drive; it's a rigging gap, not a code gap.

What this run does:

1. Cleanup: force-deactivate any stuck expression, delete the stray
   `ChaoProbeTest` parameter from an earlier run.
2. Fire ExpressionActivationRequest unconditionally against a known expression
   file, to prove the discrete channel works even when nothing is stuck.
3. Create ONE custom tracking parameter (`ChaoBindTest`), pause for the user to
   manually bind it in VTS to `happy` (chosen because it's known-visible, unlike
   the earlier attempt against `xp` which does nothing) -- proving the
   create -> bind -> inject -> visible motion chain for the continuous channel.

Run: uv run python -m chao.tools.vts_probe

Uses print()/input() rather than the Event bus -- this is a standalone diagnostic
that runs before the bus exists, not a component of the running system. See
CLAUDE.md invariant 4; this file is a deliberate, scoped exception to it.
"""

from __future__ import annotations

import asyncio
import json

from chao.outputs.vts import VTSAPIError, VTSClient

BIND_TEST_PARAM = "ChaoBindTest"
BIND_TEST_TARGET = "happy"
STRAY_PARAM = "ChaoProbeTest"
ACTIVATION_TEST_FILE = "happy.exp3.json"

HOLD_SECONDS = 3.0
RESEND_INTERVAL = 0.3  # VTS drops an injected parameter if not resent within 1s


async def hold_parameter(client: VTSClient, name: str, value: float, seconds: float) -> None:
    elapsed = 0.0
    while elapsed < seconds:
        await client.request(
            "InjectParameterDataRequest",
            {
                "faceFound": False,
                "mode": "set",
                "parameterValues": [{"id": name, "value": value, "weight": 1}],
            },
        )
        await asyncio.sleep(RESEND_INTERVAL)
        elapsed += RESEND_INTERVAL


async def cleanup(client: VTSClient) -> None:
    print("=== Cleanup ===")

    resp = await client.request("ExpressionStateRequest", {"details": False})
    active = [e for e in resp["data"]["expressions"] if e["active"]]
    for expr in active:
        print(f"Deactivating stuck expression '{expr['name']}' ...")
        await client.request(
            "ExpressionActivationRequest",
            {"expressionFile": expr["file"], "active": False, "fadeTime": 0.25},
        )

    try:
        await client.request("ParameterDeletionRequest", {"parameterName": STRAY_PARAM})
        print(f"Deleted stray parameter '{STRAY_PARAM}' from an earlier run.")
    except VTSAPIError:
        pass  # didn't exist, nothing to clean up

    if not active:
        print("Nothing to clean up.")
    print()


async def dump_state(client: VTSClient) -> None:
    print("=== Live2DParameterListRequest ===")
    params = await client.request("Live2DParameterListRequest")
    print(json.dumps(params["data"], indent=2))

    print("\n=== HotkeysInCurrentModelRequest ===")
    hotkeys = await client.request("HotkeysInCurrentModelRequest")
    print(json.dumps(hotkeys["data"], indent=2))
    print()


async def run_activation_test(client: VTSClient) -> None:
    print("=== Activation test: ExpressionActivationRequest (discrete channel) ===")
    print(f"Activating '{ACTIVATION_TEST_FILE}' for 2s -- watch the model now.")
    await client.request(
        "ExpressionActivationRequest",
        {"expressionFile": ACTIVATION_TEST_FILE, "active": True, "fadeTime": 0.25},
    )
    await asyncio.sleep(2.0)
    await client.request(
        "ExpressionActivationRequest",
        {"expressionFile": ACTIVATION_TEST_FILE, "active": False, "fadeTime": 0.25},
    )
    print("Deactivated. If the expression showed and cleared, the discrete channel is proven.\n")


async def run_bind_test(client: VTSClient) -> None:
    print("=== Bind test: does create -> manual bind -> inject actually move the model? ===")

    try:
        await client.request(
            "ParameterCreationRequest",
            {
                "parameterName": BIND_TEST_PARAM,
                "explanation": "Phase 0 bind test -- safe to delete",
                "min": 0,
                "max": 1,
                "defaultValue": 0,
            },
        )
        print(f"Created custom parameter '{BIND_TEST_PARAM}'.")
    except VTSAPIError as e:
        print(f"(create skipped -- {e})")

    print(
        f"\nNow in VTube Studio: open the current model's settings and find its "
        f"parameter mapping / Live2D parameter list. Set '{BIND_TEST_TARGET}'s input "
        f"source to '{BIND_TEST_PARAM}' (the tracking parameter you just created "
        f"should be selectable there). '{BIND_TEST_TARGET}' is known-visible, so this "
        f"proves the continuous channel rather than testing an inert candidate again.\n"
    )
    input(f"Press Enter once '{BIND_TEST_TARGET}' is bound to '{BIND_TEST_PARAM}' in VTS ...")

    # VTS drops the plugin connection during the pause above (idle timeout, or a
    # side effect of applying the binding in its UI) -- reconnect rather than
    # assume the original socket survived. The cached token means no new popup.
    print("Reconnecting (VTS may have dropped the connection while you were in settings) ...")
    await client.close()
    await client.connect()
    await client.authenticate()

    print(f"\nInjecting {BIND_TEST_PARAM} = 1 for {HOLD_SECONDS}s -- watch the model now.")
    await hold_parameter(client, BIND_TEST_PARAM, 1.0, HOLD_SECONDS)
    print("Released.\n")

    result = input(f"Did '{BIND_TEST_TARGET}' visibly trigger? (y/n) ").strip().lower()
    if result.startswith("y"):
        print(
            "\nChain confirmed: custom param -> manual VTS bind -> inject -> motion. "
            "The continuous channel works as CLAUDE.md's outputs/vts.py describes, "
            "with the added manual-binding prerequisite. Mood can stay a continuous "
            "2D point as designed -- bind the remaining mood/ball params the same way."
        )
    else:
        print(
            f"\nNo motion seen on '{BIND_TEST_TARGET}'. Either the binding didn't take "
            "in VTS, or injection-through-binding doesn't work as documented. If this "
            "keeps failing, mood likely has to collapse to discrete "
            "ExpressionActivationRequest calls instead of a continuous point -- a "
            "Phase 1 design change, not just a config fix."
        )


async def main() -> None:
    client = VTSClient()

    print(f"Connecting to {client.url} ...")
    await client.connect()

    print("Authenticating -- if this is the first run, check VTube Studio for an "
          "authorization popup (it can be missed if VTS is on another monitor).")
    await client.authenticate()
    print("Authenticated.\n")

    await cleanup(client)
    await dump_state(client)
    await run_activation_test(client)
    await run_bind_test(client)

    await client.close()
    print("\nDone. Report whether the activation test and bind test both worked.")


if __name__ == "__main__":
    asyncio.run(main())
