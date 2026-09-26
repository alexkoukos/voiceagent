"""One-visit onboarding (PRD O1, O2, O4, O5): nothing that already exists online is typed by hand.

Imports never publish. Each one becomes a pending config version in the approval queue,
so the owner sees every imported field before the agent says it (O4); approving publishes it
in one step and it can be rolled back like any other version.
"""

import asyncio
import base64
import json
import logging
import re
import time
from html import unescape
from urllib.parse import unquote

import httpx
from livekit import api
from sqlalchemy.ext.asyncio import AsyncSession

from app import config_changes
from app.booking import WEEKDAY_KEYS
from app.config import get_settings
from app.models import ConfigVersion, Practice
from app.prompts import mentions_recording

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


# --- go-live checklist ---
#
# One list the founder works through during the onboarding visit. Each item says what is
# missing and whether that is ours to fix (a server credential: "not_configured") or the
# practice's ("todo"). Required items block go-live; the rest are warnings.

AI_WORDS = ("ψηφιακ", "τεχνητ", "αυτόματ", "a.i.", "digital assistant", "virtual assistant", "artificial",
            "automated")


def discloses_ai(text: str) -> bool:
    text = text.lower()
    return any(w in text for w in AI_WORDS) or re.search(r"\bai\b", text) is not None


def _item(id_: str, prd: str | None, ok: bool, detail: str, *, required: bool = True,
          not_configured: bool = False) -> dict:
    if ok:
        status = "ok"
    elif not_configured:
        status = "not_configured"
    else:
        status = "todo" if required else "warning"
    return {"id": id_, "prd": prd, "required": required, "status": status, "detail": "" if ok else detail}


def checklist(practice: Practice, staff: list, connected_calendars: set[str], answered_calls: int,
              settings=None, trunk: dict | None = None) -> list[dict]:
    """Pure: everything is passed in, so it is testable without a database. `trunk` is
    trunk_numbers()' result; None reports the trunk check as not configured."""
    s = settings or get_settings()
    state = practice.onboarding or {}
    notif = practice.notifications or {}
    active = [p for p in staff if p.active]
    items = []

    open_days = [d for d, spans in (practice.hours or {}).items() if spans]
    items.append(_item("hours", "O1", bool(open_days), "No opening hours: import them from Google or set them."))
    services = practice.services or []
    items.append(_item("services", "O2", bool(services) and all(x.get("duration_minutes") for x in services),
                       "Services need a name and a duration (price list import or by hand)."))
    empty_kb = [k for k, v in (practice.knowledge_base or {}).items() if not str(v).strip()]
    items.append(_item("knowledge_base", "O1", not empty_kb,
                       "Empty answers the agent would skip: " + ", ".join(empty_kb[:6]), required=False))
    items.append(_item("staff", "R2", bool(active),
                       "No staff: callers can't ask for a person or be handed over.", required=False))

    calendar_ids = {c for c in [practice.calendar_id, *(p.calendar_id for p in active)] if c}
    unreachable = sorted(c for c in calendar_ids
                         if c not in connected_calendars and not s.google_service_account_json)
    items.append(_item(
        "calendars", "O3", not unreachable,
        f"{len(unreachable)} calendar(s) not connected: sign in per doctor (O3) or set GOOGLE_SERVICE_ACCOUNT_JSON.",
        not_configured=not (s.google_service_account_json or s.google_oauth_client_id)))

    items.append(_item("numbers", "O5", bool(practice.phone_numbers),
                       "No number for the agent: buy a Greek DID, add it here, run scripts/setup_inbound.py."))
    items.append(_trunk_item(practice.phone_numbers or [], trunk))
    items.append(_item("forwarding", "O5", bool(state.get("forwarding_confirmed_at")),
                       "Dial the forwarding codes on the business phone, then confirm here.", required=False))

    greeting = practice.greeting or ""
    items.append(_item("ai_disclosure", "G1", not greeting.strip() or discloses_ai(greeting),
                       "The custom greeting must say it is a digital (AI) assistant."))
    # G7: callers must hear that the call is recorded, unless nothing is recorded.
    recording = getattr(practice, "recording_enabled", True) is not False
    notice = bool(getattr(practice, "recording_notice", False)) or mentions_recording(greeting)
    items.append(_item("recording_notice", "G7", not recording or notice,
                       "Calls are recorded but the greeting doesn't say so (Greek law): turn on the recording "
                       "notice, or turn recording off."))
    dpa = state.get("dpa") or {}
    items.append(_item("dpa", "G2", bool(dpa.get("signed_on")), "Record the signed data processing agreement."))

    emails = notif.get("emails") or []
    smtp = bool(s.smtp_host and s.email_from)
    items.append(_item("email", None, bool(emails) and smtp,
                       "No business email." if not emails else "SMTP_HOST/EMAIL_FROM not set: emails stay pending.",
                       not_configured=bool(emails) and not smtp))
    sms = bool(s.telnyx_api_key and s.sms_from)
    wants_sms = notif.get("customer_sms", True) or bool(notif.get("urgent_sms"))
    items.append(_item("sms", None, not wants_sms or sms, "TELNYX_API_KEY/SMS_FROM not set: texts stay pending.",
                       required=False, not_configured=True))
    items.append(_item("fallback_number", "OP1", bool(notif.get("fallback_number")),
                       "No fallback mobile for when the agent is down (scripts/setup_failover.py).", required=False))
    items.append(_item("alerts", "OP9", bool(s.founder_email or s.founder_sms),
                       "FOUNDER_EMAIL/FOUNDER_SMS not set: nobody gets operator alerts.",
                       required=False, not_configured=True))
    items.append(_item("encryption", "OP8", bool(s.data_encryption_key),
                       "DATA_ENCRYPTION_KEY not set: patient data is stored unencrypted.",
                       required=False, not_configured=True))
    items.append(_item("test_call", "M0", answered_calls > 0 or bool(state.get("test_call_confirmed_at")),
                       "Make a test call (the /demo page or the number) and check the summary email."))
    return items


