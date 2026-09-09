"""O modulo do Spotify e opcional: sem ele configurado, o bot continua como antes."""

import pytest
from cryptography.fernet import Fernet

from bot.client import VoiceWatcherClient
from bot.config import Settings
from bot.julgar_listener import JulgarListener
from bot.voice_listener import VoiceListener
from bot.webhook import WebhookDispatcher


BASE_ENV = {
    "DISCORD_BOT_TOKEN": "fake",
    "VOICE_CHANNEL_ID": "1",
    "WEBHOOK_URL": "https://x.test/h",
    "JULGAR_CHANNEL_ID": "2",
}

SPOTIFY_KEYS = [
    "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REDIRECT_URI",
    "SPOTIFY_OAUTH_HOST", "SPOTIFY_OAUTH_PORT", "SPOTIFY_GUILD_ID",
    "SPOTIFY_CHANNEL_ID", "SPOTIFY_USER_IDS", "SPOTIFY_DB_PATH",
    "SPOTIFY_ENCRYPTION_KEY",
]


@pytest.fixture
def ambiente_limpo(monkeypatch):
    for key in SPOTIFY_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)


def build_client(spotify_listener=None) -> VoiceWatcherClient:
    return VoiceWatcherClient(
        voice_listener=VoiceListener((1,), WebhookDispatcher("https://x.test/h", None)),
        julgar_listener=JulgarListener(2, 1),
        spotify_listener=spotify_listener,
    )


def test_sem_variaveis_do_spotify_o_modulo_fica_desligado(ambiente_limpo):
    settings = Settings.from_env()
    assert settings.spotify_enabled is False


def test_configuracao_pela_metade_nao_liga_o_modulo(ambiente_limpo, monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "secret")
    # faltam guild, canal, usuarios e chave
    assert Settings.from_env().spotify_enabled is False


def test_configuracao_completa_liga_o_modulo(ambiente_limpo, monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SPOTIFY_GUILD_ID", "900000000000000001")
    monkeypatch.setenv("SPOTIFY_CHANNEL_ID", "900000000000000002")
    monkeypatch.setenv("SPOTIFY_USER_IDS", "111111111111111111,222222222222222222")
    monkeypatch.setenv("SPOTIFY_ENCRYPTION_KEY", Fernet.generate_key().decode())

    settings = Settings.from_env()
    assert settings.spotify_enabled is True
    assert settings.spotify_user_ids == (111111111111111111, 222222222222222222)
    assert settings.spotify_db_path == "/data/spotify.db"


def test_ids_invalidos_falham_cedo_com_mensagem_clara(ambiente_limpo, monkeypatch):
    monkeypatch.setenv("SPOTIFY_USER_IDS", "abc,def")
    with pytest.raises(ValueError, match="SPOTIFY_USER_IDS"):
        Settings.from_env()


def test_client_sem_spotify_nao_quebra():
    client = build_client(spotify_listener=None)
    assert client._spotify_listener is None


async def test_loops_do_spotify_nao_fazem_nada_sem_listener():
    client = build_client(spotify_listener=None)
    # nao deve levantar nada
    await client._on_spotify_panel_tick()
    await client._on_spotify_sync_tick()
    await client._on_spotify_weekly_tick()


def test_help_lista_os_comandos_antigos_e_os_novos():
    from bot.client import _build_help_embed

    embed = _build_help_embed()
    nomes = [f.name for f in embed.fields]
    assert any("Tarefas" in n for n in nomes)
    assert any("Turnos" in n for n in nomes)
    assert any("Spotify" in n for n in nomes)

    spotify_field = next(f for f in embed.fields if "Spotify" in f.name)
    for comando in ("!conectar", "!agora", "!top", "!comparar", "!desconectar"):
        assert comando in spotify_field.value
