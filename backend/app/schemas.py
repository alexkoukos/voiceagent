import datetime as dt
import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from app.languages import LANGUAGES, language_for_phone

from app.models import CallStatus, TranscriptRole


# Caps on free text that ends up in the agent's prompt.
SHORT_TEXT = 200
LONG_TEXT = 2000

Voice = Literal["default", "Puck", "Charon", "Fenrir", "Algenib", "Algieba", "Zubenelgenubi", "Kore", "Aoede"]


def normalize_phone(value: str) -> str:
    """'+30 690 762-6384' / '0030 (690) 7626384' -> '+306907626384'."""
    cleaned = re.sub(r"[\s\-(). ]", "", value)
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    return cleaned


class FriendCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone_number: str = Field(pattern=r"^\+\d{8,15}$")

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("phone_number", mode="before")
    @classmethod
    def _normalize_phone(cls, v):
        return normalize_phone(v) if isinstance(v, str) else v


class FriendOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    phone_number: str

    @computed_field
    @property
    def language(self) -> str:
        """Language a call to this friend starts in, from the phone prefix."""
        return language_for_phone(self.phone_number)


class CallCreate(BaseModel):
    friend_id: str
    # The call's free-text description is `scenario`; persona is only sent by older app builds.
    persona: str = Field(default="", max_length=LONG_TEXT)
    scenario: str = Field(min_length=1, max_length=LONG_TEXT)
    context: str = Field(default="", max_length=LONG_TEXT)
    reveal: str = Field(default="", max_length=LONG_TEXT)
    voice: Voice = "default"
    max_duration_seconds: int = Field(default=300, gt=0)
    # Show the owner's verified number (OWN_CALLER_NUMBER) instead of the trunk number.
    from_own_number: bool = False
    # Starting language; defaults to the friend's phone prefix.
    language: str | None = None

    @field_validator("language")
    @classmethod
    def _known_language(cls, v):
        if v is not None and v not in LANGUAGES:
            raise ValueError("unsupported language")
        return v


class CallEvent(BaseModel):
    status: CallStatus | None = None
    transcript_role: TranscriptRole | None = None
    transcript_text: str | None = None
    recording_url: str | None = None
    delete_recording: bool = False
    end_reason: Literal["no_answer", "declined", "unreachable", "error"] | None = None
    # Receptionist calls: median reply latency and flags the agent noticed.
    latency_ms_median: int | None = None
    flags: list[Literal["over_duration", "recording_refused", "tool_error", "name_uncertain"]] = []


class TranscriptEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    role: TranscriptRole
    text: str
    created_at: datetime


class CallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    friend_id: str | None
    status: CallStatus
    persona: str
    scenario: str
    voice: str
    from_own_number: bool
    language: str | None
    recording_url: str | None
    duration_seconds: int | None
    end_reason: str | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None


class CallDetailOut(CallOut):
    transcript_entries: list[TranscriptEntryOut] = []


class PromptTemplateCreate(BaseModel):
    title: str = Field(min_length=1, max_length=SHORT_TEXT)
    # The call's free-text description is `scenario`; persona is only sent by older app builds.
    persona: str = Field(default="", max_length=LONG_TEXT)
    scenario: str = Field(min_length=1, max_length=LONG_TEXT)
    context: str = Field(default="", max_length=LONG_TEXT)
    reveal: str = Field(default="", max_length=LONG_TEXT)
    voice: Voice | None = None


class PromptTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    persona: str
    scenario: str
    context: str
    reveal: str
    voice: str | None = None


# --- 2.0 receptionist ---

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class Service(BaseModel):
    id: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9_\-]+$")
    name: str = Field(min_length=1, max_length=SHORT_TEXT)
    duration_minutes: int = Field(gt=0, le=480)
    price: str | None = Field(default=None, max_length=SHORT_TEXT)


class BookingRules(BaseModel):
    slot_step_minutes: int = Field(default=15, gt=0, le=240)
    buffer_minutes: int = Field(default=0, ge=0, le=120)
    max_days_ahead: int = Field(default=60, gt=0, le=365)
    min_notice_minutes: int = Field(default=60, ge=0)
    holidays: list[date] = []


