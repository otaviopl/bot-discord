import logging

from bot.client import VoiceWatcherClient
from bot.config import Settings
from bot.logger import configure_logging
from bot.voice_listener import VoiceListener
from bot.webhook import WebhookDispatcher


def main() -> None:
    configure_logging()
    logger = logging.getLogger(__name__)

    try:
        settings = Settings.from_env()
    except ValueError as exc:
        logger.error(
            "Invalid configuration",
            extra={"context": {"error": str(exc)}},
        )
        raise SystemExit(1) from exc

    webhook_dispatcher = WebhookDispatcher(
        webhook_url=settings.webhook_url,
        webhook_secret=settings.webhook_secret,
    )

    voice_listener = VoiceListener(
        voice_channel_id=settings.voice_channel_id,
        webhook=webhook_dispatcher,
    )

    client = VoiceWatcherClient(voice_listener=voice_listener)
    client.run(settings.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()

