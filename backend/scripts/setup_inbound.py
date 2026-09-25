"""Creates the LiveKit side of inbound calls: an inbound SIP trunk for the practice
numbers and a dispatch rule that puts each call in its own room with the agent.

    uv run --python 3.12 --with-requirements requirements.txt python scripts/setup_inbound.py +1XXXXXXXXXX [...]

Reads LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET from the environment (or .env).
Then point the number at LiveKit in Telnyx: an FQDN connection to your project's SIP URI
(LiveKit dashboard -> Settings -> SIP URI), and assign the number to that connection.
Each number must also be in a practice's phone_numbers, or the agent hangs up.
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from livekit import api

AGENT_NAME = os.environ.get("AGENT_NAME", "prank-caller")


async def main(numbers: list[str]) -> None:
    load_dotenv()
    async with api.LiveKitAPI() as lk:
        trunks = (await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())).items
        trunk = next((t for t in trunks if t.name == "receptionist-inbound"), None)
        if trunk is None:
            trunk = await lk.sip.create_inbound_trunk(api.CreateSIPInboundTrunkRequest(
                trunk=api.SIPInboundTrunkInfo(name="receptionist-inbound", numbers=numbers, krisp_enabled=True)
            ))
            print("created inbound trunk", trunk.sip_trunk_id)
        else:
            missing = [n for n in numbers if n not in trunk.numbers]
            if missing:
                trunk = await lk.sip.update_inbound_trunk_fields(
                    trunk.sip_trunk_id, numbers=api.ListUpdate(add=missing)
                )
            print("inbound trunk", trunk.sip_trunk_id, "numbers", list(trunk.numbers))

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
