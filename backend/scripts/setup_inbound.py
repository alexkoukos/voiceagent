"""Creates the LiveKit side of inbound calls: an inbound SIP trunk for the practice
numbers and a dispatch rule that puts each call in its own room with the agent.

    uv run --python 3.12 --with-requirements requirements.txt python scripts/setup_inbound.py +1XXXXXXXXXX [...]

Reads LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET from the environment (or .env).
Then point the number at LiveKit in Telnyx: an FQDN connection to your project's SIP URI
(LiveKit dashboard -> Settings -> SIP URI), and assign the number to that connection.
Each number must also be in a practice's phone_numbers, or the agent hangs up.

The trunk only accepts calls from the carrier's signalling addresses: set SIP_ALLOWED_ADDRESSES
to a comma-separated list of IPs/CIDRs from the carrier's documentation (Telnyx: "SIP signaling
IPs"). Optionally SIP_AUTH_USERNAME / SIP_AUTH_PASSWORD add digest auth (configure the same
credentials on the carrier side). Without addresses the script refuses to run, so a forged
INVITE cannot reach the agent with any caller ID (audit P1-9).
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from livekit import api

AGENT_NAME = os.environ.get("AGENT_NAME", "prank-caller")


def allowed_addresses() -> list[str]:
    return [a.strip() for a in os.environ.get("SIP_ALLOWED_ADDRESSES", "").split(",") if a.strip()]


async def main(numbers: list[str]) -> None:
    load_dotenv()
    addresses = allowed_addresses()
    if not addresses:
        sys.exit("Set SIP_ALLOWED_ADDRESSES to the carrier's signalling IPs/CIDRs.")
    auth = {k: v for k, v in (("auth_username", os.environ.get("SIP_AUTH_USERNAME")),
                              ("auth_password", os.environ.get("SIP_AUTH_PASSWORD"))) if v}
    async with api.LiveKitAPI() as lk:
        trunks = (await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())).items
        trunk = next((t for t in trunks if t.name == "receptionist-inbound"), None)
        if trunk is None:
            trunk = await lk.sip.create_inbound_trunk(api.CreateSIPInboundTrunkRequest(
                trunk=api.SIPInboundTrunkInfo(name="receptionist-inbound", numbers=numbers, krisp_enabled=True,
                                              allowed_addresses=addresses, **auth)
            ))
            print("created inbound trunk", trunk.sip_trunk_id)
        else:
            missing = [n for n in numbers if n not in trunk.numbers]
            trunk = await lk.sip.update_inbound_trunk_fields(
                trunk.sip_trunk_id, numbers=api.ListUpdate(add=missing) if missing else None,
                allowed_addresses=addresses, **auth,
            )
            print("inbound trunk", trunk.sip_trunk_id, "numbers", list(trunk.numbers),
                  "allowed", list(trunk.allowed_addresses))

        rules = (await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())).items
        if any(r.name == "receptionist-dispatch" for r in rules):
            print("dispatch rule already exists")
            return
        rule = await lk.sip.create_dispatch_rule(api.CreateSIPDispatchRuleRequest(
            dispatch_rule=api.SIPDispatchRuleInfo(
                name="receptionist-dispatch",
                trunk_ids=[trunk.sip_trunk_id],
                rule=api.SIPDispatchRule(
                    dispatch_rule_individual=api.SIPDispatchRuleIndividual(room_prefix="inbound-")
                ),
                # No metadata: the agent asks the backend which practice was dialed.
                room_config=api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)]),
            )
        ))
        print("created dispatch rule", rule.sip_dispatch_rule_id)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1:]))
