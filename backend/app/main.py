from fastapi import Depends, FastAPI

from app.auth import require_app_token
from app.routers import calls, friends, internal, templates


app = FastAPI(title="AI Prank Caller")

app.include_router(friends.router, dependencies=[Depends(require_app_token)])
app.include_router(calls.router, dependencies=[Depends(require_app_token)])
app.include_router(templates.router, dependencies=[Depends(require_app_token)])
app.include_router(internal.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
