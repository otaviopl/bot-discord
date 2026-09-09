import logging

from bot.calendar_auth import CalendarAuth
from bot.calendar_client import CalendarClient
from bot.calendar_listener import CalendarListener
from bot.client import VoiceWatcherClient
from bot.config import Settings
from bot.julgar_listener import JulgarListener
from bot.logger import configure_logging
from bot.notion_client import NotionClient
from bot.spotify_auth import SpotifyAuth
from bot.spotify_client import SpotifyClient
from bot.spotify_listener import SpotifyListener
from bot.spotify_store import SpotifyStore
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

    spotify_listener = None
    if settings.spotify_enabled:
        try:
            import httpx

            spotify_store = SpotifyStore(
                db_path=settings.spotify_db_path,
                encryption_key=settings.spotify_encryption_key,
            )
            spotify_http = httpx.AsyncClient()
            spotify_auth = SpotifyAuth(
                client_id=settings.spotify_client_id,
                client_secret=settings.spotify_client_secret,
                redirect_uri=settings.spotify_redirect_uri,
                store=spotify_store,
                http=spotify_http,
            )
            spotify_listener = SpotifyListener(
                store=spotify_store,
                auth=spotify_auth,
                client=SpotifyClient(spotify_http),
                guild_id=settings.spotify_guild_id,
                channel_id=settings.spotify_channel_id,
                allowed_user_ids=settings.spotify_user_ids,
                oauth_host=settings.spotify_oauth_host,
                oauth_port=settings.spotify_oauth_port,
                tz_name=settings.calendar_timezone,
                http=spotify_http,
            )
            logger.info(
                "Spotify integration enabled",
                extra={
                    "context": {
                        "channel_id": str(settings.spotify_channel_id),
                        "users": len(settings.spotify_user_ids),
                        "db_path": settings.spotify_db_path,
                        "redirect_uri": settings.spotify_redirect_uri,
                    }
                },
            )
        except Exception as exc:
            logger.error(
                "Failed to initialize Spotify integration",
                extra={"context": {"error": str(exc)}},
            )
            spotify_listener = None
    else:
        logger.info(
            "Spotify integration disabled (defina SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, "
            "SPOTIFY_GUILD_ID, SPOTIFY_CHANNEL_ID, SPOTIFY_USER_IDS e SPOTIFY_ENCRYPTION_KEY)"
        )

    client = VoiceWatcherClient(
        voice_listener=voice_listener,
        julgar_listener=julgar_listener,
        notion_client=notion_client,
        timer_manager=timer_manager,
        calendar_listener=calendar_listener,
        spotify_listener=spotify_listener,
        target_user_id=settings.target_user_id,
        tz_name=settings.calendar_timezone,
    )
    client.run(settings.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
