"""Persistencia: dedup de escutas, cifragem de tokens, desconexao e sobrevivencia a restart."""

import sqlite3

import pytest
from cryptography.fernet import Fernet

from bot.spotify_store import Play, SpotifyStore

USER_A = 111111111111111111
USER_B = 222222222222222222


@pytest.fixture
def key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def store(tmp_path, key) -> SpotifyStore:
    return SpotifyStore(str(tmp_path / "spotify.db"), key)


def make_play(user_id: int, track_id: str, played_at_ms: int, name: str = "Faixa") -> Play:
    return Play(
        discord_user_id=user_id,
        track_id=track_id,
        played_at_ms=played_at_ms,
        track_name=name,
        artists="Artista X",
        artist_ids=["a1"],
        album_name="Album",
        album_image=None,
        track_url="https://open.spotify.com/track/" + track_id,
    )


async def connect(store: SpotifyStore, user_id: int, expires_at: int = 9_999_999_999) -> None:
    await store.save_account(
        discord_user_id=user_id,
        spotify_user_id=f"spotify-{user_id}",
        display_name="Alguem",
        access_token="access-secreto",
        refresh_token="refresh-secreto",
        expires_at=expires_at,
        scope="user-read-currently-playing",
        connected_at=1_700_000_000,
    )


class TestDeduplicacao:
    async def test_consulta_repetida_nao_duplica_escuta(self, store):
        await connect(store, USER_A)
        plays = [make_play(USER_A, "track1", 1_700_000_000_000)]

        primeira = await store.record_plays(plays)
        segunda = await store.record_plays(plays)  # mesma pagina lida de novo

        assert primeira == 1
        assert segunda == 0
        assert await store.count_plays(USER_A, 0, 2_000_000_000_000) == 1

    async def test_ouvir_de_novo_conta_como_outra_reproducao(self, store):
        await connect(store, USER_A)
        await store.record_plays([make_play(USER_A, "track1", 1_700_000_000_000)])
        novas = await store.record_plays([make_play(USER_A, "track1", 1_700_000_180_000)])

        assert novas == 1
        assert await store.count_plays(USER_A, 0, 2_000_000_000_000) == 2

    async def test_mesma_faixa_de_pessoas_diferentes_conta_para_cada_uma(self, store):
        await connect(store, USER_A)
        await connect(store, USER_B)
        momento = 1_700_000_000_000

        await store.record_plays([make_play(USER_A, "track1", momento)])
        await store.record_plays([make_play(USER_B, "track1", momento)])

        assert await store.count_plays(USER_A, 0, 2_000_000_000_000) == 1
        assert await store.count_plays(USER_B, 0, 2_000_000_000_000) == 1


class TestTokens:
    async def test_tokens_ficam_cifrados_no_banco(self, tmp_path, key):
        db_path = tmp_path / "spotify.db"
        store = SpotifyStore(str(db_path), key)
        await connect(store, USER_A)

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT access_token, refresh_token FROM spotify_users"
            ).fetchone()

        assert b"access-secreto" not in row[0]
        assert b"refresh-secreto" not in row[1]

        account = await store.get_account(USER_A)
        assert account.access_token == "access-secreto"
        assert account.refresh_token == "refresh-secreto"

    async def test_chave_trocada_invalida_conta_sem_derrubar_o_bot(self, tmp_path, key):
        db_path = tmp_path / "spotify.db"
        await connect(SpotifyStore(str(db_path), key), USER_A)

        outra_chave = SpotifyStore(str(db_path), Fernet.generate_key().decode())
        assert await outra_chave.get_account(USER_A) is None
        assert await outra_chave.list_accounts() == []

    async def test_update_tokens_preserva_refresh_quando_spotify_nao_manda_um_novo(self, store):
        await connect(store, USER_A)
        await store.update_tokens(USER_A, access_token="novo-access", expires_at=1_800_000_000)

        account = await store.get_account(USER_A)
        assert account.access_token == "novo-access"
        assert account.refresh_token == "refresh-secreto"
        assert account.expires_at == 1_800_000_000


