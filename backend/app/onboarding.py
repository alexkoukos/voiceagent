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
from urllib.parse import unquote, quote
from datetime import date, datetime, time, timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app import config_changes, public_fetch
from app.booking import WEEKDAY_KEYS
from app.config import get_settings
from app.models import ConfigVersion, ImportRecord, Practice

logger = logging.getLogger(__name__)

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_FIELDS = ",".join(f"places.{f}" for f in (
    "id", "displayName", "formattedAddress", "nationalPhoneNumber", "internationalPhoneNumber", "websiteUri",
    "regularOpeningHours", "currentOpeningHours", "googleMapsUri",
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
        _, url = await public_fetch.get(text)
    except (httpx.HTTPError, ValueError, OSError, TimeoutError):
        raise ImportError_("unreadable_maps_link")
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
            return {day: [["00:00", "23:59"]] for day in WEEKDAY_KEYS}
        start = f"{o.get('hour', 0):02d}:{o.get('minute', 0):02d}"
        end = f"{c.get('hour', 0):02d}:{c.get('minute', 0):02d}"
        opened, closed = o.get("day", 0), c.get("day", 0)
        if closed != opened:
            hours[GOOGLE_DAYS[opened]].append([start, "23:59"])
            day = (opened + 1) % 7
            while day != closed:
                hours[GOOGLE_DAYS[day]].append(["00:00", "23:59"])
                day = (day + 1) % 7
            if end != "00:00":
                hours[GOOGLE_DAYS[closed]].append(["00:00", end])
        elif end > start:
            hours[GOOGLE_DAYS[opened]].append([start, end])
    return {k: sorted(v) for k, v in hours.items()}


def special_hours_from_google(opening: dict) -> dict:
    def day(value):
        return date(value["year"], value["month"], value["day"])
    result = {day(item["date"]).isoformat(): [] for item in opening.get("specialDays", [])}
    for period in opening.get("periods", []):
        opened, closed = period.get("open", {}), period.get("close", {})
        if not opened.get("date") or not closed.get("date"):
            continue
        start = datetime.combine(day(opened["date"]), time(opened.get("hour", 0), opened.get("minute", 0)))
        end = datetime.combine(day(closed["date"]), time(closed.get("hour", 0), closed.get("minute", 0)))
        for value, spans in result.items():
            lo = datetime.combine(date.fromisoformat(value), time.min)
            hi = lo + timedelta(days=1)
            if start < hi and end > lo:
                spans.append([max(start, lo).strftime("%H:%M"),
                              "23:59" if end >= hi else end.strftime("%H:%M")])
    return result


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
    return await _propose_google(db, practice, place, source_ref=text)


async def _propose_google(db: AsyncSession, practice: Practice, place: dict, *, source_ref: str) -> ConfigVersion | None:
    greek = practice.language == "el"
    kb = dict(practice.knowledge_base or {})
    labels = (("Διεύθυνση", "Τηλέφωνο", "Ιστοσελίδα", "Χάρτης") if greek
              else ("Address", "Phone", "Website", "Map"))
    for label, value in zip(labels, (place.get("formattedAddress"), place.get("nationalPhoneNumber"),
                                     place.get("websiteUri"), place.get("googleMapsUri"))):
        if value:
            kb[label] = value
    changes: dict = {"knowledge_base": kb}
    name = (place.get("displayName") or {}).get("text")
    if name:
        changes["name"] = name
    hours = hours_from_google(place.get("regularOpeningHours"))
    if "periods" in (place.get("regularOpeningHours") or {}):
        changes["hours"] = hours
    special = special_hours_from_google(place.get("currentOpeningHours") or {})
    if special:
        changes["rules"] = {**(practice.rules or {}), "date_hours": {
            **(practice.rules or {}).get("date_hours", {}), **special}}
    valid = config_changes.validated(practice, changes)
    name = name or practice.name
    summary = (f"Google: {name}" + ("" if "hours" in valid else " (χωρίς ωράριο)" if greek else " (no hours)"))
    version = await config_changes.propose(db, practice, valid, author="Google", summary=summary, source="import")
    practice.google_place_id = place.get("id") or practice.google_place_id
    practice.google_refreshed_at = datetime.utcnow()
    db.add(ImportRecord(practice_id=practice.id, version_id=version.id if version else None,
                        source="google", source_ref=source_ref, raw_payload=json.dumps(place, ensure_ascii=False),
                        extracted=valid, confidence={"review_required": True}))
    return version


async def refresh_google(db: AsyncSession, practice: Practice, now: datetime) -> ConfigVersion | None:
    if (not practice.google_place_id or not get_settings().google_maps_api_key
            or (practice.google_refreshed_at and now.replace(tzinfo=None) - practice.google_refreshed_at < timedelta(days=7))):
        return None
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(f"https://places.googleapis.com/v1/places/{quote(practice.google_place_id, safe='')}",
                                    headers={"X-Goog-Api-Key": get_settings().google_maps_api_key,
                                             "X-Goog-FieldMask": PLACES_FIELDS.replace("places.", "")})
        response.raise_for_status()
    return await _propose_google(db, practice, response.json(), source_ref=practice.google_place_id)


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
    try:
        data, _ = await public_fetch.get(url)
    except (httpx.HTTPError, ValueError, OSError, TimeoutError):
        raise ImportError_("website_unreachable")
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", data.decode("utf-8", errors="replace"))
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
    version = await config_changes.propose(db, practice, valid, author="price list", summary=summary, source="import")
    db.add(ImportRecord(practice_id=practice.id, version_id=version.id if version else None,
                        source="price_list", source_ref=url or mime_type or "upload",
                        raw_payload=json.dumps(extracted, ensure_ascii=False), extracted=valid,
                        confidence={"review_names": check, "services": extracted}))
    return version


# --- O5: call forwarding codes ---


def routing_instructions(practice: Practice) -> dict:
    """Carrier-neutral checklist. Capabilities are declared during onboarding, not guessed."""
    setup = practice.call_routing or {}
    mode = setup.get("mode", "unconfigured")
    target = setup.get("ai_destination_number")
    public = setup.get("public_number")
    capabilities = setup.get("capabilities") or {}
    greek = practice.language == "el"
    if mode == "unconfigured" or not target or not public:
        return {"ready": False, "mobile_codes_available": False,
                "steps": ["Καταγράψτε τον υπάρχοντα αριθμό, τον πάροχο και το τηλεφωνικό σύστημα και επιλέξτε υποστηριζόμενη δρομολόγηση."
                          if greek else "Record the existing public number, provider and phone system, then choose a supported routing mode."]}
    actions = ({
        "ai_first": "Προωθήστε τις εισερχόμενες κλήσεις στον αριθμό AI.",
        "human_first": "Κρατήστε τα τηλέφωνα της γραμματείας· προωθήστε στο AI μόνο τις αναπάντητες κλήσεις.",
        "after_hours": "Κρατήστε την κανονική δρομολόγηση εντός ωραρίου· προωθήστε στο AI εκτός ωραρίου.",
        "overflow": "Κρατήστε την κανονική δρομολόγηση· προωθήστε στο AI όταν η γραμμή είναι κατειλημμένη.",
    } if greek else {
        "ai_first": "Forward eligible calls on the public number to the AI destination.",
        "human_first": "Keep the receptionist's phones ringing; forward only unanswered calls to the AI destination.",
        "after_hours": "Keep normal business-hours routing; forward out-of-hours calls to the AI destination.",
        "overflow": "Keep normal routing; forward busy or unavailable calls to the AI destination.",
    })
    provider = setup.get("provider") or ("τον πάροχο/διαχειριστή τηλεφωνικού κέντρου" if greek else "the phone provider/PBX administrator")
    steps = ([f"Διατηρήστε το {public} ως δημοσιευμένο αριθμό του ιατρείου.",
              f"Ζητήστε από {provider} να ρυθμίσει: {actions[mode]} Προορισμός: {target}."] if greek else
             [f"Keep {public} as the published clinic number.",
              f"Ask {provider} to configure: {actions[mode]} Destination: {target}."])
    if mode == "human_first":
        seconds = setup.get("no_answer_seconds")
        steps.append((f"Ορίστε χρόνο αναμονής {seconds} δευτερολέπτων." if greek else
                      f"Set the no-answer timeout to {seconds} seconds.") if seconds and capabilities.get("configurable_no_answer_timeout")
                     else ("Επιβεβαιώστε τον χρόνο αναμονής που υποστηρίζει ο πάροχος· μην υποσχεθείτε συγκεκριμένο αριθμό κουδουνισμάτων."
                           if greek else "Confirm the provider's supported no-answer interval; do not promise a specific ring count."))
    if mode == "after_hours":
        steps.append("Ρυθμίστε ωράριο, Σαββατοκύριακα, αργίες και έκτακτα κλεισίματα στον πάροχο/τηλεφωνικό κέντρο και δοκιμάστε κάθε περίπτωση."
                     if greek else "Configure weekly hours, weekends, holidays and exceptional closures in the provider/PBX schedule; test each case.")
    if setup.get("busy_behavior") == "forward_to_ai" and mode != "overflow":
        steps.append("Ενεργοποιήστε προώθηση κατειλημμένης γραμμής στον ίδιο αριθμό AI." if greek else
                     "Enable busy/unavailable forwarding to the same AI destination.")
    steps.append("Κάντε δοκιμαστικές κλήσεις στον δημόσιο αριθμό: κανονική, αναπάντητη και αποτυχημένη μεταφορά."
                 if greek else "Place test calls through the public number for normal, missed and failed-transfer cases before confirming activation.")
    return {"ready": True, "public_number": public, "ai_destination_number": target,
            "mode": mode, "provider": setup.get("provider", ""), "steps": steps,
            "mobile_codes_available": (mode == "human_first" and
                                       setup.get("no_answer_seconds") in (5, 10, 15, 20, 25, 30))
                                      and (setup.get("phone_system") == "mobile"
                                       and setup.get("carrier_configuration_confirmed")
                                       and setup.get("provider", "").lower() == "nova gr"),
            "carrier_configuration_confirmed": setup.get("carrier_configuration_confirmed", False),
            "note": ("Η προώθηση εξαρτάται από τον πάροχο ή το τηλεφωνικό κέντρο." if greek else
                     "Forwarding and transfer behavior are controlled by the clinic's carrier/PBX.")}


def forwarding_codes(target: str, mode: str, no_answer_seconds: int = 20) -> list[dict]:
    """Nova mobile codes from its published business support instructions.

    Other carriers and PBX systems must be configured with their provider.
    """
    if mode != "backup" or no_answer_seconds not in (5, 10, 15, 20, 25, 30):
        raise ValueError("unsupported Nova mobile forwarding mode or timeout")
    return [
        {"what": "no_answer", "code": (f"**61*{target}#" if no_answer_seconds == 20 else
                                       f"**61*{target}*11*{no_answer_seconds}#")},
        {"what": "busy", "code": f"**67*{target}#"},
    ]