# --- number on the LiveKit inbound trunk (O5) ---
#
# A number that isn't on an inbound SIP trunk never reaches the agent: LiveKit rejects the call
# before any job starts. Checked live (at most TRUNK_TIMEOUT seconds) and cached, so the
# checklist stays fast; any failure reads "unknown" and never breaks the checklist.

TRUNK_TIMEOUT = 3.0
TRUNK_CACHE_SECONDS = 300
TRUNK_ERROR_CACHE_SECONDS = 30
_trunk_cache: tuple[float, dict] | None = None


def _digits(number: str) -> str:
    return re.sub(r"\D", "", number or "")


async def _list_inbound_trunks(s) -> list:
    async with api.LiveKitAPI(s.livekit_url, s.livekit_api_key, s.livekit_api_secret) as lk:
        return list((await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())).items)


async def trunk_numbers(settings=None) -> dict:
    """{"status": "ok", "numbers": [digits...], "any_number": bool}, {"status": "not_configured"}
    or {"status": "unknown", "error": ...}. A trunk with no numbers takes calls to any number."""
    global _trunk_cache
    s = settings or get_settings()
    if not (getattr(s, "livekit_url", "") and getattr(s, "livekit_api_key", "")
            and getattr(s, "livekit_api_secret", "")):
        return {"status": "not_configured"}
    now = time.monotonic()
    if _trunk_cache is not None and now < _trunk_cache[0]:
        return _trunk_cache[1]
    try:
        trunks = await asyncio.wait_for(_list_inbound_trunks(s), timeout=TRUNK_TIMEOUT)
        numbers = sorted({_digits(n) for t in trunks for n in t.numbers})
        result = {"status": "ok", "numbers": numbers, "any_number": any(not t.numbers for t in trunks)}
        _trunk_cache = (now + TRUNK_CACHE_SECONDS, result)
    except Exception as e:  # timeout, auth, network: the checklist still loads
        logger.warning("inbound trunk check failed: %r", e)
        result = {"status": "unknown", "error": (repr(e) or type(e).__name__)[:200]}
        _trunk_cache = (now + TRUNK_ERROR_CACHE_SECONDS, result)
    return result


def _trunk_item(numbers: list[str], trunk: dict | None) -> dict:
    item_id, what = "numbers_on_trunk", "Every number must be on a LiveKit inbound trunk (scripts/setup_inbound.py)."
    if not numbers:
        return _item(item_id, "O5", False, "Add the agent's number first.", required=False)
    status = (trunk or {}).get("status", "not_configured")
    if status == "not_configured":
        return _item(item_id, "O5", False, "LIVEKIT_URL/LIVEKIT_API_KEY/LIVEKIT_API_SECRET not set: can't check. " + what,
                     required=False, not_configured=True)
    if status != "ok":
        item = _item(item_id, "O5", False, "Couldn't reach LiveKit to check the trunk; try again. " + what,
                     required=False)
        return item | {"status": "unknown"}
    if trunk.get("any_number"):
        return _item(item_id, "O5", True, "", required=False)
    missing = [n for n in numbers if _digits(n) not in set(trunk.get("numbers") or [])]
    return _item(item_id, "O5", not missing,
                 f"Not on an inbound trunk, so calls never reach the agent: {', '.join(missing)}. "
                 "Run scripts/setup_inbound.py with them.", required=False)


def ready(items: list[dict]) -> bool:
    return all(i["status"] == "ok" for i in items if i["required"])


async def readiness(db: AsyncSession, practice: Practice) -> dict:
    from sqlalchemy import func, select

    from app.models import CalendarConnection, Call, CallStatus, Staff

    staff = list((await db.execute(select(Staff).where(Staff.practice_id == practice.id))).scalars())
    connected = set((await db.execute(select(CalendarConnection.calendar_id)
                                      .where(CalendarConnection.practice_id == practice.id))).scalars())
    answered = (await db.execute(select(func.count()).select_from(Call).where(
        Call.practice_id == practice.id, Call.direction.in_(("inbound", "web")),
        Call.status == CallStatus.completed))).scalar_one()
    items = checklist(practice, staff, connected, answered, trunk=await trunk_numbers())
    state = practice.onboarding or {}
    return {"ready": ready(items), "live_at": state.get("live_at"), "dpa": state.get("dpa"), "items": items}
