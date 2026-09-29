# AI Voice Receptionist

A Greek and English AI phone receptionist for small practices. It answers calls through LiveKit and Telnyx, handles appointments and messages, and gives staff an iOS view of calls and operations. The repository also contains the original outbound calling flow; the receptionist is the current product direction.

This is an active pilot project. Feature code and automated tests are present, but provider setup and live acceptance still need to be completed for each deployment. See [PRD_STATUS.md](PRD_STATUS.md) for the detailed verification record.

## What is here

| Directory | Role |
| --- | --- |
| `backend/` | FastAPI API, Postgres models and migrations, practice configuration, booking, call records, notifications, and operations |
| `agent/` | LiveKit voice worker for inbound receptionist calls, web demos, and the legacy outbound flow |
| `ios/` | SwiftUI app for call history, receptionist settings, appointments, messages, and handoff |

Receptionist calls can answer common questions, check availability, book or change appointments, take messages, and hand off to a person. Practices can configure hours, services, staff, routing rules, calendars, and notifications. A web demo is available at `/demo/<slug>` once a practice is configured. The backend also includes import review, calendar feeds, usage controls, and data export/erasure operations.

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/) for the commands below
- Docker for local Postgres
- LiveKit, a voice model provider, and Telnyx credentials for real phone calls
- Xcode and XcodeGen for the iOS app

## Run locally

1. Copy `.env.example` to `.env` at the repository root and fill in the credentials for the features you want to run. `.env` is ignored by Git. Set distinct, long values for `APP_API_TOKEN`, `ADMIN_API_TOKEN`, and `INTERNAL_API_TOKEN`. For deployments holding caller or patient data, configure `DATA_ENCRYPTION_KEY` as well.
2. Start Postgres:

   ```bash
   docker compose up -d postgres
   ```

3. Run the migrations and API in separate terminal commands:

   ```bash
   cd backend
   uv run --python 3.12 --env-file ../.env --with-requirements requirements.txt alembic upgrade head
   uv run --python 3.12 --env-file ../.env --with-requirements requirements.txt uvicorn app.main:app --reload
   ```

4. Start the voice worker in another terminal after configuring LiveKit and a voice provider:

   ```bash
   cd agent
   uv run --python 3.12 --env-file ../.env --with-requirements requirements.txt python agent.py dev
   ```

The API health endpoint is `http://localhost:8000/health`. Interactive API docs are disabled by default; set `ENABLE_DOCS=true` only for a trusted local environment. Real calls additionally need a configured LiveKit SIP trunk, Telnyx number, and the matching backend and worker settings. Calendar, recording storage, email, SMS, and push services are optional integrations configured through environment variables.

For the iOS app, run `cd ios && xcodegen && open PrankCaller.xcodeproj`, then set the backend URL and API key in the app. See [ios/README.md](ios/README.md) for the project layout. The app's displayed name and some internal identifiers still reflect the original AI Caller project.

## Practice access

Keep `ADMIN_API_TOKEN` on the founder's device and server only. With that key, `POST /practices/{practice_id}/api-keys` issues a random key for one practice; the response shows the key only once. Give each practice its own key for the iOS app. Its practice list contains only that practice, and another practice's URLs return 404. The legacy `APP_API_TOKEN` cannot open practice administration or patient records. Revoke a lost key with `DELETE /practices/{practice_id}/api-keys/{key_id}` using the founder key, then issue a replacement. Never send the founder or worker key to a practice.

The public `/demo/{slug}` link intentionally lets anyone place a test call. Treat the slug as a shareable caller link, not as an administration credential.

## Tests

Backend tests mock external providers. Database integration tests use a separate schema in local Postgres when `TEST_DATABASE_URL` is set; otherwise they skip.

```bash
cd backend
uv run --python 3.12 --with-requirements requirements.txt \
  --with pytest --with pytest-asyncio pytest -q -c pytest.ini
```

The scripted voice scenarios in `agent/scripts/` require a running backend and worker plus provider credentials. See the usage notes at the top of [scripted_calls.py](agent/scripts/scripted_calls.py).

## Noise and speech clarity

Set `NOISE_CANCELLATION=on` on the **agent service** as well as locally. The worker uses BVC for browser microphones and BVCTelephony for phone audio, before transcription and turn detection. These filters require LiveKit Cloud transport. Each call logs the selected source and filter; an explicit `off`, `false`, `0`, or `no` disables filtering. The last checked Railway configuration (2026-09-28) had this set to `off`, so deploying code alone will not enable it.

The default realtime pause is 700 ms and Scribe's silence threshold is 0.7 seconds. Existing deployment overrides still take precedence. The pipeline retains its interruption duration/word checks and resumes after false interruptions. Compare quiet speech, traffic, music and a nearby talker before shortening the pauses; measure missed words, unwanted interruptions, correct names/times, and reply latency.

Text pipelines use explicit delivery settings: ElevenLabs speaks at `TTS_SPEED=0.93` using PCM audio, and Gemini TTS receives instructions for clear, slightly slower speech. The outgoing pipeline opening uses the same ElevenLabs model/settings as the conversation. Language switches update both recognition and speech settings. `ELEVENLABS_TTS_MODEL` can select another compatible model for a listening comparison; keep the current model until the alternative's quality and latency are verified.

Choose voices using `ELEVENLABS_VOICE_MAP`, with optional `ELEVENLABS_VOICE_MAP_EL` and `_EN` overrides. A language map's `default` selects one voice for that language regardless of the app's voice key. Audition a native Greek voice with your actual names and services before configuring its ID; the built-in voice mappings are generic voices.

For recurring pronunciation errors, set `TTS_PRONUNCIATION_ALIASES` to a JSON object such as `{"el":{"OpenAI":"Όπεν έι άι"},"en":{"Dr.":"Doctor"}}`. Whole words or phrases are replaced only in synthesized audio, including names split across generated text chunks. Transcripts and booking values keep their original spelling. Each language supports up to 100 aliases (80 characters per key, 160 per spoken value). Realtime speech models do not use this text filter; use `pipeline` or `text_pipeline` for exact pronunciation aliases. In realtime mode, the separate Deepgram transcript does not determine what Gemini hears.

## Continuous integration and deployment

GitHub Actions runs backend tests against Postgres, agent tests, and an iOS Simulator build for changes proposed to `main`. The branch is protected so its required checks must pass before merge. Railway follows `main` and automatically deploys the backend and agent after a merge; a green build confirms the code passed automated checks, while live voice and provider acceptance remain separate pilot gates.

## Deployment notes

Both `backend/` and `agent/` have Dockerfiles. The backend runs migrations on startup and needs Postgres; the agent needs the same LiveKit project and the backend URL. Set secrets in your host's secret manager, not in the repository. Keep the backend at one replica until the in-memory live update and scheduler behavior is adapted for multiple instances.

Before real callers use the system, configure and verify recording disclosure or disable recording, review retention and access settings, and run live call and handoff acceptance tests. The current status and remaining gaps are tracked in [PRD_STATUS.md](PRD_STATUS.md).

The [Level 1 PRD](<PRD AI Prank Caller (Level 1).md>) documents the legacy outbound flow.
