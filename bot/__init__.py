"""Core package for the Discord voice watcher bot."""

from .calendar_auth import CalendarAuth
from .calendar_client import CalendarClient
from .calendar_listener import CalendarListener
from .client import VoiceWatcherClient
from .config import Settings
from .julgar_listener import JulgarListener
from .notion_client import NotionClient
from .spotify_auth import SpotifyAuth
from .spotify_client import SpotifyClient
from .spotify_listener import SpotifyListener
from .spotify_store import SpotifyStore
from .timer_manager import TimerManager
from .voice_listener import VoiceListener
from .webhook import WebhookDispatcher

__all__ = [
    "CalendarAuth",
    "CalendarClient",
    "CalendarListener",
    "JulgarListener",
    "NotionClient",
    "Settings",
    "SpotifyAuth",
    "SpotifyClient",
    "SpotifyListener",
    "SpotifyStore",
    "TimerManager",
    "VoiceWatcherClient",
    "VoiceListener",
    "WebhookDispatcher",
    "shift_manager",
]

