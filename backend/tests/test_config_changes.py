"""OP2: every change is a version with rollback; a doctor's link publishes hours and
closures but queues prices and FAQ for approval."""

from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException

from app import config_changes
from app.models import AdminLink, Practice, Staff
from app.routers import manage
from app.routers import practices as practices_router
from app.schemas import AdminLinkIn, ClosureIn, PracticeIn, PracticeOut

HOURS = {"mon": [["09:00", "14:00"]]}
SERVICES = [{"id": "check", "name": "Check-up", "duration_minutes": 30, "price": "40€"}]


async def _practice(db) -> Practice:
    practice = Practice(name="Test", timezone="Europe/Athens", hours=HOURS, services=SERVICES,
                        rules={"min_notice_minutes": 0}, knowledge_base={"parking": "Δίπλα"})
    db.add(practice)
    await db.commit()
    return practice


def _token(url: str) -> str:
    return url.rsplit("/", 1)[1]


@pytest.mark.asyncio
async def test_app_update_keeps_closures_and_rolls_back(sessions):
    async with sessions() as db:
        practice = await _practice(db)
        await practices_router.add_closure(practice.id, ClosureIn(
            date_from=date(2030, 8, 10), date_to=date(2030, 8, 25)), db)

        body = PracticeOut.model_validate(await db.get(Practice, practice.id)).model_dump()
        body["hours"] = {"mon": [["10:00", "13:00"]]}
        await practices_router.update_practice(practice.id, PracticeIn.model_validate(body), db)
        practice = await db.get(Practice, practice.id)
        assert practice.hours == {"mon": [["10:00", "13:00"]]}
        assert len(practice.rules["closures"]) == 1

        versions = await practices_router.list_versions(practice.id, db=db)
        assert [v.source for v in versions][-1] == "baseline"
        baseline = versions[-1]
        await practices_router.rollback_version(practice.id, baseline.id, db)
        practice = await db.get(Practice, practice.id)
        assert practice.hours == HOURS
        assert not (practice.rules.get("closures") or [])
        assert (await practices_router.list_versions(practice.id, db=db))[0].source == "rollback"


@pytest.mark.asyncio
async def test_doctor_link_publishes_hours_and_queues_prices(sessions):
    async with sessions() as db:
        practice = await _practice(db)
        token = _token((await practices_router.create_link(practice.id, AdminLinkIn(), db)).url)

        state = await manage.state(token, db)
        assert state["services"][0]["price"] == "40€"

        await manage.put_hours(token, manage.HoursIn(hours={"mon": [["09:00", "13:00"]], "tue": []}), db)
        assert (await db.get(Practice, practice.id)).hours["mon"] == [["09:00", "13:00"]]
        with pytest.raises(HTTPException) as bad:
            await manage.put_hours(token, manage.HoursIn(hours={"mon": [["14:00", "09:00"]]}), db)
        assert bad.value.status_code == 422

        new = [{"id": "check", "name": "Check-up", "duration_minutes": 30, "price": "50€"}]
        out = await manage.put_services(token, manage.ServicesIn(services=new), db)
        assert out["status"] == "pending"
        assert (await db.get(Practice, practice.id)).services[0]["price"] == "40€"
        await manage.put_knowledge(token, manage.KnowledgeIn(knowledge_base={"parking": "Όχι"}), db)

        queue = await practices_router.list_versions(practice.id, status="pending", db=db)
        assert len(queue) == 2
        prices = next(v for v in queue if "services" in v.changes)
        faq = next(v for v in queue if "knowledge_base" in v.changes)
        await practices_router.approve_version(practice.id, prices.id, db)
        await practices_router.reject_version(practice.id, faq.id, db)
        practice = await db.get(Practice, practice.id)
        assert practice.services[0]["price"] == "50€"
        assert practice.hours["mon"] == [["09:00", "13:00"]]
        assert practice.knowledge_base == {"parking": "Δίπλα"}
        with pytest.raises(HTTPException):
            await practices_router.approve_version(practice.id, faq.id, db)


@pytest.mark.asyncio
async def test_staff_link_only_sets_own_leave_and_links_expire(sessions):
    async with sessions() as db:
        practice = await _practice(db)
        giorgos, maria = Staff(practice_id=practice.id, name="Γιώργος"), Staff(practice_id=practice.id, name="Μαρία")
        db.add_all([giorgos, maria])
        await db.commit()
        token = _token((await practices_router.create_link(practice.id, AdminLinkIn(staff_id=giorgos.id), db)).url)

        for call in (manage.put_hours(token, manage.HoursIn(hours=HOURS), db),
                     manage.put_services(token, manage.ServicesIn(services=SERVICES), db)):
            with pytest.raises(HTTPException) as denied:
                await call
            assert denied.value.status_code == 403

        await manage.post_closure(token, manage.ClosureBody(
            date_from=date(2030, 5, 1), date_to=date(2030, 5, 1), staff_id=maria.id), db)
        closure = (await db.get(Practice, practice.id)).rules["closures"][0]
        assert closure["staff_id"] == giorgos.id

        await practices_router.add_closure(practice.id, ClosureIn(
            date_from=date(2030, 6, 1), date_to=date(2030, 6, 2), staff_id=maria.id), db)
        other = next(c for c in (await db.get(Practice, practice.id)).rules["closures"] if c["staff_id"] == maria.id)
        with pytest.raises(HTTPException) as denied:
            await manage.delete_closure(token, other["id"], db)
        assert denied.value.status_code == 403

        link = await db.get(AdminLink, config_changes._hash(token))
        link.expires_at = datetime.utcnow() - timedelta(seconds=1)
        await db.commit()
        with pytest.raises(HTTPException) as gone:
            await manage.state(token, db)
        assert gone.value.status_code == 404
        assert (await manage.page(token, db)).status_code == 404

        fresh = _token((await practices_router.create_link(practice.id, AdminLinkIn(), db)).url)
        await practices_router.revoke_links(practice.id, db)
        with pytest.raises(HTTPException):
            await manage.state(fresh, db)
