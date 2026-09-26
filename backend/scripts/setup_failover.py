"""OP1 failover: when the agent does not answer, Telnyx sends the call to the practice's own
mobile instead of dropping it ("on-failure" call forwarding on the agent's number).

    TELNYX_API_KEY=... uv run --python 3.12 --with-requirements requirements.txt \
        python scripts/setup_failover.py <agent number> <fallback mobile> [--apply]

Without --apply it only shows the current and the planned setting. The fallback is usually the
practice's notifications.fallback_number. Test by stopping the agent and calling the number.
"""

import os
import sys

import httpx
from dotenv import load_dotenv

API = "https://api.telnyx.com/v2"


def main(number: str, fallback: str, apply: bool) -> None:
    load_dotenv()
    headers = {"Authorization": f"Bearer {os.environ['TELNYX_API_KEY']}"}
    with httpx.Client(timeout=15, headers=headers) as client:
        found = client.get(f"{API}/phone_numbers", params={"filter[phone_number]": number}).json().get("data") or []
        if not found:
            sys.exit(f"{number} is not on this Telnyx account")
        number_id = found[0]["id"]
        current = client.get(f"{API}/phone_numbers/{number_id}/voice").json()["data"].get("call_forwarding")
        planned = {"call_forwarding_enabled": True, "forwards_to": fallback, "forwarding_type": "on-failure"}
        print("current:", current)
        print("planned:", planned)
        if not apply:
            print("dry run: add --apply to change it")
            return
        r = client.patch(f"{API}/phone_numbers/{number_id}/voice", json={"call_forwarding": planned})
        r.raise_for_status()
        print("applied:", r.json()["data"].get("call_forwarding"))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--apply"]
    if len(args) != 2:
        sys.exit(__doc__)
    main(args[0], args[1], "--apply" in sys.argv)
