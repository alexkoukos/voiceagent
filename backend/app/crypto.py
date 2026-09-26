"""Field encryption for patient data at rest (GDPR Art. 9 for health practices).

Anyone who gets the database (a leaked URL, a backup) should not be able to read who called
a doctor and why. Two column types:

- `SecretText`: AES-GCM with a random nonce, for free text (transcripts, summaries, names,
  message reasons, notification bodies).
- `SecretLookup`: AES-SIV, deterministic, for phone numbers and other values found by
  equality. The same number always encrypts the same way, so `WHERE caller_number = :n`
  and unique constraints keep working; the cost is that equal values are visibly equal.

The key is DATA_ENCRYPTION_KEY (base64, 32 bytes), kept outside the database. Without it
values are stored as plain text (local development). Values written before the key was set
stay readable; scripts/encrypt_existing.py rewrites them. Losing the key loses the data:
keep a copy somewhere other than Railway.
"""

import base64
import logging
import os
from functools import lru_cache

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, AESSIV
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import String, Text
from sqlalchemy.types import TypeDecorator

from app.config import get_settings

logger = logging.getLogger(__name__)

RANDOM = "enc1:"
LOOKUP = "enc1d:"


class KeyError_(Exception):
    pass


@lru_cache
def _keys() -> tuple[AESGCM, AESSIV] | None:
    raw = get_settings().data_encryption_key
    if not raw:
        return None
    try:
        master = base64.b64decode(raw, validate=True)
    except ValueError:
        raise KeyError_("DATA_ENCRYPTION_KEY is not base64")
    if len(master) != 32:
        raise KeyError_("DATA_ENCRYPTION_KEY must be 32 bytes")

    def derive(info: bytes, length: int) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(master)

    return AESGCM(derive(b"voiceagent text v1", 32)), AESSIV(derive(b"voiceagent lookup v1", 64))


BLOB = b"ENC1"


def encrypt_bytes(data: bytes) -> bytes:
    keys = _keys()
    if keys is None:
        raise KeyError_("DATA_ENCRYPTION_KEY is not set")
    nonce = os.urandom(12)
    return BLOB + nonce + keys[0].encrypt(nonce, data, None)


def decrypt_bytes(blob: bytes) -> bytes:
    keys = _keys()
    if keys is None or not blob.startswith(BLOB):
        raise KeyError_("not an encrypted blob, or no key")
    return keys[0].decrypt(blob[4:16], blob[16:], None)


@lru_cache
def token_secret() -> bytes:
    """Signs short-lived playback links for encrypted recordings."""
    raw = get_settings().data_encryption_key
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"voiceagent links v1").derive(
        base64.b64decode(raw))


def enabled() -> bool:
    return _keys() is not None


def encrypt(value: str | None, *, lookup: bool = False) -> str | None:
    keys = _keys()
    if value is None or keys is None or value.startswith((RANDOM, LOOKUP)):
        return value
    data = value.encode()
    if lookup:
        return LOOKUP + base64.urlsafe_b64encode(keys[1].encrypt(data, None)).decode()
    nonce = os.urandom(12)
    return RANDOM + base64.urlsafe_b64encode(nonce + keys[0].encrypt(nonce, data, None)).decode()


def decrypt(value: str | None) -> str | None:
    if value is None or not value.startswith((RANDOM, LOOKUP)):
        return value
    keys = _keys()
    if keys is None:
        raise KeyError_("encrypted data but DATA_ENCRYPTION_KEY is not set")
    if value.startswith(LOOKUP):
        return keys[1].decrypt(base64.urlsafe_b64decode(value[len(LOOKUP):]), None).decode()
    blob = base64.urlsafe_b64decode(value[len(RANDOM):])
    return keys[0].decrypt(blob[:12], blob[12:], None).decode()


class SecretText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value)

    def process_result_value(self, value, dialect):
        return decrypt(value)


class SecretLookup(TypeDecorator):
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value, lookup=True)

    def process_result_value(self, value, dialect):
        return decrypt(value)
