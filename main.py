import logging

from bot.client import VoiceWatcherClient
from bot.config import Settings
from bot.julgar_listener import JulgarListener
from bot.logger import configure_logging
from bot.notion_client import NotionClient
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
    julgar_listener = JulgarListener(
        text_channel_id=settings.julgar_channel_id,
        adm_voice_channel_id=settings.voice_channel_id,
    )

    notion_client = None
    if settings.notion_token and settings.notion_database_id:
        notion_client = NotionClient(
            token=settings.notion_token,
            database_id=settings.notion_database_id,
        )
        logger.info("Notion integration enabled")
    else:
        logger.warning(
            "Notion integration disabled (NOTION_TOKEN or NOTION_DATABASE_ID not set)"
        )

    client = VoiceWatcherClient(
        voice_listener=voice_listener,
        julgar_listener=julgar_listener,
        notion_client=notion_client,
    )
    client.run(settings.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()

