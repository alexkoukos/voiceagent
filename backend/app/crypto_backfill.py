"""Encrypts patient data written before DATA_ENCRYPTION_KEY was set (see app/crypto.py).

One transaction: every value is encrypted, decrypted again and compared before commit, so a
wrong key or a bug rolls everything back and nothing is half-encrypted. Idempotent.
Runs from scripts/encrypt_existing.py, or at startup when ENCRYPT_BACKFILL=1.
"""

import logging

from sqlalchemy import text

from app import crypto
from app.database import Base, engine

logger = logging.getLogger(__name__)


async def run() -> dict[str, int]:
    if not crypto.enabled():
        raise crypto.KeyError_("DATA_ENCRYPTION_KEY is not set")
    import app.models  # noqa: F401  (registers the tables)
    counts: dict[str, int] = {}
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
                    sealed = crypto.encrypt(value, lookup=lookup)
                    if crypto.decrypt(sealed) != value:
                        raise RuntimeError(f"{table.name}.{name}: round trip failed, nothing committed")
                    await conn.execute(text(f'UPDATE "{table.name}" SET "{name}" = :v WHERE "{pk}" = :k'),
                                       {"v": sealed, "k": key})
                if rows:
                    counts[f"{table.name}.{name}"] = len(rows)
    logger.warning("encryption backfill committed: %s", counts or "nothing to do")
    return counts
