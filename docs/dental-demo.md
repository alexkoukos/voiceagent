# Papadopoulou dental demo

The browser page is a minimal voice demo: start/end call, connection status, and a collapsed **Services, prices & opening hours** section. No live transcription is displayed or sent to the browser. Internal call records still follow the existing retention settings.

Anyone with `/demo/{slug}` can see the configured services, durations, prices, timezone, and weekly hours without signing in. `/demo/{slug}/info` returns those same public fields as JSON. Both read the practice database used by the assistant. No credentials, calendar IDs, patient records, messages, or internal knowledge-base entries are exposed. Missing prices are shown as “Ask for pricing.” Weekly hours do not represent live appointment availability; the assistant checks availability during the call.

## An isolated demo database entry

`backend/config/demo_dentist.json` contains a fictional **Οδοντιατρείο Παπαδοπούλου** with two services: a 30-minute checkup (€40) and 45-minute cleaning (€60). These are explicitly sample prices, not verified prices from a real clinic. It has no phone numbers or external calendar, no email/SMS recipients, no reminders, one concurrent call, and a €5 monthly usage cap. Web calls already have audio recording disabled by the backend.

After starting the API and voice worker as described in the README, run from `backend/`:

```sh
uv run --python 3.12 --env-file ../.env --with-requirements requirements.txt \
  python scripts/setup_dental_demo.py --backend http://localhost:8000
```

Use the deployed HTTPS backend URL to create a hosted demo. `ADMIN_API_TOKEN` is read from the environment and never included in the shareable link. The script creates a **new** practice with a random slug each time; it never modifies an existing clinic or calendar. Share the printed demo link. Existing dental demo links receive the simplified UI when the backend is deployed and display their own stored prices and hours.

## Reply timing and wording

Greek ElevenLabs pipeline calls use Scribe's end-of-speech event directly with no extra endpointing delay. The existing 0.7-second silence threshold remains, as do interruption safeguards. English semantic turn detection and the fallback speech pipeline retain their previous settings. The demo asks for microphone access before starting a paid session, and releases the microphone on failed connection, busy response, or hangup.

The prompt asks for direct, short answers, spoken currency, and no repeated “one moment” messages. These changes remove avoidable work; they do not establish a measured production latency improvement. Provider, worker startup, network, and hosting region delays still need a live call check after deployment. Existing environment overrides remain in effect.

## Before sharing

- Deploy both backend and worker; creating the configuration alone does not deploy the code.
- Open the link on desktop and mobile. Confirm microphone permission, audible greeting, retry, and hangup.
- Ask the price of a cleaning and check the answer against the public information section. Ask for hours, then make and cancel a fictional appointment.
- Speak a longer sentence with a brief pause and verify the assistant waits for you to finish. Compare end-of-speech to first-audio timings in the protected call monitor.
- Use fictional names and details for demo appointments. Replace the link placeholder in `docs/dental-demo-email.md` with the tested hosted link.

## Shareable live dashboard

Apply migrations through `0030` before deploying the backend. The setup script now also prints a **shareable live dashboard** URL, valid for seven days by default. For the main demo use `--permanent`, so it stays available until explicitly revoked. That single link includes the call interface, public service prices/hours, live operating metrics, cost explanations, four selectable ElevenLabs voices, and suggested test scenarios. Use this URL in the email.

The dashboard is in English; the call assistant and clinic information stay in Greek. It refreshes every two seconds during active calls and every eight seconds when idle, pauses when the tab is hidden, and labels interrupted updates as stale. Reported costs normally arrive after hangup. No sample results are inserted into the live dashboard.

A founder can create another link with `POST /practices/{practice_id}/demo-dashboard`, passing `{"expires_in_days": 7}` and the founder key in `x-api-key`. The duration can be 1–30 days, or `null` for no automatic expiry. To preserve an existing URL, use founder-authenticated `PATCH /practices/{practice_id}/demo-dashboard/{id}` with `{"expires_in_days": null}`. This keeps the same token and associated calls. The practice must explicitly be marked `routing_rules.demo_only: true`; the dental fixture already is. Only mark a dedicated fictional practice this way. Do not connect it to real clinic calendars or notifications. The response returns `id`, `path`, and `expires_at`; save the ID to revoke it. The random bearer token is returned only in the link, and only its SHA-256 digest is stored.

Revoke a link with founder-authenticated `DELETE /practices/{practice_id}/demo-dashboard/{id}`. Expired, revoked, offboarded, or no-longer-demo links deny the dashboard page, data, embedded call page, and session creation. Responses are not cached or indexed and do not send referrers. Anyone who receives the unique link can use it until expiry or explicit revocation; it does not require a separate login.

Each call started inside that dashboard is attached to its link **before** dispatching the worker. The dashboard only reads those attached web calls for that practice. Earlier calls, ordinary `/demo/{slug}` calls, phone calls, calls from another share link, and calls whose data was deleted are excluded. There is no caller-name, transcript, recording, message, or raw telemetry endpoint on the shared dashboard. The founder monitor remains protected.

To keep polling bounded, the view includes up to the latest 100 calls and 10,000 relevant timing events. If more exist, the dashboard shows that its window is limited. Answer latency uses the answer estimate, separately from the first-sound estimate (which can include filler speech). Outcomes reflect application events, not independently reviewed booking accuracy. Unknown measurements display as dashes, never fabricated zeroes.

Cost cards distinguish clinic prices (sample EUR), reported provider usage (estimated USD), and subscription pricing (not quoted). They use the existing stored, versioned rates and list excluded operating costs. Missing usage and missing rates stay visible; a fully priced usage report is not an all-in invoice. No currency conversion or revenue claims are made.

## Persistence

Keep the main demo practice and link in the deployed Postgres database. Deployments run additive migrations and do not recreate or reset them. Do not rerun the setup script to restart a demo: it creates a separate practice. Back up the database and keep deployment encryption keys stable. No-expiry links still respect offboarding, revocation and usage limits. Existing transcript/telemetry retention remains in effect; keeping the demo does not retain personal details forever.
