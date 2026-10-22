import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "ja", "on")


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing setting in .env: {name}")
    return value


@dataclass(frozen=True)
class Config:
    discord_token: str
    guild_id: int
    navidrome_url: str
    navidrome_user: str
    navidrome_password: str
    navidrome_max_bitrate: int
    web_host: str
    web_port: int
    trusted_proxies: str
    panel_url: str
    secret_key: str
    cookie_secure: bool
    admin_username: str
    admin_password: str
    db_path: Path
    default_volume: int
    idle_disconnect_minutes: int
    timezone: str


def load_config() -> Config:
    db_path = Path(os.getenv("DB_PATH", "data/panel.db"))
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    return Config(
        discord_token=_required("DISCORD_TOKEN"),
        guild_id=int(_required("DISCORD_GUILD_ID")),
        navidrome_url=_required("NAVIDROME_URL").rstrip("/"),
        navidrome_user=_required("NAVIDROME_USER"),
        navidrome_password=_required("NAVIDROME_PASSWORD"),
        navidrome_max_bitrate=int(os.getenv("NAVIDROME_MAX_BITRATE", "0") or 0),
        web_host=os.getenv("WEB_HOST", "127.0.0.1"),
        web_port=int(os.getenv("WEB_PORT", "8080")),
        trusted_proxies=os.getenv("TRUSTED_PROXIES", "127.0.0.1"),
        panel_url=os.getenv("PANEL_URL", "").rstrip("/"),
        secret_key=_required("SECRET_KEY"),
        cookie_secure=_bool(os.getenv("COOKIE_SECURE", "true")),
        admin_username=os.getenv("ADMIN_USERNAME", "admin").strip() or "admin",
        admin_password=os.getenv("ADMIN_PASSWORD", ""),
        db_path=db_path,
        default_volume=max(0, min(100, int(os.getenv("DEFAULT_VOLUME", "50")))),
        idle_disconnect_minutes=int(os.getenv("IDLE_DISCONNECT_MINUTES", "5")),
        timezone=os.getenv("TIMEZONE", "Europe/Berlin").strip() or "Europe/Berlin",
    )