class Department(BaseModel):
    id: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9_\-]+$")
    name: str = Field(min_length=1, max_length=SHORT_TEXT)
    aliases: list[str] = []
    service_ids: list[str] = []
    staff_ids: list[str] = []


class NotificationSettings(BaseModel):
    emails: list[str] = []
    # Numbers that get urgent alerts by SMS (emergency, urgent message, unanswered handoff).
    urgent_sms: list[str] = []
    customer_sms: bool = True
    digest_time: str = Field(default="20:00", pattern=HHMM)
    monthly_report: bool = True
    # Also email summaries of web demo calls.
    web_summaries: bool = False


class ReminderSettings(BaseModel):
    enabled: bool = False
    time: str = Field(default="18:00", pattern=HHMM)
    waitlist: bool = False


class PracticeIn(BaseModel):
    name: str = Field(min_length=1, max_length=SHORT_TEXT)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9\-]{6,64}$")
    timezone: str = "Europe/Athens"
    language: Literal["el", "en"] = "el"
    voice: Voice = "Zubenelgenubi"
    greeting: str = Field(default="", max_length=LONG_TEXT)
    phone_numbers: list[str] = []
    # "mon".."sun" -> [["09:00", "14:00"], ["17:00", "21:00"]]
    hours: dict[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"], list[tuple[str, str]]] = {}
    services: list[Service] = Field(min_length=1)
    rules: BookingRules = BookingRules()
    knowledge_base: dict[str, str] = {}
    calendar_id: str | None = None
    vertical: str = ""
    routing_rules: dict = {}
    departments: list[Department] = []
    notifications: NotificationSettings = NotificationSettings()
    reminders: ReminderSettings = ReminderSettings()
    outbound_number: str | None = None
    max_concurrent_calls: int = Field(default=2, ge=1, le=20)
    retention_recordings_days: int = Field(default=30, ge=1, le=3650)
    retention_transcripts_days: int = Field(default=90, ge=1, le=3650)
    avg_booking_value: float = Field(default=0, ge=0)
    guarantee_threshold: int = Field(default=10, ge=0)
    monthly_cost_cap_eur: float | None = Field(default=None, gt=0)
    blocked_numbers: list[str] = []

    @field_validator("phone_numbers", "blocked_numbers")
    @classmethod
    def _numbers(cls, v):
        out = [normalize_phone(n) for n in v]
        for n in out:
            if not re.fullmatch(r"\+\d{8,15}", n):
                raise ValueError(f"bad phone number: {n}")
        return out

    @field_validator("hours")
    @classmethod
    def _hours(cls, v):
        for spans in v.values():
            for a, b in spans:
                if not (re.fullmatch(HHMM, a) and re.fullmatch(HHMM, b) and a < b):
                    raise ValueError(f"bad opening hours: {a}-{b}")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v):
        from zoneinfo import ZoneInfo
        ZoneInfo(v)
        return v

    def to_columns(self) -> dict:
        data = self.model_dump(mode="json")
        data["hours"] = {k: [list(s) for s in spans] for k, spans in data["hours"].items()}
        return data


class PracticeOut(PracticeIn):
    model_config = ConfigDict(from_attributes=True)

    id: str
    services: list[Service]


class AppointmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    call_id: str | None
    customer_name: str
    customer_phone: str | None
    service_id: str
    service_name: str
    starts_at: datetime
    ends_at: datetime
    status: str
    gcal_event_id: str | None


class InboundStart(BaseModel):
    """The agent asks which practice a SIP call belongs to (by the dialed number)."""
    dialed_number: str
    caller_number: str | None = None


class CheckAvailability(BaseModel):
    when: str = Field(min_length=1, max_length=SHORT_TEXT)
    service_id: str | None = Field(default=None, max_length=SHORT_TEXT)
    staff: str | None = Field(default=None, max_length=SHORT_TEXT)
    # When moving an appointment: its own slot doesn't count as busy.
    appointment_id: str | None = None
    # "νωρίτερα" / "αργότερα": only times strictly after / before this HH:MM.
    after: str | None = Field(default=None, pattern=r"^([01]?\d|2[0-3]):[0-5]\d$")
    before: str | None = Field(default=None, pattern=r"^([01]?\d|2[0-3]):[0-5]\d$")


