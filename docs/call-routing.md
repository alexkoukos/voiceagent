# Existing-number call routing

The clinic keeps its public number. Its carrier or PBX forwards eligible calls to a separate AI ingress DID in `phone_numbers`. The backend identifies the clinic by that DID. The public number is recorded in `call_routing.public_number`; it is not ported or replaced.

Add `call_routing` to the practice create/update payload, or send the routing object alone to `PUT /practices/{id}/routing/setup`. Example for a missed-call pilot:

```json
{
  "phone_numbers": ["+302100000002"],
  "call_routing": {
    "mode": "human_first",
    "public_number": "+302100000001",
    "ai_destination_number": "+302100000002",
    "provider": "Clinic PBX provider",
    "phone_system": "sip_pbx",
    "capabilities": {
      "no_answer": true,
      "configurable_no_answer_timeout": true
    },
    "no_answer_seconds": 20,
    "transfer_destinations": ["+306900000001"],
    "transfer_failure": "collect_callback",
    "carrier_configuration_confirmed": false
  }
}
```

The example is the routing portion of a `PracticeIn` request; other required practice fields still apply. Supported modes are `ai_first`, `human_first`, `after_hours`, `overflow`, and `unconfigured`. Mark a capability true only after verifying it with the clinic's provider. Weekly opening hours live in `hours`; holidays and closures can be recorded in `call_routing` for the provider/PBX setup. The service does not change an external provider's routing automatically.

`GET /practices/{id}/routing/setup` returns clinic-specific configuration steps. For a confirmed Nova GR mobile with human-first routing and a supported timeout, `GET /practices/{id}/forwarding` returns the [Nova-documented mobile codes](https://nova.gr/epixeiriseis/eksipiretisi-pelatwn/simvolea-ipiresies/pos-diachirizome-tin-proothisi-kliseon-sto-kinito). Other phone systems and modes require provider/PBX instructions. After configuring forwarding, test a normal call, a no-answer or busy call as applicable, an after-hours call, a successful human handoff, and an unanswered handoff through the public number.

The inbound call log records arrival at the AI DID, AI answer, handoff attempts/results, and call end. It records the upstream forwarding reason only if the SIP provider supplies `sip.forwardingReason`; it does not infer a human ring attempt from an AI arrival. Call details expose the time-stamped `routing` events.

For phone handoffs, the AI dials the configured staff destination into the active LiveKit room and waits for an answer. If the dial fails or times out, the AI applies `transfer_failure` (`return_to_ai`, `take_message`, or `collect_callback`). A staff member joining through the app follows the same fallback. Verify that the outbound SIP trunk and caller ID can dial the destination before pilot use.
