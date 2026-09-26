from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
MASTER_PROMPT_PATH = CONFIG_DIR / "master_prompt.md"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://user:password@localhost:5433/voiceagent"

    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    gemini_api_key: str = ""

    sip_trunk_id: str = ""
    sip_outbound_number: str = ""
    # The owner's own number, verified with Telnyx, for calls placed "from my number".
    own_caller_number: str = ""
    telnyx_public_key: str = ""

    # S3-compatible recording storage (a Railway bucket, Cloudflare R2, ...).
    # Named like the variables Railway buckets expose, so they can be referenced directly.
    aws_endpoint_url: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_s3_bucket_name: str = ""
    aws_default_region: str = "auto"
    aws_s3_url_style: str = "virtual-host"

    # Service account key (the whole JSON) for practices' Google Calendars.
    # Database-only booking requires an unassigned calendar_id.
    google_service_account_json: str = ""

    # Notifications. Email over SMTP (any provider); SMS through Telnyx Messaging;
    # push through APNs (needs a paid Apple developer account). Unset channels stay pending.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = ""
    telnyx_api_key: str = ""
    # Sender for SMS: a Telnyx number or a registered alphanumeric sender id.
    sms_from: str = ""
    apns_key_p8: str = ""
    apns_key_id: str = ""
    apns_team_id: str = ""
    apns_topic: str = "com.alekos.prankcaller"

    # Operator alerts (OP9): failover, cost caps, blocked spam, failed notifications, emergencies.
    # Unacknowledged after ALERT_ACK_MINUTES they also go to the backup contact.
    founder_email: str = ""
    founder_sms: str = ""
    backup_email: str = ""
    backup_sms: str = ""
    alert_ack_minutes: int = 30
    # OP1 health checks: LiveKit every 5 minutes; a no-op agent job every N minutes (0 = off).
    # Off by default: each agent job starts a ~400 MB process, and a probe during a live call
    # got the Railway agent OOM-killed before. Turn on (e.g. 15) once the agent has more memory.
    health_agent_check_minutes: int = 0

    # Call summaries (a cheap text model, after hang-up).
    summary_model: str = "gemini-3.5-flash-lite"
    # Onboarding (O1, O2): Google Places for the business profile, a vision model for price lists.
    google_maps_api_key: str = ""
    extraction_model: str = "gemini-3.5-flash"
    # Cost estimate per call, EUR per minute.
    cost_model_eur_per_min: float = 0.03
    cost_telephony_eur_per_min: float = 0.01
    # Background jobs: notification sender, digests, reminders, retention.
    scheduler_enabled: bool = True

    max_call_duration_seconds: int = 300
    max_concurrent_calls: int = 1

    # Agent the backend dispatches receptionist web calls to; "prank-caller-test" for a local worker.
    agent_name: str = "prank-caller"

    # Patient data encryption at rest (app/crypto.py): base64 of 32 random bytes. Keep a copy
    # outside Railway: without it encrypted data can't be read.
    data_encryption_key: str = ""
    # Encrypt older rows once at startup (app/crypto_backfill.py); unset afterwards.
    encrypt_backfill: bool = False
    internal_api_token: str = ""
    app_api_token: str = ""
    backend_public_url: str = "http://localhost:8000"
    # Swagger UI / OpenAPI schema; off by default so a deployed backend doesn't publish its API.
    enable_docs: bool = False

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, v: str) -> str:
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v[len(prefix):]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_receptionist_prompt(language: str = "el") -> str:
    name = "receptionist_prompt.md" if language == "el" else "receptionist_prompt.en.md"
    return (CONFIG_DIR / name).read_text(encoding="utf-8")


def load_master_prompt(language: str = "el") -> str:
    """Greek calls use the Greek master prompt; every other language the English one."""
    path = MASTER_PROMPT_PATH if language == "el" else CONFIG_DIR / "master_prompt.en.md"
    return path.read_text(encoding="utf-8")