class TestDesconexao:
    async def test_desconectar_apaga_conta_e_escutas(self, store):
        await connect(store, USER_A)
        await connect(store, USER_B)
        await store.record_plays(
            [make_play(USER_A, "t1", 1), make_play(USER_A, "t2", 2), make_play(USER_B, "t1", 3)]
        )

        removidas = await store.delete_account(USER_A)

        assert removidas == 2
        assert await store.get_account(USER_A) is None
        assert await store.count_plays(USER_A, 0, 2_000_000_000_000) == 0
        # a outra pessoa nao e afetada
        assert await store.count_plays(USER_B, 0, 2_000_000_000_000) == 1


class TestReinicio:
    async def test_historico_e_painel_sobrevivem_ao_restart(self, tmp_path, key):
        db_path = str(tmp_path / "spotify.db")

        antes = SpotifyStore(db_path, key)
        await connect(antes, USER_A)
        await antes.record_plays([make_play(USER_A, "t1", 1_700_000_000_000)])
        await antes.set_value("panel_message_id", "987654321")
        await antes.set_sync_cursor(USER_A, 1_700_000_000_000, 1_700_000_100)

        depois = SpotifyStore(db_path, key)  # novo processo, mesmo volume

        assert await depois.count_plays(USER_A, 0, 2_000_000_000_000) == 1
        assert await depois.get_value("panel_message_id") == "987654321"
        conta = await depois.get_account(USER_A)
        assert conta.last_played_at_ms == 1_700_000_000_000

    async def test_cursor_nunca_anda_para_tras(self, store):
        await connect(store, USER_A)
        await store.set_sync_cursor(USER_A, 5_000, 1)
        await store.set_sync_cursor(USER_A, 1_000, 2)

        conta = await store.get_account(USER_A)
        assert conta.last_played_at_ms == 5_000


class TestAgregacoes:
    async def test_top_e_musicas_em_comum(self, store):
        await connect(store, USER_A)
        await connect(store, USER_B)
        base = 1_700_000_000_000

        await store.record_plays(
            [
                make_play(USER_A, "shared", base, "Musica Comum"),
                make_play(USER_A, "shared", base + 1000, "Musica Comum"),
                make_play(USER_A, "so-dela", base + 2000, "So Dela"),
                make_play(USER_B, "shared", base + 3000, "Musica Comum"),
                make_play(USER_B, "so-dele", base + 4000, "So Dele"),
            ]
        )

        topo = await store.top_tracks(USER_A, 0, 2_000_000_000_000, limit=5)
        assert topo[0]["track_id"] == "shared"
        assert topo[0]["plays"] == 2

        comuns = await store.shared_tracks(USER_A, USER_B, 0, 2_000_000_000_000)
        assert [t["track_id"] for t in comuns] == ["shared"]

    async def test_janela_de_tempo_filtra_escutas(self, store):
        await connect(store, USER_A)
        await store.record_plays(
            [make_play(USER_A, "dentro", 1_500), make_play(USER_A, "fora", 5_000)]
        )

        assert await store.count_plays(USER_A, 1_000, 2_000) == 1

    async def test_semana_sem_atividade_devolve_zero(self, store):
        await connect(store, USER_A)
        assert await store.count_plays(USER_A, 0, 1_000) == 0
        assert await store.top_tracks(USER_A, 0, 1_000) == []
        assert await store.top_artists(USER_A, 0, 1_000) == []


class TestOAuthState:
    async def test_state_valido_e_consumido_uma_unica_vez(self, store):
        await store.save_oauth_state("abc", USER_A, 1_000)

        assert await store.consume_oauth_state("abc", 900, 1_100) == USER_A
        assert await store.consume_oauth_state("abc", 900, 1_100) is None

    async def test_state_expirado_e_rejeitado(self, store):
        await store.save_oauth_state("velho", USER_A, 1_000)
        assert await store.consume_oauth_state("velho", 900, 5_000) is None

    async def test_state_desconhecido_e_rejeitado(self, store):
        assert await store.consume_oauth_state("nunca-existiu", 900, 1_000) is None
