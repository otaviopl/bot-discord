"""Persistencia SQLite do modulo Spotify (tokens cifrados + escutas registradas)."""

import asyncio
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from cryptography.fernet import Fernet, InvalidToken

MAX_TRACK_MS = 15 * 60 * 1000  # teto para escutas antigas, sem duracao gravada

SCHEMA = """
CREATE TABLE IF NOT EXISTS spotify_users (
    discord_user_id   TEXT PRIMARY KEY,
    spotify_user_id   TEXT,
    display_name      TEXT,
    access_token      BLOB NOT NULL,
    refresh_token     BLOB NOT NULL,
    expires_at        INTEGER NOT NULL,
    scope             TEXT,
    connected_at      INTEGER NOT NULL,
    last_synced_at    INTEGER,
    last_played_at_ms INTEGER,
    needs_reauth      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS spotify_plays (
    discord_user_id TEXT NOT NULL,
    track_id        TEXT NOT NULL,
    played_at_ms    INTEGER NOT NULL,
    track_name      TEXT NOT NULL,
    artists         TEXT NOT NULL,
    artist_ids      TEXT,
    album_name      TEXT,
    album_image     TEXT,
    track_url       TEXT,
    duration_ms     INTEGER,
    PRIMARY KEY (discord_user_id, track_id, played_at_ms)
);

CREATE INDEX IF NOT EXISTS idx_plays_user_time
    ON spotify_plays (discord_user_id, played_at_ms);

-- Camada 2: tempo medido amostrando progress_ms do player.
CREATE TABLE IF NOT EXISTS spotify_measured (
    discord_user_id TEXT NOT NULL,
    track_id        TEXT NOT NULL,
    started_ms      INTEGER NOT NULL,
    ended_ms        INTEGER NOT NULL,
    ms_played       INTEGER NOT NULL,
    PRIMARY KEY (discord_user_id, track_id, started_ms)
);

CREATE INDEX IF NOT EXISTS idx_measured_user_time
    ON spotify_measured (discord_user_id, started_ms);

-- Camada 3: ms_played real, importado do Extended Streaming History.
-- `ended_ms` vem do campo `ts` do arquivo, que marca quando a faixa PAROU de tocar;
-- o inicio e calculado para tras a partir de ms_played.
CREATE TABLE IF NOT EXISTS spotify_imported (
    discord_user_id TEXT NOT NULL,
    started_ms      INTEGER NOT NULL,
    ended_ms        INTEGER NOT NULL,
    track_key       TEXT NOT NULL,
    track_id        TEXT,
    track_name      TEXT,
    artists         TEXT,
    ms_played       INTEGER NOT NULL,
    PRIMARY KEY (discord_user_id, ended_ms, track_key)
);

CREATE INDEX IF NOT EXISTS idx_imported_user_time
    ON spotify_imported (discord_user_id, started_ms);

-- Cache de generos por artista. O campo `genres` do Spotify esta deprecated e volta
-- vazio para muitos artistas; guardamos o resultado (inclusive o vazio) para nao
-- repetir a chamada a cada consulta.
CREATE TABLE IF NOT EXISTS spotify_artist_genres (
    artist_id  TEXT PRIMARY KEY,
    name       TEXT,
    genres     TEXT NOT NULL,
    fetched_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS spotify_kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spotify_oauth_states (
    state           TEXT PRIMARY KEY,
    discord_user_id TEXT NOT NULL,
    created_at      INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class SpotifyAccount:
    discord_user_id: int
    spotify_user_id: Optional[str]
    display_name: Optional[str]
    access_token: str
    refresh_token: str
    expires_at: int
    scope: Optional[str]
    connected_at: int
    last_synced_at: Optional[int]
    last_played_at_ms: Optional[int]
    needs_reauth: bool


@dataclass(frozen=True)
class Play:
    discord_user_id: int
    track_id: str
    played_at_ms: int
    track_name: str
    artists: str
    artist_ids: List[str]
    album_name: Optional[str]
    album_image: Optional[str]
    track_url: Optional[str]
    duration_ms: Optional[int] = None


class SpotifyStore:
    """SQLite com tokens cifrados em Fernet. Toda I/O roda em thread separada."""

    def __init__(self, db_path: str, encryption_key: str) -> None:
        self._db_path = db_path
        self._fernet = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
        self._logger = logging.getLogger(__name__)
        self._lock = asyncio.Lock()

        directory = os.path.dirname(os.path.abspath(db_path))
        if directory:
            os.makedirs(directory, exist_ok=True)

        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Migracoes aditivas: nunca removem nem reescrevem dados ja gravados."""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(spotify_plays)")}
        if "duration_ms" not in columns:
            conn.execute("ALTER TABLE spotify_plays ADD COLUMN duration_ms INTEGER")
            self._logger.info("Migracao aplicada: spotify_plays.duration_ms")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    async def _run(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    # ------------------------------------------------------------------ #
    # Contas
    # ------------------------------------------------------------------ #

    async def save_account(
        self,
        discord_user_id: int,
        spotify_user_id: Optional[str],
        display_name: Optional[str],
        access_token: str,
        refresh_token: str,
        expires_at: int,
        scope: Optional[str],
        connected_at: int,
    ) -> None:
        access_enc = self._fernet.encrypt(access_token.encode())
        refresh_enc = self._fernet.encrypt(refresh_token.encode())

        def _op() -> None:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO spotify_users (
                        discord_user_id, spotify_user_id, display_name,
                        access_token, refresh_token, expires_at, scope,
                        connected_at, needs_reauth
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(discord_user_id) DO UPDATE SET
                        spotify_user_id = excluded.spotify_user_id,
                        display_name    = excluded.display_name,
                        access_token    = excluded.access_token,
                        refresh_token   = excluded.refresh_token,
                        expires_at      = excluded.expires_at,
                        scope           = excluded.scope,
                        needs_reauth    = 0
                    """,
                    (
                        str(discord_user_id),
                        spotify_user_id,
                        display_name,
                        access_enc,
                        refresh_enc,
                        expires_at,
                        scope,
                        connected_at,
                    ),
                )

        await self._run(_op)

    async def update_tokens(
        self,
        discord_user_id: int,
        access_token: str,
        expires_at: int,
        refresh_token: Optional[str] = None,
    ) -> None:
        access_enc = self._fernet.encrypt(access_token.encode())
        refresh_enc = self._fernet.encrypt(refresh_token.encode()) if refresh_token else None

        def _op() -> None:
            with self._connect() as conn:
                if refresh_enc is not None:
                    conn.execute(
                        "UPDATE spotify_users SET access_token = ?, refresh_token = ?, "
                        "expires_at = ?, needs_reauth = 0 WHERE discord_user_id = ?",
                        (access_enc, refresh_enc, expires_at, str(discord_user_id)),
                    )
                else:
                    conn.execute(
                        "UPDATE spotify_users SET access_token = ?, expires_at = ?, "
                        "needs_reauth = 0 WHERE discord_user_id = ?",
                        (access_enc, expires_at, str(discord_user_id)),
                    )

        await self._run(_op)

    async def mark_needs_reauth(self, discord_user_id: int) -> None:
        def _op() -> None:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE spotify_users SET needs_reauth = 1 WHERE discord_user_id = ?",
                    (str(discord_user_id),),
                )

        await self._run(_op)

    async def get_account(self, discord_user_id: int) -> Optional[SpotifyAccount]:
        def _op() -> Optional[sqlite3.Row]:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM spotify_users WHERE discord_user_id = ?",
                    (str(discord_user_id),),
                )
                return cur.fetchone()

        row = await self._run(_op)
        return self._row_to_account(row) if row else None

    async def list_accounts(self) -> List[SpotifyAccount]:
        def _op() -> List[sqlite3.Row]:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT * FROM spotify_users ORDER BY connected_at ASC"
                ).fetchall()

        rows = await self._run(_op)
        accounts = []
        for row in rows:
            account = self._row_to_account(row)
            if account is not None:
                accounts.append(account)
        return accounts

    def _row_to_account(self, row: sqlite3.Row) -> Optional[SpotifyAccount]:
        try:
            access = self._fernet.decrypt(row["access_token"]).decode()
            refresh = self._fernet.decrypt(row["refresh_token"]).decode()
        except InvalidToken:
            self._logger.error(
                "Failed to decrypt Spotify tokens (chave de criptografia mudou?)",
                extra={"context": {"discord_user_id": row["discord_user_id"]}},
            )
            return None

        return SpotifyAccount(
            discord_user_id=int(row["discord_user_id"]),
            spotify_user_id=row["spotify_user_id"],
            display_name=row["display_name"],
            access_token=access,
            refresh_token=refresh,
            expires_at=int(row["expires_at"]),
            scope=row["scope"],
            connected_at=int(row["connected_at"]),
            last_synced_at=row["last_synced_at"],
            last_played_at_ms=row["last_played_at_ms"],
            needs_reauth=bool(row["needs_reauth"]),
        )

    async def delete_account(self, discord_user_id: int) -> int:
        """Remove conta e todas as escutas registradas. Retorna quantas escutas foram apagadas."""

        def _op() -> int:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM spotify_plays WHERE discord_user_id = ?",
                    (str(discord_user_id),),
                )
                total = int(cur.fetchone()[0])
                conn.execute("DELETE FROM spotify_plays WHERE discord_user_id = ?", (str(discord_user_id),))
                conn.execute("DELETE FROM spotify_measured WHERE discord_user_id = ?", (str(discord_user_id),))
                conn.execute("DELETE FROM spotify_imported WHERE discord_user_id = ?", (str(discord_user_id),))
                conn.execute("DELETE FROM spotify_users WHERE discord_user_id = ?", (str(discord_user_id),))
                conn.execute(
                    "DELETE FROM spotify_oauth_states WHERE discord_user_id = ?",
                    (str(discord_user_id),),
                )
                return total

        return await self._run(_op)

    # ------------------------------------------------------------------ #
    # Escutas registradas
    # ------------------------------------------------------------------ #

    async def record_plays(self, plays: Sequence[Play]) -> int:
        """Insere escutas ignorando duplicatas (mesmo usuario + faixa + horario)."""
        if not plays:
            return 0

        rows = [
            (
                str(p.discord_user_id),
                p.track_id,
                p.played_at_ms,
                p.track_name,
                p.artists,
                json.dumps(p.artist_ids, ensure_ascii=False),
                p.album_name,
                p.album_image,
                p.track_url,
                p.duration_ms,
            )
            for p in plays
        ]

        def _op() -> int:
            with self._connect() as conn:
                before = conn.total_changes
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO spotify_plays (
                        discord_user_id, track_id, played_at_ms, track_name,
                        artists, artist_ids, album_name, album_image, track_url,
                        duration_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                return conn.total_changes - before

        return await self._run(_op)

    async def set_sync_cursor(self, discord_user_id: int, last_played_at_ms: int, synced_at: int) -> None:
        def _op() -> None:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE spotify_users
                       SET last_played_at_ms = MAX(COALESCE(last_played_at_ms, 0), ?),
                           last_synced_at = ?
                     WHERE discord_user_id = ?
                    """,
                    (last_played_at_ms, synced_at, str(discord_user_id)),
                )

        await self._run(_op)

    async def touch_sync(self, discord_user_id: int, synced_at: int) -> None:
        def _op() -> None:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE spotify_users SET last_synced_at = ? WHERE discord_user_id = ?",
                    (synced_at, str(discord_user_id)),
                )

        await self._run(_op)

    async def count_plays(self, discord_user_id: int, start_ms: int, end_ms: int) -> int:
        def _op() -> int:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM spotify_plays "
                    "WHERE discord_user_id = ? AND played_at_ms >= ? AND played_at_ms < ?",
                    (str(discord_user_id), start_ms, end_ms),
                )
                return int(cur.fetchone()[0])

        return await self._run(_op)

    async def top_tracks(
        self, discord_user_id: int, start_ms: int, end_ms: int, limit: int = 5
    ) -> List[Dict[str, Any]]:
        def _op() -> List[Dict[str, Any]]:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    SELECT track_id, track_name, artists, track_url, COUNT(*) AS plays
                      FROM spotify_plays
                     WHERE discord_user_id = ? AND played_at_ms >= ? AND played_at_ms < ?
                     GROUP BY track_id
                     ORDER BY plays DESC, MAX(played_at_ms) DESC
                     LIMIT ?
                    """,
                    (str(discord_user_id), start_ms, end_ms, limit),
                )
                return [dict(row) for row in cur.fetchall()]

        return await self._run(_op)

    async def top_artists(
        self, discord_user_id: int, start_ms: int, end_ms: int, limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Agrega pelo artista principal da faixa, usando o id do Spotify como chave.

        Cair no nome quebraria com artistas homonimos e com nomes que contem virgula.
        """

        def _op() -> List[sqlite3.Row]:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT artists, artist_ids FROM spotify_plays "
                    "WHERE discord_user_id = ? AND played_at_ms >= ? AND played_at_ms < ?",
                    (str(discord_user_id), start_ms, end_ms),
                ).fetchall()

        rows = await self._run(_op)
        counts: Dict[str, int] = {}
        names: Dict[str, str] = {}

        for row in rows:
            display = (row["artists"] or "").split(", ")[0].strip()
            key = display
            try:
                ids = json.loads(row["artist_ids"] or "[]")
                if ids:
                    key = ids[0]
            except (ValueError, TypeError):
                pass
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
            names.setdefault(key, display or "Artista desconhecido")

        ordered = sorted(counts.items(), key=lambda item: (-item[1], names[item[0]]))[:limit]
        return [{"artist": names[key], "plays": plays} for key, plays in ordered]

    async def shared_tracks(
        self, user_a: int, user_b: int, start_ms: int, end_ms: int, limit: int = 5
    ) -> List[Dict[str, Any]]:
        def _op() -> List[Dict[str, Any]]:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    SELECT a.track_id,
                           MAX(a.track_name) AS track_name,
                           MAX(a.artists)    AS artists,
                           MAX(a.track_url)  AS track_url,
                           COUNT(DISTINCT a.played_at_ms) AS plays_a,
                           (SELECT COUNT(*) FROM spotify_plays b
                             WHERE b.discord_user_id = ?
                               AND b.track_id = a.track_id
                               AND b.played_at_ms >= ? AND b.played_at_ms < ?) AS plays_b
                      FROM spotify_plays a
                     WHERE a.discord_user_id = ?
                       AND a.played_at_ms >= ? AND a.played_at_ms < ?
                     GROUP BY a.track_id
                    HAVING plays_b > 0
                     ORDER BY (plays_a + plays_b) DESC, track_name ASC
                     LIMIT ?
                    """,
                    (
                        str(user_b), start_ms, end_ms,
                        str(user_a), start_ms, end_ms,
                        limit,
                    ),
                )
                return [dict(row) for row in cur.fetchall()]

        return await self._run(_op)

    async def raw_plays(
        self, discord_user_id: int, start_ms: int, end_ms: int
    ) -> List[Dict[str, Any]]:
        """Camada 1: escutas do historico recente, com a duracao da faixa."""

        def _op() -> List[sqlite3.Row]:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT played_at_ms, duration_ms, track_id FROM spotify_plays "
                    "WHERE discord_user_id = ? AND played_at_ms >= ? AND played_at_ms < ? "
                    "ORDER BY played_at_ms ASC",
                    (str(discord_user_id), start_ms, end_ms),
                ).fetchall()

        return [dict(row) for row in await self._run(_op)]

    async def raw_measured(
        self, discord_user_id: int, start_ms: int, end_ms: int
    ) -> List[Dict[str, Any]]:
        """Camada 2: sessoes medidas pela amostragem do player."""

        def _op() -> List[sqlite3.Row]:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT started_ms, ended_ms, ms_played, track_id FROM spotify_measured "
                    "WHERE discord_user_id = ? AND started_ms < ? AND ended_ms >= ? "
                    "ORDER BY started_ms ASC",
                    (str(discord_user_id), end_ms, start_ms),
                ).fetchall()

        return [dict(row) for row in await self._run(_op)]

    async def raw_imported(
        self, discord_user_id: int, start_ms: int, end_ms: int
    ) -> List[Dict[str, Any]]:
        """Camada 3: ms_played real vindo do arquivo do Spotify."""

        def _op() -> List[sqlite3.Row]:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT started_ms, ended_ms, ms_played, track_id, track_name "
                    "FROM spotify_imported "
                    "WHERE discord_user_id = ? AND started_ms < ? AND ended_ms >= ? "
                    "ORDER BY started_ms ASC",
                    (str(discord_user_id), end_ms, start_ms),
                ).fetchall()

        return [dict(row) for row in await self._run(_op)]

    async def upsert_measured(
        self,
        discord_user_id: int,
        track_id: str,
        started_ms: int,
        ended_ms: int,
        ms_played: int,
    ) -> None:
        """Grava/atualiza a sessao medida. Persistir a cada amostra evita perder
        o que ja foi medido se o bot reiniciar no meio de uma faixa."""

        def _op() -> None:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO spotify_measured (
                        discord_user_id, track_id, started_ms, ended_ms, ms_played
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(discord_user_id, track_id, started_ms) DO UPDATE SET
                        ended_ms  = excluded.ended_ms,
                        ms_played = excluded.ms_played
                    """,
                    (str(discord_user_id), track_id, started_ms, ended_ms, ms_played),
                )

        await self._run(_op)

    async def record_imported(
        self, discord_user_id: int, entries: Sequence[Dict[str, Any]]
    ) -> int:
        """Insere entradas do arquivo do Spotify, ignorando duplicatas."""
        if not entries:
            return 0

        rows = [
            (
                str(discord_user_id),
                entry["started_ms"],
                entry["ended_ms"],
                entry["track_key"],
                entry.get("track_id"),
                entry.get("track_name"),
                entry.get("artists"),
                entry["ms_played"],
            )
            for entry in entries
        ]

        def _op() -> int:
            with self._connect() as conn:
                before = conn.total_changes
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO spotify_imported (
                        discord_user_id, started_ms, ended_ms, track_key,
                        track_id, track_name, artists, ms_played
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                return conn.total_changes - before

        return await self._run(_op)

    async def imported_range(self, discord_user_id: int) -> Optional[Dict[str, Any]]:
        """Menor e maior data importada, para dizer o que o arquivo cobre."""

        def _op() -> Optional[sqlite3.Row]:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT MIN(started_ms) AS inicio, MAX(ended_ms) AS fim, "
                    "COUNT(*) AS total FROM spotify_imported WHERE discord_user_id = ?",
                    (str(discord_user_id),),
                ).fetchone()

        row = await self._run(_op)
        if row is None or row["total"] == 0:
            return None
        return {"inicio": int(row["inicio"]), "fim": int(row["fim"]), "total": int(row["total"])}

    async def top_artist_ids(
        self, discord_user_id: int, start_ms: int, end_ms: int, limit: int = 40
    ) -> List[Dict[str, Any]]:
        """Artistas principais mais ouvidos na janela, com o id do Spotify."""

        def _op() -> List[sqlite3.Row]:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT artist_ids, artists FROM spotify_plays "
                    "WHERE discord_user_id = ? AND played_at_ms >= ? AND played_at_ms < ?",
                    (str(discord_user_id), start_ms, end_ms),
                ).fetchall()

        rows = await self._run(_op)
        contagem: Dict[str, int] = {}
        nomes: Dict[str, str] = {}

        for row in rows:
            try:
                ids = json.loads(row["artist_ids"] or "[]")
            except (ValueError, TypeError):
                ids = []
            if not ids:
                continue
            principal = ids[0]
            contagem[principal] = contagem.get(principal, 0) + 1
            nomes.setdefault(principal, (row["artists"] or "").split(", ")[0].strip())

        ordenados = sorted(contagem.items(), key=lambda item: -item[1])[:limit]
        return [
            {"artist_id": aid, "name": nomes.get(aid, "?"), "plays": n}
            for aid, n in ordenados
        ]

    async def get_cached_genres(self, artist_ids: Sequence[str]) -> Dict[str, List[str]]:
        if not artist_ids:
            return {}

        def _op() -> List[sqlite3.Row]:
            marcadores = ",".join("?" * len(artist_ids))
            with self._connect() as conn:
                return conn.execute(
                    f"SELECT artist_id, genres FROM spotify_artist_genres "
                    f"WHERE artist_id IN ({marcadores})",
                    tuple(artist_ids),
                ).fetchall()

        rows = await self._run(_op)
        resultado = {}
        for row in rows:
            try:
                resultado[row["artist_id"]] = json.loads(row["genres"])
            except (ValueError, TypeError):
                resultado[row["artist_id"]] = []
        return resultado

    async def cache_genres(
        self, artist_id: str, name: Optional[str], genres: List[str], fetched_at: int
    ) -> None:
        def _op() -> None:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO spotify_artist_genres (artist_id, name, genres, fetched_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(artist_id) DO UPDATE SET "
                    "name = excluded.name, genres = excluded.genres, "
                    "fetched_at = excluded.fetched_at",
                    (artist_id, name, json.dumps(genres, ensure_ascii=False), fetched_at),
                )

        await self._run(_op)

    async def shared_track_ids(
        self, user_a: int, user_b: int, start_ms: int, end_ms: int, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Faixas que as duas pessoas ouviram na janela, das mais tocadas para as menos."""

        def _op() -> List[Dict[str, Any]]:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    SELECT a.track_id,
                           MAX(a.track_name) AS track_name,
                           MAX(a.artists)    AS artists,
                           MAX(a.track_url)  AS track_url,
                           COUNT(*) AS plays_a,
                           (SELECT COUNT(*) FROM spotify_plays b
                             WHERE b.discord_user_id = ?
                               AND b.track_id = a.track_id
                               AND b.played_at_ms >= ? AND b.played_at_ms < ?) AS plays_b
                      FROM spotify_plays a
                     WHERE a.discord_user_id = ?
                       AND a.played_at_ms >= ? AND a.played_at_ms < ?
                     GROUP BY a.track_id
                    HAVING plays_b > 0
                     ORDER BY (plays_a + plays_b) DESC
                     LIMIT ?
                    """,
                    (str(user_b), start_ms, end_ms, str(user_a), start_ms, end_ms, limit),
                )
                return [dict(row) for row in cur.fetchall()]

        return await self._run(_op)

    async def first_play_at(self, discord_user_id: int) -> Optional[int]:
        def _op() -> Optional[int]:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT MIN(played_at_ms) FROM spotify_plays WHERE discord_user_id = ?",
                    (str(discord_user_id),),
                )
                value = cur.fetchone()[0]
                return int(value) if value is not None else None

        return await self._run(_op)

    # ------------------------------------------------------------------ #
    # Chave/valor (painel, resumos publicados)
    # ------------------------------------------------------------------ #

    async def get_value(self, key: str) -> Optional[str]:
        def _op() -> Optional[str]:
            with self._connect() as conn:
                cur = conn.execute("SELECT value FROM spotify_kv WHERE key = ?", (key,))
                row = cur.fetchone()
                return row["value"] if row else None

        return await self._run(_op)

    async def set_value(self, key: str, value: str) -> None:
        def _op() -> None:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO spotify_kv (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

        await self._run(_op)

    async def delete_value(self, key: str) -> None:
        def _op() -> None:
            with self._connect() as conn:
                conn.execute("DELETE FROM spotify_kv WHERE key = ?", (key,))

        await self._run(_op)

    # ------------------------------------------------------------------ #
    # States de OAuth
    # ------------------------------------------------------------------ #

    async def save_oauth_state(self, state: str, discord_user_id: int, created_at: int) -> None:
        def _op() -> None:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO spotify_oauth_states (state, discord_user_id, created_at) "
                    "VALUES (?, ?, ?)",
                    (state, str(discord_user_id), created_at),
                )

        await self._run(_op)

    async def consume_oauth_state(self, state: str, max_age_seconds: int, now: int) -> Optional[int]:
        """Valida e remove o state. Retorna o discord_user_id se valido e dentro do prazo."""

        def _op() -> Optional[int]:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT discord_user_id, created_at FROM spotify_oauth_states WHERE state = ?",
                    (state,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                conn.execute("DELETE FROM spotify_oauth_states WHERE state = ?", (state,))
                if now - int(row["created_at"]) > max_age_seconds:
                    return None
                return int(row["discord_user_id"])

        return await self._run(_op)

    async def purge_expired_states(self, max_age_seconds: int, now: int) -> None:
        def _op() -> None:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM spotify_oauth_states WHERE created_at < ?",
                    (now - max_age_seconds,),
                )

        await self._run(_op)
