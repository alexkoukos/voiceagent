import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import CallStatus, TranscriptRole


# Caps on free text that ends up in the agent's prompt.
SHORT_TEXT = 200
LONG_TEXT = 2000

Voice = Literal["default", "Puck", "Charon", "Fenrir", "Kore", "Aoede"]


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


class CallCreate(BaseModel):
    friend_id: str
    persona: str = Field(min_length=1, max_length=LONG_TEXT)
    scenario: str = Field(min_length=1, max_length=LONG_TEXT)
    context: str = Field(default="", max_length=LONG_TEXT)
    reveal: str = Field(default="", max_length=LONG_TEXT)
    voice: Voice = "default"
    max_duration_seconds: int = Field(default=300, gt=0)
    # Show the owner's verified number (OWN_CALLER_NUMBER) instead of the trunk number.
    from_own_number: bool = False


class CallEvent(BaseModel):
    status: CallStatus | None = None
    transcript_role: TranscriptRole | None = None
    transcript_text: str | None = None
    recording_url: str | None = None
    delete_recording: bool = False


class TranscriptEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    role: TranscriptRole
    text: str
    created_at: datetime


class CallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    friend_id: str
    status: CallStatus
    persona: str
    scenario: str
    voice: str
    from_own_number: bool
    recording_url: str | None
    duration_seconds: int | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None


class CallDetailOut(CallOut):
    transcript_entries: list[TranscriptEntryOut] = []


class PromptTemplateCreate(BaseModel):
    title: str = Field(min_length=1, max_length=SHORT_TEXT)
    persona: str = Field(min_length=1, max_length=LONG_TEXT)
    scenario: str = Field(min_length=1, max_length=LONG_TEXT)
    context: str = Field(default="", max_length=LONG_TEXT)
    reveal: str = Field(default="", max_length=LONG_TEXT)


class PromptTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    persona: str
    scenario: str
    context: str
    reveal: str
