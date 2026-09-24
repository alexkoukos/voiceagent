from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import CallStatus, TranscriptRole


class FriendCreate(BaseModel):
    name: str = Field(min_length=1)
    phone_number: str = Field(pattern=r"^\+\d{8,15}$")


class FriendOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    phone_number: str


class CallCreate(BaseModel):
    friend_id: str
    persona: str
    scenario: str
    context: str = ""
    reveal: str = ""
    voice: str = "default"
    max_duration_seconds: int = Field(default=300, gt=0)


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
    recording_url: str | None
    duration_seconds: int | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None


class CallDetailOut(CallOut):
    transcript_entries: list[TranscriptEntryOut] = []


class PromptTemplateCreate(BaseModel):
    title: str
    persona: str
    scenario: str
    context: str = ""
    reveal: str = ""


class PromptTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    persona: str
    scenario: str
    context: str
    reveal: str
