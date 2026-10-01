"""Public Greek receptionist landing page and demo requests."""

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.dialects.postgresql import insert
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import notifications
from app.config import get_settings
from app.database import get_db
from app.models import DemoLead

router = APIRouter(tags=["landing"])
PAGE = (Path(__file__).resolve().parents[1] / "templates" / "landing.html").read_text(encoding="utf-8")
BUSINESS_TYPES = {"dentist", "medical", "dietitian", "aesthetic", "barber", "salon", "other"}


class LeadIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    business_name: str = Field(min_length=2, max_length=120)
    business_type: str
    contact_method: str
    contact_detail: str = Field(min_length=5, max_length=254)
    website: str = Field(default="", max_length=300)
    website_trap: str = ""
    request_id: str = Field(min_length=16, max_length=80)

    @field_validator("name", "business_name", "contact_detail", "website", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("business_type")
    @classmethod
    def valid_business_type(cls, value):
        if value not in BUSINESS_TYPES:
            raise ValueError("invalid business type")
        return value

    @field_validator("contact_method")
    @classmethod
    def valid_method(cls, value):
        if value not in {"email", "phone"}:
            raise ValueError("invalid contact method")
        return value

    @field_validator("request_id")
    @classmethod
    def valid_request_id(cls, value):
        if not re.fullmatch(r"[0-9a-f-]{16,80}", value):
            raise ValueError("invalid request id")
        return value

    @field_validator("website")
    @classmethod
    def valid_website(cls, value):
        if value:
            parsed = urlparse(value)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname:
                raise ValueError("invalid website")
        return value

    def valid_contact(self) -> bool:
        if self.contact_method == "email":
            return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", self.contact_detail))
        return bool(re.fullmatch(r"\+?[0-9 ()-]{7,25}", self.contact_detail))


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    settings = get_settings()
    slug = settings.landing_demo_slug.strip()
    if slug and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", slug):
        demo = f'<iframe title="Ζωντανή δοκιμή φωνής" data-src="/demo/{escape(slug)}?embed=1" allow="microphone; autoplay" loading="lazy"></iframe>'
        demo_class = "has-demo"
    else:
        demo = '<p class="demo-unavailable">Η ζωντανή δοκιμή δεν είναι διαθέσιμη αυτή τη στιγμή. Ζητήστε ένα προσωπικό demo και θα σας δείξουμε πώς λειτουργεί.</p>'
        demo_class = ""
    base = settings.backend_public_url.rstrip("/")
    og_image = escape(f"{base}/landing-assets/social.png", quote=True)
    page = (PAGE.replace("__DEMO_EMBED__", demo).replace("__DEMO_CLASS__", demo_class)
            .replace("__REQUEST_ID__", secrets.token_hex(16)).replace("__OG_IMAGE__", og_image))
    if request.query_params.get("sent") == "1":
        page = page.replace('id="form-status"', 'id="form-status" data-sent="true"')
    return HTMLResponse(page, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def _legal_page(title: str, content: str) -> HTMLResponse:
    page = (f'<!doctype html><html lang="el"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{escape(title)} · Ψηφιακή Γραμματεία</title>'
            '<link rel="icon" href="/landing-assets/favicon.svg" type="image/svg+xml">'
            '<link rel="stylesheet" href="/landing-assets/landing.css"></head><body>'
            '<header class="site-header"><div class="shell header-inner"><a class="brand" href="/">'
            'γραμματεία<span class="brand-accent">.</span></a>'
            '<a class="button button-small button-primary" href="/#contact">Ζητήστε demo ↗</a></div></header>'
            f'<main class="shell legal-page"><p class="eyebrow">ΠΛΗΡΟΦΟΡΙΕΣ</p><h1>{escape(title)}</h1>{content}'
            '<p><a class="text-link" href="/">← Επιστροφή στην αρχική</a></p></main></body></html>')
    return HTMLResponse(page)


@router.get("/privacy", response_class=HTMLResponse)
async def privacy():
    return _legal_page("Απόρρητο", """
      <p>Η φόρμα demo συλλέγει το όνομα, την επιχείρηση, τον τύπο της, τον τρόπο και τα στοιχεία επικοινωνίας, καθώς και έναν προαιρετικό σύνδεσμο. Τα χρησιμοποιούμε για να οργανώσουμε το demo που ζητήσατε.</p>
      <p>Τα αιτήματα αποθηκεύονται στην εφαρμογή και προωθούνται στο εταιρικό inbox πωλήσεων. Κρατάμε επίσης ένα μη αναστρέψιμο τεχνικό αποτύπωμα της σύνδεσης για περιορισμό ανεπιθύμητων αιτημάτων. Δεν ζητάμε στοιχεία ασθενών ή πελατών στη φόρμα. Για πρόσβαση, διόρθωση ή διαγραφή των στοιχείων σας, επικοινωνήστε μαζί μας μέσω της φόρμας και αναφέρετε το αίτημά σας.</p>
      <p>Η ζωντανή δοκιμή φωνής είναι ξεχωριστή από τη φόρμα. Πριν την ενεργοποιήσουμε δημόσια, θα δημοσιεύσουμε τους ειδικούς όρους για ηχογραφήσεις, απομαγνητοφωνήσεις και χρόνο διατήρησης.</p>
    """)


@router.get("/terms", response_class=HTMLResponse)
async def terms():
    return _legal_page("Όροι demo", """
      <p>Η δοκιμή παρουσιάζει τον βοηθό σε φανταστική επιχείρηση. Δεν δημιουργεί πραγματικά ραντεβού και δεν πρέπει να χρησιμοποιείται για επείγοντα ή ιατρικά αιτήματα.</p>
      <p>Το προσωπικό demo και η πιλοτική περίοδος 14 ημερών οργανώνονται κατόπιν επικοινωνίας. Η τιμή, η χρήση που περιλαμβάνεται και οι όροι της πιλοτικής συνεργασίας συμφωνούνται πριν από την ενεργοποίηση.</p>
    """)


def _same_origin(request: Request) -> bool:
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    origin = request.headers.get("origin")
    if not origin:
        return True
    parsed = urlparse(origin)
    return parsed.hostname == request.url.hostname and parsed.port == request.url.port


async def _payload(request: Request) -> LeadIn:
    body = await request.body()
    if len(body) > 4096:
        raise HTTPException(413, "Request too large")
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    try:
        if content_type == "application/json":
            data = json.loads(body)
        elif content_type == "application/x-www-form-urlencoded":
            data = {k: v[-1] for k, v in parse_qs(body.decode("utf-8"), keep_blank_values=True).items()}
        else:
            raise HTTPException(415, "Unsupported form format")
        lead = LeadIn.model_validate(data)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(422, "Ελέγξτε τα στοιχεία της φόρμας.")
    if not lead.valid_contact():
        raise HTTPException(422, "Ελέγξτε το email ή το τηλέφωνο επικοινωνίας.")
    return lead


@router.post("/leads")
async def submit_lead(request: Request, db: AsyncSession = Depends(get_db)):
    if not _same_origin(request):
        raise HTTPException(403, "Invalid request origin")
    lead = await _payload(request)
    if lead.website_trap:
        raise HTTPException(400, "Invalid request")
    settings = get_settings()
    if not (settings.founder_email and settings.smtp_host and settings.email_from and settings.data_encryption_key):
        raise HTTPException(503, "Η φόρμα δεν είναι διαθέσιμη αυτή τη στιγμή. Δοκιμάστε ξανά αργότερα.")
    existing = (await db.execute(select(DemoLead.id).where(DemoLead.id == lead.request_id))).first()
    if existing:
        result = {"ok": True}
    else:
        ip = request.client.host if request.client else "unknown"
        source_hash = hmac.new(settings.data_encryption_key.encode(), ip.encode(), hashlib.sha256).hexdigest()
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"landing-lead:{source_hash}"})
        recent = (await db.execute(select(func.count()).select_from(DemoLead).where(
            DemoLead.source_hash == source_hash,
            DemoLead.created_at >= datetime.utcnow() - timedelta(hours=1),
        ))).scalar_one()
        if recent >= 5:
            raise HTTPException(429, "Πολλά αιτήματα. Δοκιμάστε ξανά αργότερα.")
        statement = insert(DemoLead).values(
            id=lead.request_id, name=lead.name, business_name=lead.business_name,
            business_type=lead.business_type, contact_method=lead.contact_method,
            contact_detail=lead.contact_detail, website=lead.website, source_hash=source_hash,
            created_at=datetime.utcnow(),
        ).on_conflict_do_nothing(index_elements=[DemoLead.id]).returning(DemoLead.id)
        inserted = (await db.execute(statement)).scalar_one_or_none()
        if inserted:
            await notifications.queue(
                db, practice_id=None, kind="demo_lead", channel="email", recipient=settings.founder_email,
                subject="Νέο αίτημα για προσωπικό demo",
                body=(f"Όνομα: {lead.name}\nΕπιχείρηση: {lead.business_name}\n"
                      f"Τύπος: {lead.business_type}\nΕπικοινωνία ({lead.contact_method}): {lead.contact_detail}\n"
                      f"Ιστότοπος: {lead.website or '—'}\n"),
                dedupe_key=f"demo-lead:{lead.request_id}",
            )
            await db.commit()
            notifications.kick()
        else:
            await db.rollback()
        result = {"ok": True}
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse(result)
    return HTMLResponse('<!doctype html><html lang="el"><meta charset="utf-8"><title>Ευχαριστούμε</title><main><h1>Λάβαμε το αίτημά σας.</h1><p>Θα επικοινωνήσουμε μαζί σας για να οργανώσουμε το demo.</p><a href="/">Επιστροφή στην αρχική</a></main></html>')
