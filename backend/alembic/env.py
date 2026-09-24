import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.database import Base
from app import models  # noqa: F401  (registers tables on Base.metadata)

config = context.config
target_metadata = Base.metadata


def _run(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online():
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=get_settings().database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
