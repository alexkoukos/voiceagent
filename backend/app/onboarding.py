"""One-visit onboarding (PRD O1, O2, O4, O5): nothing that already exists online is typed by hand.

Imports never publish. Each one becomes a pending config version in the approval queue,
so the owner sees every imported field before the agent says it (O4); approving publishes it
in one step and it can be rolled back like any other version.
"""

import base64
import json
import logging
import re
from html import unescape
from urllib.parse import unquote

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app import config_changes
from app.booking import WEEKDAY_KEYS
from app.config import get_settings
from app.models import ConfigVersion, Practice

logger = logging.getLogger(__name__)

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_FIELDS = ",".join(f"places.{f}" for f in (
    "id", "displayName", "formattedAddress", "nationalPhoneNumber", "internationalPhoneNumber", "websiteUri",
    "regularOpeningHours", "googleMapsUri",
))
# Google's day numbers start on Sunday.
GOOGLE_DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
UNCERTAIN = 0.7


class ImportError_(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- O1: Google profile ---


async def _query_from(text: str) -> str:
    """A Maps link (short or long) or plain words -> words to search for."""
    text = text.strip()
    if not text.startswith("http"):
        return text
    url = text
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            url = str((await client.get(text)).url)
    except httpx.HTTPError:
        pass
    m = re.search(r"/place/([^/@?]+)", url)
    if m:
        return unquote(m.group(1)).replace("+", " ")
    m = re.search(r"[?&]q=([^&]+)", url)
    if m:
        return unquote(m.group(1)).replace("+", " ")
    raise ImportError_("unreadable_maps_link")


def hours_from_google(opening: dict | None) -> dict:
    """regularOpeningHours.periods -> {"mon": [["09:00", "14:00"], ...]}."""
    hours: dict[str, list] = {k: [] for k in WEEKDAY_KEYS}
    for p in (opening or {}).get("periods") or []:
        o, c = p.get("open") or {}, p.get("close")
        if c is None:  # open 24 hours
            hours[GOOGLE_DAYS[o.get("day", 0)]].append(["00:00", "23:59"])
            continue
        start = f"{o.get('hour', 0):02d}:{o.get('minute', 0):02d}"
        end = f"{c.get('hour', 0):02d}:{c.get('minute', 0):02d}"
        if c.get("day") != o.get("day") or end <= start:
            end = "23:59"
        hours[GOOGLE_DAYS[o.get("day", 0)]].append([start, end])
    return {k: sorted(v) for k, v in hours.items()}


async def import_google(db: AsyncSession, practice: Practice, text: str) -> ConfigVersion | None:
    key = get_settings().google_maps_api_key
    if not key:
        raise ImportError_("google_maps_api_key_missing")
    query = await _query_from(text)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(PLACES_URL, headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": PLACES_FIELDS},
                              json={"textQuery": query, "languageCode": practice.language or "el", "regionCode": "GR"})
    if r.status_code != 200:
        logger.warning("places search %s: %s", r.status_code, r.text[:300])
        raise ImportError_("google_error")
    places = r.json().get("places") or []
    if not places:
        raise ImportError_("not_found")
    place = places[0]
    greek = practice.language == "el"
    kb = dict(practice.knowledge_base or {})
    labels = (("Διεύθυνση", "Τηλέφωνο", "Ιστοσελίδα", "Χάρτης") if greek
              else ("Address", "Phone", "Website", "Map"))
    for label, value in zip(labels, (place.get("formattedAddress"), place.get("nationalPhoneNumber"),
                                     place.get("websiteUri"), place.get("googleMapsUri"))):
        if value:
            kb[label] = value
    changes: dict = {"knowledge_base": kb}
    hours = hours_from_google(place.get("regularOpeningHours"))
    if any(hours.values()):
        changes["hours"] = hours
    valid = config_changes.validated(practice, changes)
    name = (place.get("displayName") or {}).get("text", query)
    summary = (f"Google: {name}" + ("" if "hours" in valid else " (χωρίς ωράριο)" if greek else " (no hours)"))
    return await config_changes.propose(db, practice, valid, author="Google", summary=summary, source="import")


# --- O2: price list ---

PRICE_PROMPT = """This is a price list of a {vertical} business in Greece. Extract every service.
Return JSON only: {{"services": [{{"name": str, "price": str or null, "duration_minutes": int or null,
"confidence": number 0-1}}]}}.
- name: as written (keep Greek).
- price: as written with the currency, e.g. "40€" or "από 30€". null if not shown.
- duration_minutes: only if written; otherwise null. Never guess.
- confidence: how sure you are the name and price are read correctly.
"""


GREEK_LATIN = dict(zip(
    "αβγδεζηικλμνξοπρστυφωάέήίόύώϊϋΐΰς",
    "avgdeziiklmnxoprstyfoaeiiouoiyiys",
)) | {"θ": "th", "χ": "ch", "ψ": "ps"}


def _slug(name: str, used: set[str]) -> str:
    table = str.maketrans(GREEK_LATIN)
    base = re.sub(r"[^a-z0-9]+", "-", name.lower().translate(table)).strip("-")[:40] or "service"
    slug, i = base, 2
    while slug in used:
        slug, i = f"{base}-{i}", i + 1
    used.add(slug)
    return slug


async def _gemini_json(parts: list[dict]) -> dict:
    s = get_settings()
    if not s.gemini_api_key:
        raise ImportError_("gemini_api_key_missing")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{s.extraction_model}:generateContent"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, params={"key": s.gemini_api_key}, json={
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        })
    if r.status_code != 200:
        logger.warning("price list extraction %s: %s", r.status_code, r.text[:300])
        raise ImportError_("extraction_failed")
    try:
        text = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])
        return json.loads(text)
    except (KeyError, IndexError, ValueError):
        raise ImportError_("extraction_failed")


