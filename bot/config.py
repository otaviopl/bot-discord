import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    discord_bot_token: str
    voice_channel_ids: tuple[int, ...]
    webhook_url: str
    julgar_channel_id: int
    webhook_secret: Optional[str] = None
    target_user_id: Optional[int] = None
    notion_token: Optional[str] = None
    notion_database_id: Optional[str] = None
    notion_shift_database_id: Optional[str] = None
    # Google Calendar (opcional)
    google_client_id: Optional[str] = None
    google_client_secret: Optional[str] = None
    calendar_channel_id: Optional[int] = None
    calendar_redirect_uri: str = "http://localhost:8080/oauth2callback"
    calendar_oauth_port: int = 8080
    calendar_timezone: str = "America/Sao_Paulo"
    # Spotify (opcional)
    spotify_client_id: Optional[str] = None
    spotify_client_secret: Optional[str] = None
    spotify_redirect_uri: str = "https://example.com/spotify/callback"
    spotify_oauth_host: str = "0.0.0.0"
    spotify_oauth_port: int = 8888
    spotify_guild_id: Optional[int] = None
    spotify_channel_id: Optional[int] = None
    spotify_user_ids: tuple[int, ...] = ()
    spotify_db_path: str = "/data/spotify.db"
    spotify_encryption_key: Optional[str] = None

    @property
    def spotify_enabled(self) -> bool:
        return bool(
            self.spotify_client_id
            and self.spotify_client_secret
            and self.spotify_guild_id
            and self.spotify_channel_id
            and self.spotify_user_ids
            and self.spotify_encryption_key
        )

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()

        token = _required_env("DISCORD_BOT_TOKEN")
        voice_channel_ids = _required_int_list_env("VOICE_CHANNEL_IDS", fallback_key="VOICE_CHANNEL_ID")
        webhook_url = _required_env("WEBHOOK_URL")
        julgar_channel_id = _required_int_env("JULGAR_CHANNEL_ID")
        webhook_secret = os.getenv("WEBHOOK_SECRET")
        target_user_id_raw = os.getenv("TARGET_USER_ID")
        target_user_id = int(target_user_id_raw) if target_user_id_raw else None
        notion_token = os.getenv("NOTION_TOKEN")
        notion_database_id = os.getenv("NOTION_DATABASE_ID")
        notion_shift_database_id = os.getenv("NOTION_SHIFT_DATABASE_ID")

        google_client_id = os.getenv("GOOGLE_CLIENT_ID") or None
        google_client_secret = os.getenv("GOOGLE_CLIENT_SECRET") or None
        calendar_channel_id_raw = os.getenv("CALENDAR_CHANNEL_ID")
        calendar_channel_id = int(calendar_channel_id_raw) if calendar_channel_id_raw else None
        calendar_redirect_uri = os.getenv("CALENDAR_REDIRECT_URI", "http://localhost:8080/oauth2callback")
        calendar_oauth_port_raw = os.getenv("CALENDAR_OAUTH_PORT", "8080")
        try:
            calendar_oauth_port = int(calendar_oauth_port_raw)
        except ValueError:
            calendar_oauth_port = 8080
        calendar_timezone = os.getenv("CALENDAR_TIMEZONE", "America/Sao_Paulo")

        spotify_client_id = os.getenv("SPOTIFY_CLIENT_ID") or None
        spotify_client_secret = os.getenv("SPOTIFY_CLIENT_SECRET") or None
        spotify_redirect_uri = os.getenv(
            "SPOTIFY_REDIRECT_URI", "https://example.com/spotify/callback"
        )
        spotify_oauth_host = os.getenv("SPOTIFY_OAUTH_HOST", "0.0.0.0")
        spotify_oauth_port = _optional_int_env("SPOTIFY_OAUTH_PORT") or 8888
        spotify_guild_id = _optional_int_env("SPOTIFY_GUILD_ID")
        spotify_channel_id = _optional_int_env("SPOTIFY_CHANNEL_ID")
        spotify_user_ids = _optional_int_list_env("SPOTIFY_USER_IDS")
        spotify_db_path = os.getenv("SPOTIFY_DB_PATH", "/data/spotify.db")
        spotify_encryption_key = os.getenv("SPOTIFY_ENCRYPTION_KEY") or None

        return cls(
            discord_bot_token=token,
            voice_channel_ids=voice_channel_ids,
            webhook_url=webhook_url,
            julgar_channel_id=julgar_channel_id,
            webhook_secret=webhook_secret,
            target_user_id=target_user_id,
            notion_token=notion_token,
            notion_database_id=notion_database_id,
            notion_shift_database_id=notion_shift_database_id,
            google_client_id=google_client_id,
            google_client_secret=google_client_secret,
            calendar_channel_id=calendar_channel_id,
            calendar_redirect_uri=calendar_redirect_uri,
            calendar_oauth_port=calendar_oauth_port,
            calendar_timezone=calendar_timezone,
            spotify_client_id=spotify_client_id,
            spotify_client_secret=spotify_client_secret,
            spotify_redirect_uri=spotify_redirect_uri,
            spotify_oauth_host=spotify_oauth_host,
            spotify_oauth_port=spotify_oauth_port,
            spotify_guild_id=spotify_guild_id,
            spotify_channel_id=spotify_channel_id,
            spotify_user_ids=spotify_user_ids,
            spotify_db_path=spotify_db_path,
            spotify_encryption_key=spotify_encryption_key,
        )


def _required_env(key: str) -> str:
    value = os.getenv(key)
    if value is None or value.strip() == "":
        raise ValueError(f"Missing required environment variable: {key}")
    return value


def _required_int_env(key: str) -> int:
    raw_value = _required_env(key)
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {key} must be a valid integer") from exc


def _required_int_list_env(key: str, fallback_key: str | None = None) -> tuple[int, ...]:
    raw = os.getenv(key)
    if not raw and fallback_key:
        raw = os.getenv(fallback_key)
    if not raw or not raw.strip():
        raise ValueError(f"Missing required environment variable: {key}")
    try:
        return tuple(int(v.strip()) for v in raw.split(",") if v.strip())
    except ValueError as exc:
        raise ValueError(f"Environment variable {key} must be comma-separated integers") from exc


def _optional_int_env(key: str) -> Optional[int]:
    raw = os.getenv(key)
    if not raw or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Environment variable {key} must be a valid integer") from exc


def _optional_int_list_env(key: str) -> tuple[int, ...]:
    raw = os.getenv(key)
    if not raw or not raw.strip():
        return ()
    try:
        return tuple(int(v.strip()) for v in raw.split(",") if v.strip())
    except ValueError as exc:
        raise ValueError(f"Environment variable {key} must be comma-separated integers") from exc
