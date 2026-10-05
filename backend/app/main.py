import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import is_master, require_app_token, require_admin_token, require_any_token
from app.config import get_settings
from app.database import get_db
from app.languages import LANGUAGES
from app.routers import calls, demo, demo_dashboard, friends, internal, landing, manage, monitor, oauth, ops, practices, recordings, sparring, templates, webhooks
from fastapi.staticfiles import StaticFiles
from pathlib import Path

_docs = get_settings().enable_docs


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Notification sender and the once-a-minute jobs (digests, reminders, retention).
    from app import crypto
    if not crypto.enabled():
        logging.getLogger("app").warning("DATA_ENCRYPTION_KEY is not set: patient data is stored unencrypted")
    elif get_settings().encrypt_backfill:
        from app import crypto_backfill
        await crypto_backfill.run()
    tasks = []
    if get_settings().scheduler_enabled:
        from app import notifications, scheduler
        tasks = [asyncio.create_task(notifications.run_worker()), asyncio.create_task(scheduler.run())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(
    title="AI Caller",
    lifespan=lifespan,
    docs_url="/docs" if _docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _docs else None,
)

app.include_router(friends.router, dependencies=[Depends(require_app_token)])
app.include_router(calls.router)
app.include_router(templates.router, dependencies=[Depends(require_app_token)])
app.include_router(practices.router, dependencies=[Depends(require_admin_token)])
app.include_router(practices.misc, dependencies=[Depends(require_admin_token)])
app.include_router(ops.router, dependencies=[Depends(require_admin_token)])
app.include_router(oauth.router, dependencies=[Depends(require_admin_token)])
app.include_router(ops.alerts_router, dependencies=[Depends(require_admin_token)])
app.include_router(internal.router)
app.include_router(demo.router)
app.include_router(demo_dashboard.router)
app.include_router(sparring.router)
app.include_router(manage.router)
app.include_router(recordings.router)
app.include_router(oauth.public)
app.include_router(webhooks.router)
app.include_router(landing.router)
app.include_router(monitor.router)
app.mount("/landing-assets", StaticFiles(directory=Path(__file__).resolve().parent / "templates" / "landing-assets"), name="landing-assets")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/options", dependencies=[Depends(require_any_token)])
async def options(db: AsyncSession = Depends(get_db), x_api_key: str = Header(default="")):
    """What the app may offer; never exposes the numbers themselves."""
    tenant = bool(db.info.get("tenant_id"))
    return {
        "account_scope": "practice" if tenant else "founder" if is_master(x_api_key) else "dialer",
        "own_number_available": not tenant and bool(get_settings().own_caller_number),
        "languages": [{"code": c, "name": n} for c, (n, _) in LANGUAGES.items()],
    }
