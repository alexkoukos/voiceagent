"""Calls are Greek; English only when the caller says "English mode" (never the model's guess)."""

from datetime import datetime, timezone

import pytest

from app import receptionist, routing
from app.models import Call, CallStatus, Practice
from app.schemas import LanguageArgs


@pytest.mark.asyncio
async def test_greek_by_default_english_only_on_request(sessions):
    async with sessions() as db:
        practice = Practice(name="Test", timezone="Europe/Athens", hours={},
                            services=[{"id": "check", "name": "Check-up", "duration_minutes": 30}])
        db.add(practice)
        await db.commit()
        # A foreign number no longer starts the call in English.
        assert receptionist.call_language(practice, "+447700900123") == "el"
        call = Call(practice_id=practice.id, direction="inbound", caller_number="+447700900123", persona="",
                    scenario="", status=CallStatus.active, language="el")
        db.add(call)
        await db.commit()

        # The model saying "language=en" on route_call changes nothing.
        await routing.route(db, practice, call, intent="question", language="en", now=datetime.now(timezone.utc))
        assert call.language == "el"

        assert (await receptionist.tool_set_language(db, call, LanguageArgs(language="en")))["ok"]
        assert (await db.get(Call, call.id)).language == "en"
        meta = await receptionist.build_metadata(db, practice, call)
        assert meta["language"] == "en" and "English" in meta["prompts"]["en"]