class BookAppointment(BaseModel):
    date: dt.date
    time: str = Field(pattern=HHMM)
    service_id: str = Field(max_length=SHORT_TEXT)
    customer_name: str = Field(min_length=1, max_length=SHORT_TEXT)
    customer_phone: str | None = Field(default=None, max_length=30)
    staff: str | None = Field(default=None, max_length=SHORT_TEXT)
    name_uncertain: bool = False
    confirmation_id: str | None = None
    confirmation_text: str | None = Field(default=None, max_length=SHORT_TEXT)

    @field_validator("customer_phone")
    @classmethod
    def _phone(cls, v):
        return normalize_phone(v) if v else v


class StaffIn(BaseModel):
    name: str = Field(min_length=1, max_length=SHORT_TEXT)
    role: Literal["doctor", "secretary", "owner", "staff"] = "staff"
    aliases: list[str] = []
    service_ids: list[str] = []
    hours: dict[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"], list[tuple[str, str]]] = {}
    calendar_id: str | None = None
    phone: str | None = None
    email: str | None = None
    bookable: bool = True
    active: bool = True

    @field_validator("phone")
    @classmethod
    def _phone(cls, v):
        return normalize_phone(v) if v else v

    def to_columns(self) -> dict:
        data = self.model_dump(mode="json")
        data["hours"] = {k: [list(x) for x in spans] for k, spans in data["hours"].items()}
        return data


class StaffOut(StaffIn):
    model_config = ConfigDict(from_attributes=True)

    id: str


class ReceptionistCallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    practice_id: str | None
    direction: str
    caller_number: str | None
    status: CallStatus
    use_case: str | None
    outcome: str | None
    appointment_id: str | None
    summary: str | None
    flags: list[str] = []
    cost_estimate: float | None
    latency_ms_median: int | None
    hours_state: str | None
    purpose: str | None
    review: dict | None
    recording_url: str | None
    duration_seconds: int | None
    end_reason: str | None
    language: str | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None


class RoutingEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: str
    value: str
    rule: str
    path: str
    created_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    call_id: str | None
    staff_id: str | None
    caller_name: str
    callback_number: str | None
    reason: str
    best_time: str
    urgent: bool
    status: str
    created_at: datetime


class HandoffOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    call_id: str
    staff_id: str | None
    mode: str
    status: str
    created_at: datetime


class ReceptionistCallDetail(ReceptionistCallOut):
    transcript_entries: list[TranscriptEntryOut] = []
    routing: list[RoutingEventOut] = []
    messages: list[MessageOut] = []
    handoffs: list[HandoffOut] = []
    appointment: AppointmentOut | None = None


class CallReview(BaseModel):
    routing_correct: bool | None = None
    booking_correct: bool | None = None
    note: str = Field(default="", max_length=LONG_TEXT)


class MessageUpdate(BaseModel):
    status: Literal["new", "done"]


class HandoffJoin(BaseModel):
    listen_only: bool = False


class DeviceIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    practice_id: str | None = None
    staff_id: str | None = None
    environment: Literal["sandbox", "production"] = "sandbox"


class AppointmentCreate(BaseModel):
    date: dt.date
    time: str = Field(pattern=HHMM)
    service_id: str
    customer_name: str = Field(min_length=1, max_length=SHORT_TEXT)
    customer_phone: str | None = None
    staff: str | None = None

    @field_validator("customer_phone")
    @classmethod
    def _phone(cls, v):
        return normalize_phone(v) if v else v


class AppointmentMove(BaseModel):
    date: dt.date
    time: str = Field(pattern=HHMM)


class ClosureIn(BaseModel):
    """"Κλειστά 10 έως 25 Αυγούστου" or "ο Γιώργος λείπει Παρασκευή" (OP3). Both days included."""
    date_from: dt.date
    date_to: dt.date
    staff_id: str | None = None
    reason: str | None = Field(default=None, max_length=200)

    @field_validator("date_to")
    @classmethod
    def _not_before_start(cls, v: dt.date, info) -> dt.date:
        if "date_from" in info.data and v < info.data["date_from"]:
            raise ValueError("date_to is before date_from")
        return v


class ClosureOut(BaseModel):
    id: str
    date_from: dt.date
    date_to: dt.date
    staff_id: str | None
    reason: str | None
    # Booked appointments inside the closure: they need rebooking.
    to_rebook: list[AppointmentOut]


class ConfigVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    source: str
    author: str
    summary: str
    changes: dict
    created_at: datetime
    decided_at: datetime | None


class AdminLinkIn(BaseModel):
    staff_id: str | None = None
    hours: int = Field(default=72, ge=1, le=24 * 30)


class AdminLinkOut(BaseModel):
    url: str
    expires_at: datetime


class DataRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    counts: dict
    created_at: datetime


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    practice_id: str | None
    kind: str
    call_id: str | None
    subject: str
    body: str
    created_at: datetime
    acked_at: datetime | None
    escalated_at: datetime | None


class PhoneIn(BaseModel):
    phone: str

    @field_validator("phone")
    @classmethod
    def _phone(cls, v):
        v = normalize_phone(v)
        if not re.fullmatch(r"\+\d{8,15}", v):
            raise ValueError("bad phone number")
        return v


class CostCapIn(BaseModel):
    monthly_cost_cap_eur: float | None = Field(default=None, gt=0)


class UsageOut(BaseModel):
    month_cost_eur: float
    monthly_cost_cap_eur: float | None
    blocked_numbers: list[str]
    offboarded_at: datetime | None


class WaitlistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    customer_name: str
    phone: str
    service_id: str
    date_from: dt.date
    date_to: dt.date
    part_of_day: str | None
    status: str
    created_at: datetime


# --- agent tool arguments ---


class RouteArgs(BaseModel):
    intent: str = Field(max_length=30)
    staff: str | None = Field(default=None, max_length=SHORT_TEXT)
    department: str | None = Field(default=None, max_length=SHORT_TEXT)
    language: str | None = Field(default=None, max_length=5)


class FindArgs(BaseModel):
    phone: str | None = Field(default=None, max_length=30)


class RescheduleArgs(BaseModel):
    appointment_id: str
    date: dt.date
    time: str = Field(pattern=HHMM)
    confirmation_id: str | None = None
    confirmation_text: str | None = Field(default=None, max_length=SHORT_TEXT)


class AppointmentRef(BaseModel):
    appointment_id: str
    confirmation_id: str | None = None
    confirmation_text: str | None = Field(default=None, max_length=SHORT_TEXT)


class PrepareAction(BaseModel):
    action: Literal["book", "reschedule", "cancel"]
    date: dt.date | None = None
    time: str | None = Field(default=None, pattern=HHMM)
    service_id: str | None = Field(default=None, max_length=SHORT_TEXT)
    customer_name: str | None = Field(default=None, max_length=SHORT_TEXT)
    staff: str | None = Field(default=None, max_length=SHORT_TEXT)
    appointment_id: str | None = None


class MessageArgs(BaseModel):
    caller_name: str = Field(default="", max_length=SHORT_TEXT)
    callback_number: str | None = Field(default=None, max_length=30)
    reason: str = Field(default="", max_length=LONG_TEXT)
    best_time: str = Field(default="", max_length=SHORT_TEXT)
    urgent: bool = False
    for_whom: str | None = Field(default=None, max_length=SHORT_TEXT)


class TransferArgs(BaseModel):
    target: str | None = Field(default=None, max_length=SHORT_TEXT)


class HandoffResult(BaseModel):
    handoff_id: str
    status: Literal["joined", "unanswered", "transferred", "failed"]


class WaitlistArgs(BaseModel):
    when: str = Field(max_length=SHORT_TEXT)
    service_id: str = Field(max_length=SHORT_TEXT)
    customer_name: str = Field(default="", max_length=SHORT_TEXT)
    days: int = 7


class FlagArgs(BaseModel):
    flag: Literal["name_uncertain", "recording_refused", "over_duration", "tool_error"]