async def _page_text(url: str) -> str:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code != 200:
        raise ImportError_("website_unreachable")
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", r.text)
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html)))[:30000]


def merge_services(current: list[dict], extracted: list[dict], default_minutes: int = 30) -> tuple[list[dict], list[str]]:
    """Known services (same name) keep their id and duration and get the new price; new ones are
    added. Returns the list and the names worth a second look (low confidence, missing data)."""
    def key(n: str) -> str:
        return re.sub(r"\W+", "", n.lower())
    used = {s["id"] for s in current}
    out, check = [dict(s) for s in current], []
    index = {key(s["name"]): i for i, s in enumerate(out)}
    for e in extracted:
        name = (e.get("name") or "").strip()
        if not name:
            continue
        price = (str(e["price"]).strip() or None) if e.get("price") is not None else None
        minutes = e.get("duration_minutes")
        minutes = int(minutes) if isinstance(minutes, (int, float)) and 5 <= minutes <= 480 else None
        if (e.get("confidence") or 0) < UNCERTAIN or price is None:
            check.append(name)
        k = key(name)
        if k in index:
            item = out[index[k]]
            item["price"] = price or item.get("price")
            if minutes:
                item["duration_minutes"] = minutes
            continue
        if minutes is None:
            check.append(name)
        out.append({"id": _slug(name, used), "name": name[:100], "duration_minutes": minutes or default_minutes,
                    "price": price})
        index[k] = len(out) - 1
    return out, list(dict.fromkeys(check))


async def import_price_list(
    db: AsyncSession, practice: Practice, *, data_b64: str | None = None, mime_type: str | None = None,
    url: str | None = None,
) -> ConfigVersion | None:
    prompt = PRICE_PROMPT.format(vertical=practice.vertical or "service")
    if url:
        parts = [{"text": prompt + "\n\nWebsite text:\n" + await _page_text(url)}]
    else:
        try:
            raw = base64.b64decode(data_b64 or "", validate=True)
        except ValueError:
            raise ImportError_("bad_file")
        if not raw or len(raw) > MAX_UPLOAD_BYTES:
            raise ImportError_("bad_file")
        if mime_type not in ("image/jpeg", "image/png", "image/heic", "image/webp", "application/pdf"):
            raise ImportError_("unsupported_type")
        parts = [{"inline_data": {"mime_type": mime_type, "data": data_b64}}, {"text": prompt}]
    extracted = (await _gemini_json(parts)).get("services") or []
    if not extracted:
        raise ImportError_("nothing_found")
    services, check = merge_services(practice.services or [], extracted)
    valid = config_changes.validated(practice, {"services": services})
    greek = practice.language == "el"
    summary = (f"Τιμοκατάλογος: {len(extracted)} υπηρεσίες" if greek else f"Price list: {len(extracted)} services")
    if check:
        summary += (" · έλεγξε: " if greek else " · check: ") + ", ".join(check[:8])
    return await config_changes.propose(db, practice, valid, author="price list", summary=summary, source="import")


# --- O5: call forwarding codes ---


def forwarding_codes(target: str, mode: str, no_answer_seconds: int = 20) -> list[dict]:
    """GSM codes for Cosmote, Vodafone and Nova mobiles: dialed on the business phone.
    Landlines differ per provider."""
    if mode == "full":
        return [{"what": "all_calls", "code": f"**21*{target}#"}]
    return [
        {"what": "no_answer", "code": f"**61*{target}**{no_answer_seconds}#"},
        {"what": "busy", "code": f"**67*{target}#"},
        {"what": "unreachable", "code": f"**62*{target}#"},
    ]
