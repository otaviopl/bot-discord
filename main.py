import logging

from bot.calendar_auth import CalendarAuth
from bot.calendar_client import CalendarClient
from bot.calendar_listener import CalendarListener
from bot.client import VoiceWatcherClient
from bot.config import Settings
from bot.julgar_listener import JulgarListener
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
    julgar_listener = JulgarListener(
        text_channel_id=settings.julgar_channel_id,
        adm_voice_channel_id=settings.voice_channel_id,
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
        calendar_listener=calendar_listener,
    )
    client.run(settings.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()

