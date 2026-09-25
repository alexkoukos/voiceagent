import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

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


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    friend_id: Mapped[str] = mapped_column(ForeignKey("friends.id"), nullable=False)

    persona: Mapped[str] = mapped_column(Text, nullable=False)
    scenario: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[str] = mapped_column(Text, default="")
    reveal: Mapped[str] = mapped_column(Text, default="")
    voice: Mapped[str] = mapped_column(String, default="default")
    max_duration_seconds: Mapped[int] = mapped_column(Integer, default=300)
    from_own_number: Mapped[bool] = mapped_column(Boolean, default=False)

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

    friend: Mapped["Friend"] = relationship(back_populates="calls")
    transcript_entries: Mapped[list["TranscriptEntry"]] = relationship(
        back_populates="call", order_by="TranscriptEntry.created_at"
    )


class TranscriptEntry(Base):
    __tablename__ = "transcript_entries"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), nullable=False)
    role: Mapped[TranscriptRole] = mapped_column(Enum(TranscriptRole), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    call: Mapped["Call"] = relationship(back_populates="transcript_entries")
