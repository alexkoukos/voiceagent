"""Encrypts patient data written before DATA_ENCRYPTION_KEY was set (app/crypto_backfill.py).

    DATABASE_URL=... DATA_ENCRYPTION_KEY=... \
    uv run --python 3.12 --with-requirements requirements.txt python scripts/encrypt_existing.py

On Railway, set ENCRYPT_BACKFILL=1 instead: the backend runs it once at startup.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import crypto_backfill  # noqa: E402

if __name__ == "__main__":
    for column, n in asyncio.run(crypto_backfill.run()).items():
        print(f"{column}: {n} encrypted")
