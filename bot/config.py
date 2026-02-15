import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    discord_bot_token: str
    voice_channel_id: int
    webhook_url: str
    webhook_secret: Optional[str] = None

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()

        token = _required_env("DISCORD_BOT_TOKEN")
        voice_channel_id = _required_int_env("VOICE_CHANNEL_ID")
        webhook_url = _required_env("WEBHOOK_URL")
        webhook_secret = os.getenv("WEBHOOK_SECRET")

        return cls(
            discord_bot_token=token,
            voice_channel_id=voice_channel_id,
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
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

