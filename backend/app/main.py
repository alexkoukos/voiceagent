from fastapi import Depends, FastAPI

from app.auth import require_app_token
from app.config import get_settings
from app.languages import LANGUAGES
from app.routers import calls, friends, internal, templates, webhooks

_docs = get_settings().enable_docs

app = FastAPI(
    title="AI Caller",
    docs_url="/docs" if _docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _docs else None,
)

app.include_router(friends.router, dependencies=[Depends(require_app_token)])
app.include_router(calls.router, dependencies=[Depends(require_app_token)])
app.include_router(templates.router, dependencies=[Depends(require_app_token)])
app.include_router(internal.router)
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
