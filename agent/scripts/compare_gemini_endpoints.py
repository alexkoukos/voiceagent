"""Time to first token of the receptionist model on AI Studio vs Vertex AI in one region.

Run it where the agent runs (Railway EU), so the network path is the real one:

    railway run --service agent python scripts/compare_gemini_endpoints.py --location eu \\
        --project gen-lang-client-0414182517 [--prompt-file prompt.md] [-n 20]

Needs GEMINI_API_KEY and GOOGLE_SERVICE_ACCOUNT_JSON (with Vertex AI access on the project).
Requests alternate between the two endpoints so both see the same conditions.
"""

import argparse
import asyncio
import json
import os
import statistics
import time

from google import genai
from google.genai import types
from google.oauth2 import service_account

FALLBACK_PROMPT = ("Είσαι ο ψηφιακός βοηθός ενός ιατρείου και απαντάς στο τηλέφωνο. Μιλάς σύντομα και ευγενικά. "
                   "Ωράριο: Δευτέρα έως Παρασκευή 09:00-14:00 και 17:00-21:00. ") * 40
TURN = "Καλησπέρα, θα ήθελα ένα ραντεβού για έλεγχο την άλλη Τρίτη το πρωί."


async def ttft(client: genai.Client, model: str, system: str) -> float:
    started = time.perf_counter()
    stream = await client.aio.models.generate_content_stream(
        model=model, contents=TURN, config=types.GenerateContentConfig(
            system_instruction=system, temperature=0.3, max_output_tokens=60,
            thinking_config=types.ThinkingConfig(thinking_level="minimal")))
    async for chunk in stream:
        if chunk.text:
            return (time.perf_counter() - started) * 1000
    return float("nan")


def spread(values: list[float]) -> str:
    ordered = sorted(v for v in values if v == v)
    if not ordered:
        return "no samples"
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return f"p50 {statistics.median(ordered):.0f} ms  p95 {p95:.0f} ms  (n={len(ordered)})"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--location", default="eu")
    parser.add_argument("--project", required=True)
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", "gemini-3.5-flash-lite"))
    parser.add_argument("--prompt-file")
    parser.add_argument("-n", type=int, default=20)
    args = parser.parse_args()
    system = open(args.prompt_file, encoding="utf-8").read() if args.prompt_file else FALLBACK_PROMPT
    credentials = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://www.googleapis.com/auth/cloud-platform"])
    clients = {
        "ai_studio": genai.Client(api_key=os.environ["GEMINI_API_KEY"]),
        f"vertex_{args.location}": genai.Client(vertexai=True, project=args.project, location=args.location,
                                                credentials=credentials),
    }
    results = {name: [] for name in clients}
    for name, client in clients.items():  # warm up connections and caches
        await ttft(client, args.model, system)
    for _ in range(args.n):
        for name, client in clients.items():
            results[name].append(await ttft(client, args.model, system))
    for name, values in results.items():
        print(f"{name:24} {spread(values)}")


if __name__ == "__main__":
    asyncio.run(main())
