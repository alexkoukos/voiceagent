"""Encrypts patient data written before DATA_ENCRYPTION_KEY was set (see app/crypto.py).

    DATABASE_URL=... DATA_ENCRYPTION_KEY=... \
    uv run --python 3.12 --with-requirements requirements.txt python scripts/encrypt_existing.py

Safe to run twice: values already encrypted are skipped. Back up the database first, and keep
the key: without it the encrypted rows can't be read.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app import crypto  # noqa: E402
from app.database import engine  # noqa: E402
from app.models import Base  # noqa: E402


async def main() -> None:
    if not crypto.enabled():
        sys.exit("DATA_ENCRYPTION_KEY is not set")
    async with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            cols = [(c.name, isinstance(c.type, crypto.SecretLookup)) for c in table.columns
                    if isinstance(c.type, (crypto.SecretText, crypto.SecretLookup))]
            if not cols:
                continue
            pk = table.primary_key.columns.values()[0].name
            for name, lookup in cols:
                rows = (await conn.execute(text(
                    f'SELECT "{pk}", "{name}" FROM "{table.name}" '
                    f"WHERE \"{name}\" IS NOT NULL AND \"{name}\" <> '' AND \"{name}\" NOT LIKE 'enc1%'"
                ))).all()
                for key, value in rows:
                    await conn.execute(text(f'UPDATE "{table.name}" SET "{name}" = :v WHERE "{pk}" = :k'),
                                       {"v": crypto.encrypt(value, lookup=lookup), "k": key})
                print(f"{table.name}.{name}: {len(rows)} encrypted")


if __name__ == "__main__":
    asyncio.run(main())
