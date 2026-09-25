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

    max_call_duration_seconds: int = 300
    max_concurrent_calls: int = 1

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


def load_master_prompt(language: str = "el") -> str:
    """Greek calls use the Greek master prompt; every other language the English one."""
    path = MASTER_PROMPT_PATH if language == "el" else CONFIG_DIR / "master_prompt.en.md"
    return path.read_text(encoding="utf-8")
