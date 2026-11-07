# Web demo

A practice can be tried from a browser, without a phone line: `/demo/<slug>` opens a page with a call button, a male/female voice choice and the practice's public information (services, prices, hours). `/demo/<slug>/info` returns the same public fields as JSON. Nothing private is exposed: no calendar IDs, patient records, messages or internal knowledge-base entries. Web calls are never recorded, and no transcript is sent to the browser.

## A fictional demo practice

`backend/config/demo_dentist.json` describes a fictional practice, «Οδοντιατρείο Παπαδοπούλου», with sample services and prices, no phone numbers, no external calendar, no notification recipients, one line and a small monthly usage cap. With the API and worker running (see the [README](../README.md)):

```sh
cd backend
uv run --python 3.12 --env-file ../.env --with-requirements requirements.txt \
  python scripts/setup_dental_demo.py --backend http://localhost:8000 [--permanent]
```

The script reads `ADMIN_API_TOKEN` from the environment, sends it only to a local backend or over HTTPS, and creates a **new** practice with a random slug each time; it never changes an existing one. It prints the demo page and a shareable dashboard link.

## Shareable dashboard

The dashboard link wraps the call page with the practice's public information, live metrics (answer delay, outcomes) and a cost breakdown, for calls started from that link only. It is valid for 7 days, or until revoked with `--permanent`.

| Action | Request (founder key in `x-api-key`) |
|---|---|
| Create | `POST /practices/{id}/demo-dashboard` with `{"expires_in_days": 7}` (1–30, or `null` for no expiry) |
| Keep a link forever | `PATCH /practices/{id}/demo-dashboard/{link_id}` with `{"expires_in_days": null}` |
| Revoke | `DELETE /practices/{id}/demo-dashboard/{link_id}` |

- Only practices marked `routing_rules.demo_only: true` can have one. Mark only a fictional practice that is connected to no real calendar or notification.
- The token is random and only its SHA-256 digest is stored. Anyone with the link can use it until it expires or is revoked; responses are not cached, indexed or sent with a referrer.
- The dashboard reads only calls attached to its own link, at most the latest 100. It has no caller-name, transcript, recording or raw telemetry endpoint. Unknown measurements show as dashes, never as zeroes, and costs are estimates from the versioned rates in `config/pricing.json`.

## Before sharing a demo

- Deploy both the backend and the worker.
- Open the link on desktop and mobile: microphone permission, greeting, retry and hang-up.
- Ask a price and the hours, then book and cancel a fictional appointment.
- Check the reply times in `/monitor`.
