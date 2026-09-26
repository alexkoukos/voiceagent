import enum
import uuid
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.crypto import SecretLookup, SecretText
from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class CallStatus(str, enum.Enum):
    pending = "pending"
    queued = "queued"
    dialing = "dialing"
    active = "active"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class TranscriptRole(str, enum.Enum):
    agent = "agent"
    friend = "friend"


class Friend(Base):
    __tablename__ = "friends"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    phone_number: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    # Set on delete; the row stays so past calls still show the name.
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    calls: Mapped[list["Call"]] = relationship(back_populates="friend")


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String, nullable=False)
    persona: Mapped[str] = mapped_column(Text, nullable=False)
    scenario: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[str] = mapped_column(Text, default="")
    reveal: Mapped[str] = mapped_column(Text, default="")
    # Voice the app switches to when this preset is picked; None keeps the user's choice.
    voice: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class Practice(Base):
    """A business whose calls the agent answers (2.0 receptionist).

    `phone_numbers` are the numbers callers dial (E.164); an inbound call is matched to
    its practice by the dialed number. `hours` maps "mon".."sun" to [["09:00", "14:00"], ...].
    `services` is [{"id", "name", "duration_minutes", "price"?}]. `rules` holds booking
    rules: holidays, buffer_minutes, max_days_ahead, min_notice_minutes, slot_step_minutes.
    """

    __tablename__ = "practices"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Public web demo link: /demo/<slug>. None turns the demo off.
    slug: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    timezone: Mapped[str] = mapped_column(String, default="Europe/Athens")
    language: Mapped[str] = mapped_column(String, default="el")
    voice: Mapped[str] = mapped_column(String, default="Zubenelgenubi")
    # First thing the agent says; discloses that it's an AI assistant (PRD G1).
    greeting: Mapped[str] = mapped_column(Text, default="")
    phone_numbers: Mapped[list] = mapped_column(JSON, default=list)
    hours: Mapped[dict] = mapped_column(JSON, default=dict)
    services: Mapped[list] = mapped_column(JSON, default=list)
    rules: Mapped[dict] = mapped_column(JSON, default=dict)
    knowledge_base: Mapped[dict] = mapped_column(JSON, default=dict)
    # Google Calendar id; None keeps appointments only in our database.
    calendar_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Vertical template it was made from (dentist, barber, ...); informational.
    vertical: Mapped[str] = mapped_column(String, default="")
    # Evaluated by the backend, not the model (see app/routing.py for the keys).
    routing_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    # [{"id", "name", "service_ids", "staff_ids"}] for multi-service businesses (R8).
    departments: Mapped[list] = mapped_column(JSON, default=list)
    # {"emails": [...], "customer_sms": true, "digest_time": "20:00", "monthly_report": true}
    notifications: Mapped[dict] = mapped_column(JSON, default=dict)
    # {"enabled": false, "time": "18:00", "waitlist": false}
    reminders: Mapped[dict] = mapped_column(JSON, default=dict)
    # Caller ID for outbound reminder calls; empty uses SIP_OUTBOUND_NUMBER.
    outbound_number: Mapped[str | None] = mapped_column(String, nullable=True)
    max_concurrent_calls: Mapped[int] = mapped_column(Integer, default=2)
    retention_recordings_days: Mapped[int] = mapped_column(Integer, default=30)
    retention_transcripts_days: Mapped[int] = mapped_column(Integer, default=90)
    # For the monthly value report and the performance guarantee.
    avg_booking_value: Mapped[float] = mapped_column(Float, default=0)
    guarantee_threshold: Mapped[int] = mapped_column(Integer, default=10)
    # OP10: no new calls once this month's cost reaches the cap (alert at 80%). None = no cap.
    monthly_cost_cap_eur: Mapped[float | None] = mapped_column(Float, nullable=True)
    # OP10: callers the agent never answers (E.164).
    blocked_numbers: Mapped[list] = mapped_column(JSON, default=list)
    # OP7: set on offboarding; the agent stops answering.
    offboarded_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # OP2: PBKDF2 of the 4-6 digit PIN for changes by phone. None turns phone changes off.
    admin_pin_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    # Go-live checklist state (app/onboarding.py): {"dpa": {"signed_on", "signed_by"},
    # "forwarding_confirmed_at", "test_call_confirmed_at", "live_at"}. Not part of PracticeIn,
    # so a full practice update never clears it.
    onboarding: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class Staff(Base):
    """A person calls can be booked with or handed to (R2, R6). Each has their own calendar."""

    __tablename__ = "staff"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    practice_id: Mapped[str] = mapped_column(ForeignKey("practices.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # doctor, secretary, owner, staff
    role: Mapped[str] = mapped_column(String, default="staff")
    # Other ways callers say the name ("Γιώργο", "τον γιατρό").
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    # Service ids they do; empty means all.
    service_ids: Mapped[list] = mapped_column(JSON, default=list)
    # Own hours; empty uses the practice's.
    hours: Mapped[dict] = mapped_column(JSON, default=dict)
    calendar_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # For SIP transfer (handoff fallback) and urgent SMS.
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    bookable: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class Customer(Base):
    """Someone who called or booked; found again by phone number (B9, R4)."""

    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("practice_id", "phone"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    practice_id: Mapped[str] = mapped_column(ForeignKey("practices.id"), nullable=False, index=True)
    phone: Mapped[str] = mapped_column(SecretLookup, nullable=False)
    name: Mapped[str] = mapped_column(SecretText, default="")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class AppointmentStatus(str, enum.Enum):
    booked = "booked"
    cancelled = "cancelled"


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    practice_id: Mapped[str] = mapped_column(ForeignKey("practices.id"), nullable=False, index=True)
    call_id: Mapped[str | None] = mapped_column(ForeignKey("calls.id"), nullable=True)
    customer_name: Mapped[str] = mapped_column(SecretText, nullable=False)
    customer_phone: Mapped[str | None] = mapped_column(SecretLookup, nullable=True)
    service_id: Mapped[str] = mapped_column(String, nullable=False)
    service_name: Mapped[str] = mapped_column(String, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[AppointmentStatus] = mapped_column(
        Enum(AppointmentStatus), default=AppointmentStatus.booked
    )
    gcal_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # call id + slot: a retried booking returns the same appointment instead of a duplicate (B8).
    idempotency_key: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    staff_id: Mapped[str | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    # agent, reminder, waitlist, app
    source: Mapped[str] = mapped_column(String, default="agent")
    # none, calling, confirmed, cancelled, no_answer
    reminder_status: Mapped[str] = mapped_column(String, default="none")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    # Outbound (Level 1) calls have a friend; inbound and web calls have a practice.
    friend_id: Mapped[str | None] = mapped_column(ForeignKey("friends.id"), nullable=True)
    practice_id: Mapped[str | None] = mapped_column(ForeignKey("practices.id"), nullable=True)
    # outbound, inbound or web
    direction: Mapped[str] = mapped_column(String, default="outbound")
    caller_number: Mapped[str | None] = mapped_column(SecretLookup, nullable=True)
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    # booking, call_center, outbound
    use_case: Mapped[str | None] = mapped_column(String, nullable=True)
    # booked, rescheduled, cancelled, confirmed, info_given, message_taken, transferred, abandoned, failed
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    appointment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str | None] = mapped_column(SecretText, nullable=True)
    # name_uncertain, urgent, tool_error, over_duration, emergency, recording_refused
    flags: Mapped[list] = mapped_column(JSON, default=list)
    cost_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms_median: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # open, break or closed when the call came in (R3)
    hours_state: Mapped[str | None] = mapped_column(String, nullable=True)
    # Reminder / waitlist calls: what they're about.
    purpose: Mapped[str | None] = mapped_column(String, nullable=True)
    # Human review for the accuracy metrics: {"routing_correct": bool, "booking_correct": bool, "note": str}
    review: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    finalized: Mapped[bool] = mapped_column(Boolean, default=False)

    persona: Mapped[str] = mapped_column(Text, nullable=False)
    scenario: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[str] = mapped_column(Text, default="")
    reveal: Mapped[str] = mapped_column(Text, default="")
    voice: Mapped[str] = mapped_column(String, default="default")
    max_duration_seconds: Mapped[int] = mapped_column(Integer, default=300)
    from_own_number: Mapped[bool] = mapped_column(Boolean, default=False)
    # Language the call starts in (e.g. "el"); the agent follows the friend if they switch.
    language: Mapped[str | None] = mapped_column(String, nullable=True)

    status: Mapped[CallStatus] = mapped_column(
        Enum(CallStatus), default=CallStatus.pending
    )
    recording_url: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delete_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set when a call doesn't connect: no_answer, declined, unreachable or error.
    end_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)

    friend: Mapped["Friend | None"] = relationship(back_populates="calls")
    transcript_entries: Mapped[list["TranscriptEntry"]] = relationship(
        back_populates="call", order_by="TranscriptEntry.created_at"
    )


class TranscriptEntry(Base):
    __tablename__ = "transcript_entries"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), nullable=False)
    role: Mapped[TranscriptRole] = mapped_column(Enum(TranscriptRole), nullable=False)
    text: Mapped[str] = mapped_column(SecretText, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    call: Mapped["Call"] = relationship(back_populates="transcript_entries")


class RecordingDeletion(Base):
    """Durable object deletion, including egress uploads that finish after hang-up."""

    __tablename__ = "recording_deletions"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    call_id: Mapped[str | None] = mapped_column(ForeignKey("calls.id"), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class ConfigVersion(Base):
    """One change to a practice's hours, services, rules or knowledge base (OP2).

    `changes` holds only the fields that changed. A published version also stores the
    full `snapshot` after it, so rolling back is one step. Services, prices and FAQ changes
    from a doctor's link wait as `pending` until the founder approves them.
    """

    __tablename__ = "config_versions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    practice_id: Mapped[str] = mapped_column(ForeignKey("practices.id"), nullable=False, index=True)
    # published, pending, rejected
    status: Mapped[str] = mapped_column(String, default="published")
    # app, link, rollback, baseline
    source: Mapped[str] = mapped_column(String, default="app")
    author: Mapped[str] = mapped_column(String, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    changes: Mapped[dict] = mapped_column(JSON, default=dict)
    snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)


class AdminLink(Base):
    """A magic link that lets the business change hours, closures, prices and FAQ from a
    browser (OP2). Only the SHA-256 of the token is stored."""

    __tablename__ = "admin_links"

    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    practice_id: Mapped[str] = mapped_column(ForeignKey("practices.id"), nullable=False, index=True)
    staff_id: Mapped[str | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)


class DataRequest(Base):
    """A patient's export or erasure request, logged for the controller (OP8). The number
    itself is not kept: after an erasure it must not be stored anywhere."""

    __tablename__ = "data_requests"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    practice_id: Mapped[str] = mapped_column(ForeignKey("practices.id"), nullable=False, index=True)
    phone_hash: Mapped[str] = mapped_column(String, nullable=False)
    # export, erase
    kind: Mapped[str] = mapped_column(String, nullable=False)
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class Alert(Base):
    """Something urgent a person must see (urgent message, emergency, unanswered handoff,
    failover, cost cap). Unacknowledged ones go to the backup contact (OP9)."""

    __tablename__ = "alerts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    practice_id: Mapped[str | None] = mapped_column(ForeignKey("practices.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    subject: Mapped[str] = mapped_column(SecretText, default="")
    body: Mapped[str] = mapped_column(SecretText, default="")
    dedupe_key: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    acked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(nullable=True)


class AdminRequest(Base):
    """A change the business asked for by SMS or by phone (OP2), waiting for their yes."""

    __tablename__ = "admin_requests"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    practice_id: Mapped[str] = mapped_column(ForeignKey("practices.id"), nullable=False, index=True)
    # sms, phone
    channel: Mapped[str] = mapped_column(String, nullable=False)
    sender: Mapped[str] = mapped_column(SecretLookup, nullable=False, index=True)
    staff_id: Mapped[str | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    text: Mapped[str] = mapped_column(SecretText, default="")
    parsed: Mapped[dict] = mapped_column(JSON, default=dict)
    readback: Mapped[str] = mapped_column(SecretText, default="")
    # pending, applied, queued (sent for approval), cancelled, expired
    status: Mapped[str] = mapped_column(String, default="pending")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)


class CalendarConnection(Base):
    """A Google Calendar connected by its owner's sign-in (O3), instead of shared with the
    service account. The refresh token is encrypted at rest."""

    __tablename__ = "calendar_connections"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    practice_id: Mapped[str] = mapped_column(ForeignKey("practices.id"), nullable=False, index=True)
    staff_id: Mapped[str | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    google_email: Mapped[str] = mapped_column(SecretText, default="")
    calendar_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    refresh_token: Mapped[str] = mapped_column(SecretText, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class Message(Base):
    """A message taken for the business (C3)."""

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    practice_id: Mapped[str] = mapped_column(ForeignKey("practices.id"), nullable=False, index=True)
    call_id: Mapped[str | None] = mapped_column(ForeignKey("calls.id"), nullable=True)
    staff_id: Mapped[str | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    caller_name: Mapped[str] = mapped_column(SecretText, default="")
    callback_number: Mapped[str | None] = mapped_column(SecretLookup, nullable=True)
    # Non-medical words only (G3).
    reason: Mapped[str] = mapped_column(SecretText, default="")
    best_time: Mapped[str] = mapped_column(SecretText, default="")
    urgent: Mapped[bool] = mapped_column(Boolean, default=False)
    # new, done
    status: Mapped[str] = mapped_column(String, default="new")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class Notification(Base):
    """Outbox: every notification is queued here and retried until sent."""

    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    practice_id: Mapped[str | None] = mapped_column(ForeignKey("practices.id"), nullable=True, index=True)
    call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # call_summary, booking_customer, change_customer, message, urgent, handoff_unanswered, digest, monthly_report, handoff
    kind: Mapped[str] = mapped_column(String, nullable=False)
    # email, sms, push
    channel: Mapped[str] = mapped_column(String, nullable=False)
    recipient: Mapped[str] = mapped_column(SecretLookup, nullable=False)
    subject: Mapped[str] = mapped_column(SecretText, default="")
    body: Mapped[str] = mapped_column(SecretText, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    # pending (also awaiting configuration), sent, failed (gave up), skipped (dedupe marker)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Same event never queued twice (e.g. digest:<practice>:<date>).
    dedupe_key: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(nullable=True)


class RoutingEvent(Base):
    """Every routing decision with the rule that made it (R9)."""

    __tablename__ = "routing_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    practice_id: Mapped[str] = mapped_column(ForeignKey("practices.id"), nullable=False, index=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), nullable=False, index=True)
    # intent, staff, hours, known_caller, emergency, human, language, department
    kind: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[str] = mapped_column(String, default="")
    # e.g. "R2 staff alias", "R6 ask twice"
    rule: Mapped[str] = mapped_column(String, default="")
    path: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class Handoff(Base):
    """A caller being handed to a person: in-app join (W1) or SIP transfer (C6, C7)."""

    __tablename__ = "handoffs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    practice_id: Mapped[str] = mapped_column(ForeignKey("practices.id"), nullable=False, index=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), nullable=False)
    staff_id: Mapped[str | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    room_name: Mapped[str] = mapped_column(String, nullable=False)
    # app or sip
    mode: Mapped[str] = mapped_column(String, default="app")
    # ringing, joined, unanswered, transferred, failed
    status: Mapped[str] = mapped_column(String, default="ringing")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)


class WaitlistEntry(Base):
    """A caller who wants an earlier or different slot; offered freed slots (use case C)."""

    __tablename__ = "waitlist"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    practice_id: Mapped[str] = mapped_column(ForeignKey("practices.id"), nullable=False, index=True)
    customer_name: Mapped[str] = mapped_column(SecretText, default="")
    phone: Mapped[str] = mapped_column(SecretLookup, nullable=False)
    service_id: Mapped[str] = mapped_column(String, nullable=False)
    staff_id: Mapped[str | None] = mapped_column(String, nullable=True)
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    part_of_day: Mapped[str | None] = mapped_column(String, nullable=True)
    # waiting, offered, booked, expired
    status: Mapped[str] = mapped_column(String, default="waiting")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class Device(Base):
    """An iOS device that gets push for a practice (handoffs, urgent messages)."""

    __tablename__ = "devices"

    token: Mapped[str] = mapped_column(String, primary_key=True)
    practice_id: Mapped[str | None] = mapped_column(ForeignKey("practices.id"), nullable=True)
    staff_id: Mapped[str | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    # sandbox or production APNs
    environment: Mapped[str] = mapped_column(String, default="sandbox")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
