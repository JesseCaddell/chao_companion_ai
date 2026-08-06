"""Phase 0 diagnostic (design doc §17, §19 open question 1).

Connects to VTube Studio, authenticates, and dumps the full Live2D parameter list
and hotkey list unfiltered -- the point is to read the parameter names the rigger
chose, especially whether the ball has an independent position parameter. Also
triggers the first available hotkey and creates + injects a custom parameter, to
prove both output channels work end to end.

Run: uv run python -m chao.tools.vts_probe

Uses print() rather than the Event bus -- this is a standalone diagnostic that
runs before the bus exists, not a component of the running system. See CLAUDE.md
invariant 4; this file is a deliberate, scoped exception to it.
"""

from __future__ import annotations

import asyncio
import json

from chao.outputs.vts import VTSAPIError, VTSClient

CUSTOM_PARAM_NAME = "ChaoProbeTest"


async def main() -> None:
    client = VTSClient()

    print(f"Connecting to {client.url} ...")
    await client.connect()

    print("Authenticating -- if this is the first run, check VTube Studio for an "
          "authorization popup (it can be missed if VTS is on another monitor).")
    await client.authenticate()
    print("Authenticated.\n")

    print("=== Live2DParameterListRequest ===")
    params = await client.request("Live2DParameterListRequest")
    print(json.dumps(params["data"], indent=2))

    print("\n=== HotkeysInCurrentModelRequest ===")
    hotkeys = await client.request("HotkeysInCurrentModelRequest")
    print(json.dumps(hotkeys["data"], indent=2))

    available = hotkeys["data"].get("availableHotkeys", [])
    if available:
        target = available[0]
        print(f"\n=== Triggering hotkey: {target['name']!r} ({target['hotkeyID']}) ===")
        trigger = await client.request("HotkeyTriggerRequest", {"hotkeyID": target["hotkeyID"]})
        print(json.dumps(trigger["data"], indent=2))
    else:
        print("\nNo hotkeys found on the current model -- skipping trigger step.")

    print(f"\n=== ParameterCreationRequest: {CUSTOM_PARAM_NAME} ===")
    try:
        create = await client.request(
            "ParameterCreationRequest",
            {
                "parameterName": CUSTOM_PARAM_NAME,
                "explanation": "Phase 0 diagnostic probe parameter",
                "min": 0,
                "max": 1,
                "defaultValue": 0,
            },
        )
        print(json.dumps(create["data"], indent=2))
    except VTSAPIError as e:
        print(f"(create skipped -- {e})")

    print(f"\n=== InjectParameterDataRequest: {CUSTOM_PARAM_NAME}=1 ===")
    inject = await client.request(
        "InjectParameterDataRequest",
        {"parameterValues": [{"id": CUSTOM_PARAM_NAME, "value": 1, "weight": 1}]},
    )
    print(json.dumps(inject["data"], indent=2))

    await client.close()
    print(
        "\nDone. Check the parameter list above for the ball's position parameter "
        "-- if there isn't one, §8.2's ball spring is a rigging task, not a code task."
    )


if __name__ == "__main__":
    asyncio.run(main())
