# PRD: AI Prank Caller (Level 1)

Sep 23, 2026 · @Alex

## Goal

Level 1 is an AI voice agent that calls friends from a Greek landline number (210) and runs a prank from a scenario I write for each call. Every call has two layers: my master system prompt (fixed) and a per-call custom prompt (who the friend is, what role the agent plays, what the joke is).

- **User:** only me, from a Swift iOS app (single user, personal tool).
- **Recipients:** friends.
- **Level 1 success:** one complete 1 to 3 minute call in natural Greek, with response latency under 1 second, started from a prompt in under 30 seconds.

## User flow

A call starts from my iOS app: pick a friend, write the scenario, tap Call.

```mermaid
sequenceDiagram
    participant Me as iOS app
    participant API as Backend
    participant Bot as Voice agent
    participant GW as SIP trunk (210)
    participant F as Friend
    Me->>API: friend + custom prompt + voice
    API->>API: merge prompts
    API->>Bot: start call
    Bot->>GW: SIP INVITE
    GW->>F: call from 210
    F-->>GW: answers
    Bot-->>F: real-time conversation
    Bot->>API: transcript + duration + recording
    API-->>Me: live status + transcript
```

The backend merges the two prompts, starts the call and streams what the agent says back to the app live.

## Functional requirements

| # | Requirement | Priority |
| --- | --- | --- |
| F1 | Outbound call from a Greek 210 landline (cloud SIP) to a Greek mobile | Must |
| F2 | Master system prompt applied to every call, editable from a config file | Must |
| F3 | Per-call custom prompt (scenario, role, friend's name, goal) | Must |
| F4 | Natural conversation in Greek with barge-in (stops talking when the friend interrupts) | Must |
| F5 | Agent hangs up by itself (hang-up tool) and there is a hard duration limit | Must |
| F6 | Recording of every call (audio + transcript) with playback in the iOS app | Must |
| F7 | Live transcript and a Hang up button in the iOS app | Should |
| F8 | Voice choice per call (male / female / style) | Should |
| F9 | Saved prompt templates (e.g. "wrong order", "fake call from the university") | Could |

After v1: many concurrent calls, with a live list of active calls in the app.

## Architecture & stack

Decision: Swift iOS app from day one, a Greek 210 landline over a cloud SIP trunk, Gemini Live for voice (about 15x cheaper than OpenAI Realtime), and everything in the cloud. No SIM, no hardware.

```mermaid
flowchart LR
    A[iOS app<br/>SwiftUI] -->|REST + WebSocket| B[Backend<br/>FastAPI + Postgres]
    B --> V[Voice agent<br/>LiveKit Cloud]
    V <--> R[Gemini Live]
    V <-->|SIP trunk| T[DIDWW<br/>210 number]
    T -->|PSTN| F[Friend]
    V -->|audio| S[(Recordings bucket)]
```

[DIDWW](https://www.didww.com/phone-numbers/local-numbers/Greece/Local/Athens/30-21) sells +30 21 (Athens) numbers with local and international SIP trunking for outbound, which is all LiveKit SIP needs. Greek landline numbers require ID and an address inside the prefix area ([Twilio regulatory](https://www.twilio.com/en-us/guidelines/gr/regulatory)), and my Athens address covers 210.

| Layer | Level 1 choice | Alternative |
| --- | --- | --- |
| 210 number + SIP trunk | DIDWW, +30 21 number with outbound SIP trunking | Zadarma or Telnyx |
| Voice pipeline | LiveKit Agents + LiveKit SIP | Pipecat με SIP transport |
| Voice model | Gemini Live native audio (speech-to-speech) | gpt-realtime-mini (about $0.10 to $0.33 per minute) or STT + cheap LLM + TTS |
| Backend | FastAPI + Postgres, stateful (calls, prompts, templates, transcripts, recording metadata) | Spring Boot |
| Hosting | Online in the cloud (Fly.io or Railway) + LiveKit Cloud, recordings in a private bucket (e.g. Cloudflare R2) | Local Mac + ngrok, dev only |
| App | SwiftUI iOS app, sideloaded from Xcode | TestFlight |

Everything runs online: the DIDWW SIP trunk connects straight to LiveKit Cloud, with nothing at home.

Recording happens in the voice agent (LiveKit Egress or saving the audio frames), not at the carrier, and the iOS app plays it back.

**Concurrent calls (after v1):** one SIP trunk carries many simultaneous channels from the same 210 number, so scaling means buying channels, not hardware. LiveKit agents scale horizontally (one worker per call) and the backend needs a call queue. That is why the v1 schema already stores a call\_id per call.

## Prompt design

The final call prompt is master + per-call. The master sets tone and limits; the per-call prompt sets the scenario.

**Master system prompt (fixed, mine):**

- Speak natural, everyday Greek in short sentences.
- Stay in the scenario's role until it is time for the reveal.
- Never ask for passwords, cards, money or personal data.
- Never impersonate police, a hospital, a bank or a specific real person, and never tell anyone that someone close to them is hurt or in danger.
- If the other person seems anxious or upset in a bad way, or asks directly "are you a bot?", reveal immediately: "Φάρσα σου έκανε ο Αλέξανδρος, είμαι AI".
- End the call with the hang-up tool after the reveal or when the scenario is done.

**Per-call prompt (template):**

```markdown
Friend: {name}
Your role: {persona}, e.g. a delivery guy who lost a pizza
Scenario: {scenario}
Inside jokes / context: {context}
Reveal: {when and how you say it was a prank}
Max duration: {minutes}
```

## Guardrails

The number is tied to my ID through KYC, so these protect me too, not only my friends.

- **Hard duration cap:** 5 minutes, then automatic hang-up.
- **No voice cloning** of real people without their consent.
- **Recording with notice:** every call is recorded. At the reveal the agent says the call was recorded, and deletes it right away if the friend asks. In Greece, recording a conversation without consent can be a criminal matter (Article 370A of the Penal Code), so this step is not skipped. Recordings live in a private bucket that only my backend can access.

j

## Milestones & development time

v1 (M1 to M5) is about 40 to 65 hours of work: roughly 4 to 6 weeks solo at 10 to 15 hours a week. The estimate assumes first time with LiveKit SIP and SwiftUI; the DIDWW KYC wait runs in parallel.

| Milestone | Deliverable | Effort |
| --- | --- | --- |
| M1: 210 line | Buy a 210 number on DIDWW (KYC), SIP trunk into LiveKit Cloud, test call | 3 to 5 h + KYC wait |
| M2: Hello call | LiveKit agent calls my phone and speaks Greek through Gemini Live | 8 to 12 h |
| M3: Prompts, recording, state | Master + per-call merge, hang-up tool, hard cap, recording, Postgres schema | 10 to 15 h |
| M4: iOS app | SwiftUI: prompt form, Call, live transcript, recording playback | 15 to 25 h |
| M5: Deploy + first prank (v1) | Backend and agent live in the cloud, call a friend | 4 to 6 h |
| M6: Concurrent (after v1) | More SIP channels, call queue, active-calls list | 8 to 12 h |

**Non-goals for Level 1:** inbound calls, voice cloning, multiple users, App Store. Concurrent calls only after v1.

## Estimated cost

Running v1 costs roughly $15 to $30 a month at 200 call minutes. The variable cost is about $0.03 to $0.07 per minute all-in, less than a fifth of what OpenAI Realtime alone would cost ($0.40).

| Item | Cost | Type |
| --- | --- | --- |
| Gemini Live, audio in + out ([pricing](https://ai.google.dev/gemini-api/docs/pricing)) | \~$0.023 / min | Per minute |
| DIDWW calls to Greek mobiles | \~$0.02 to $0.05 / min (estimate, not on the public page) | Per minute |
| LiveKit agent minutes ([pricing](https://livekit.com/pricing)) | Free up to 1,000 min, then $0.01 / min | Per minute |
| DIDWW 210 number | \~$2 to $5 / month (estimate) | Fixed |
| LiveKit Cloud Build plan | $0 / month | Fixed |
| Backend + Postgres hosting (Fly.io or Railway) | \~$5 to $10 / month | Fixed |
| Recordings bucket (Cloudflare R2) | \~$0 within the free tier | Fixed |
| Apple Developer account | $0 with 7-day sideload, or $99 / year | Optional |

## Open questions

- [ ] How good is Gemini Live's Greek over 8 kHz phone audio, and what does DIDWW charge per minute to Greek mobiles?
- [ ] Does DIDWW's KYC accept my Athens address, and how many days does it take?
- [ ] How natural does Realtime's Greek sound compared to ElevenLabs?
- [ ] How many concurrent channels does the SIP trunk include in the base plan?
