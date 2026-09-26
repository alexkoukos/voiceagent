import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.auth import require_app_token
from app.config import get_settings
from app.languages import LANGUAGES
from app.routers import calls, demo, friends, internal, manage, ops, practices, templates, webhooks

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
app.include_router(calls.router, dependencies=[Depends(require_app_token)])
app.include_router(templates.router, dependencies=[Depends(require_app_token)])
app.include_router(practices.router, dependencies=[Depends(require_app_token)])
app.include_router(practices.misc, dependencies=[Depends(require_app_token)])
app.include_router(ops.router, dependencies=[Depends(require_app_token)])
app.include_router(ops.alerts_router, dependencies=[Depends(require_app_token)])
app.include_router(internal.router)
app.include_router(demo.router)
app.include_router(manage.router)
app.include_router(webhooks.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/options", dependencies=[Depends(require_app_token)])
async def options():
    """What the app may offer; never exposes the numbers themselves."""
    return {
        "own_number_available": bool(get_settings().own_caller_number),
        "languages": [{"code": c, "name": n} for c, (n, _) in LANGUAGES.items()],
    }
