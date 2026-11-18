"""Settings read from environment variables."""
import os
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    db_path: str
    media_dir: str
    gemini_api_key: str
    gemini_model: str
    gemini_fallback_models: tuple
    wa_token: str
    wa_phone_number_id: str
    wa_verify_token: str
    wa_app_secret: str
    wa_api_version: str
    public_base_url: str
    admin_user: str
    admin_password: str
    timezone: str = "Asia/Kolkata"

    def today(self) -> date:
        """Workers are in India; the server (WSL, Cloud Run) usually runs in UTC."""
        return datetime.now(ZoneInfo(self.timezone)).date()


def load_dotenv(path: str = ".env") -> None:
    """Read KEY=VALUE lines from .env. Real environment variables win."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_settings() -> Settings:
    load_dotenv()
    env = os.environ.get
    return Settings(
        db_path=env("KP_DB_PATH", "data/kaampramaan.db"),
        media_dir=env("KP_MEDIA_DIR", "data/media"),
        gemini_api_key=env("GEMINI_API_KEY", ""),
        gemini_model=env("GEMINI_MODEL", "gemini-3.8-flash"),
        gemini_fallback_models=tuple(m.strip() for m in env("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash,gemini-3.1-flash-lite").split(",") if m.strip()),
        wa_token=env("WA_ACCESS_TOKEN", ""),
        wa_phone_number_id=env("WA_PHONE_NUMBER_ID", ""),
        wa_verify_token=env("WA_VERIFY_TOKEN", ""),
        wa_app_secret=env("WA_APP_SECRET", ""),
        wa_api_version=env("WA_API_VERSION", "v21.0"),
        public_base_url=env("KP_PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        admin_user=env("KP_ADMIN_USER", "admin"),
        admin_password=env("KP_ADMIN_PASSWORD", ""),
        timezone=env("KP_TIMEZONE", "Asia/Kolkata"),
    )
