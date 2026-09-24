from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.auth import require_app_token
from app.database import Base, engine
from app.routers import calls, friends, internal, templates


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Dev convenience only — replace with Alembic migrations before deploying.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(title="AI Prank Caller", lifespan=lifespan)

app.include_router(friends.router, dependencies=[Depends(require_app_token)])
app.include_router(calls.router, dependencies=[Depends(require_app_token)])
app.include_router(templates.router, dependencies=[Depends(require_app_token)])
app.include_router(internal.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
