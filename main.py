import logging

from bot.calendar_auth import CalendarAuth
from bot.calendar_client import CalendarClient
from bot.calendar_listener import CalendarListener
from bot.client import VoiceWatcherClient
from bot.config import Settings
from bot.julgar_listener import JulgarListener
from bot.logger import configure_logging
from bot.notion_client import NotionClient
from bot.timer_manager import TimerManager
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
        voice_channel_ids=settings.voice_channel_ids,
        webhook=webhook_dispatcher,
    )
    julgar_listener = JulgarListener(
        text_channel_id=settings.julgar_channel_id,
        adm_voice_channel_id=settings.voice_channel_ids[0],
    )

    notion_client = None
    timer_manager = TimerManager()
    if settings.notion_token and settings.notion_database_id:
        notion_client = NotionClient(
            token=settings.notion_token,
            database_id=settings.notion_database_id,
            shifts_database_id=settings.notion_shift_database_id,
        )
        logger.info(
            "Notion integration enabled",
            extra={"context": {"shifts_db": bool(settings.notion_shift_database_id)}},
        )
    else:
        logger.warning(
            "Notion integration disabled (NOTION_TOKEN or NOTION_DATABASE_ID not set)"
        )

    calendar_listener = None
    if settings.google_client_id and settings.google_client_secret:
        calendar_auth = CalendarAuth(
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            redirect_uri=settings.calendar_redirect_uri,
        )
        calendar_client = CalendarClient(
            auth=calendar_auth,
            timezone=settings.calendar_timezone,
        )
        calendar_channel_id = settings.calendar_channel_id or settings.julgar_channel_id
        calendar_listener = CalendarListener(
            auth=calendar_auth,
            calendar_client=calendar_client,
            channel_id=calendar_channel_id,
            oauth_port=settings.calendar_oauth_port,
        )
        logger.info(
            "Google Calendar integration enabled",
            extra={"context": {"channel_id": str(calendar_channel_id)}},
        )
    else:
        logger.info("Google Calendar integration disabled (GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET not set)")

    client = VoiceWatcherClient(
        voice_listener=voice_listener,
        julgar_listener=julgar_listener,
        notion_client=notion_client,
        timer_manager=timer_manager,
        calendar_listener=calendar_listener,
        target_user_id=settings.target_user_id,
        tz_name=settings.calendar_timezone,
    )
    client.run(settings.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
