from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
MASTER_PROMPT_PATH = CONFIG_DIR / "master_prompt.md"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://user:password@localhost:5432/voiceagent"

    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    gemini_api_key: str = ""

    sip_trunk_id: str = ""
    sip_outbound_number: str = ""

    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = "voiceagent-recordings"

    max_call_duration_seconds: int = 300

    internal_api_token: str = ""
    app_api_token: str = ""
    backend_public_url: str = "http://localhost:8000"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_master_prompt() -> str:
    return MASTER_PROMPT_PATH.read_text(encoding="utf-8")
