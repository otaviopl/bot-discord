"""Leitura do Extended Streaming History que a pessoa baixa da conta do Spotify.

E a unica fonte com `ms_played` real — o mesmo dado que alimenta o Wrapped. Nao existe
endpoint equivalente: o arquivo e pedido em Conta > Privacidade e chega por e-mail em
ate 30 dias, como um zip de JSONs.

Dois formatos circulam:

- Extended (`Streaming_History_Audio_*.json`): `ts`, `ms_played`, `spotify_track_uri`,
  `master_metadata_track_name`, `master_metadata_album_artist_name`.
- Antigo (`StreamingHistory*.json`): `endTime`, `msPlayed`, `trackName`, `artistName`,
  sem identificador de faixa e com o horario em minutos.

Nos dois, o timestamp marca quando a faixa PAROU de tocar; o inicio e calculado para
tras a partir do tempo tocado.
"""

import hashlib
import io
import json
import logging
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Limites contra arquivo malformado ou zip bomb.
MAX_ARQUIVOS = 200
MAX_BYTES_DESCOMPRIMIDOS = 512 * 1024 * 1024  # 512 MB somados
MIN_MS_PLAYED = 1000  # abaixo de 1s e ruido, nao escuta


class ImportError_(Exception):
    """Arquivo invalido ou fora dos limites."""


def _to_ms(valor: str) -> Optional[int]:
    """Aceita '2023-01-01T12:00:00Z' (extended) e '2023-01-01 12:00' (antigo)."""
    if not valor:
        return None
    texto = valor.strip().replace("Z", "+00:00")
    for tentativa in (texto, texto.replace(" ", "T")):
        try:
            momento = datetime.fromisoformat(tentativa)
        except ValueError:
            continue
        if momento.tzinfo is None:
            momento = momento.replace(tzinfo=timezone.utc)
        return int(momento.timestamp() * 1000)
    return None


def _track_key(track_id: Optional[str], nome: Optional[str], artista: Optional[str]) -> str:
    """Chave estavel para deduplicar. Usa o id quando existe; senao, nome + artista."""
    if track_id:
        return track_id
    bruto = f"{(nome or '').lower()}|{(artista or '').lower()}"
    return "h:" + hashlib.sha1(bruto.encode("utf-8")).hexdigest()[:24]


def _parse_entry(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None

    # Podcast nao entra: o modulo e de musica.
    if raw.get("episode_name") or raw.get("spotify_episode_uri"):
        return None

    if "ms_played" in raw:  # formato extended
        ms_played = raw.get("ms_played")
        fim = _to_ms(raw.get("ts", ""))
        nome = raw.get("master_metadata_track_name")
        artista = raw.get("master_metadata_album_artist_name")
        uri = raw.get("spotify_track_uri") or ""
        track_id = uri.rsplit(":", 1)[-1] if uri.startswith("spotify:track:") else None
    elif "msPlayed" in raw:  # formato antigo
        ms_played = raw.get("msPlayed")
        fim = _to_ms(raw.get("endTime", ""))
        nome = raw.get("trackName")
        artista = raw.get("artistName")
        track_id = None
    else:
        return None

    if fim is None or not isinstance(ms_played, int) or ms_played < MIN_MS_PLAYED:
        return None
    if not nome and not track_id:
        return None  # entrada sem metadado nenhum, nao da para deduplicar

    return {
        "started_ms": fim - ms_played,
        "ended_ms": fim,
        "ms_played": ms_played,
        "track_id": track_id,
        "track_name": nome,
        "artists": artista,
        "track_key": _track_key(track_id, nome, artista),
    }


def _parse_json_bytes(dados: bytes) -> List[Dict[str, Any]]:
    try:
        conteudo = json.loads(dados.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ImportError_(f"JSON inválido: {exc}") from exc

    if not isinstance(conteudo, list):
        raise ImportError_("O arquivo deveria conter uma lista de reproduções.")

    entradas = []
    for bruto in conteudo:
        entrada = _parse_entry(bruto)
        if entrada is not None:
            entradas.append(entrada)
    return entradas


def parse_upload(dados: bytes, nome_arquivo: str) -> Dict[str, Any]:
    """Le um .json ou um .zip do Spotify e devolve as escutas normalizadas."""
    nome = (nome_arquivo or "").lower()

    if nome.endswith(".json"):
        entradas = _parse_json_bytes(dados)
        return {"entradas": entradas, "arquivos": 1, "ignorados": []}

    if not nome.endswith(".zip"):
        raise ImportError_("Envie o `.zip` que o Spotify manda, ou um `.json` de dentro dele.")

    try:
        arquivo = zipfile.ZipFile(io.BytesIO(dados))
    except zipfile.BadZipFile as exc:
        raise ImportError_("O zip está corrompido ou não é um zip.") from exc

    membros = [
        info
        for info in arquivo.infolist()
        if not info.is_dir() and info.filename.lower().endswith(".json")
    ]
    if not membros:
        raise ImportError_("Não achei nenhum `.json` dentro do zip.")
    if len(membros) > MAX_ARQUIVOS:
        raise ImportError_(f"O zip tem {len(membros)} arquivos; o limite é {MAX_ARQUIVOS}.")

    total_bytes = sum(info.file_size for info in membros)
    if total_bytes > MAX_BYTES_DESCOMPRIMIDOS:
        raise ImportError_("O conteúdo descomprimido passa do limite de 512 MB.")

    entradas: List[Dict[str, Any]] = []
    ignorados: List[str] = []
    lidos = 0

    for info in membros:
        base = info.filename.rsplit("/", 1)[-1].lower()
        # Só os arquivos de histórico de áudio; playlists, biblioteca etc. ficam de fora.
        if not (base.startswith("streaming_history_audio") or base.startswith("streaminghistory")):
            ignorados.append(info.filename)
            continue
        try:
            with arquivo.open(info) as membro:
                entradas.extend(_parse_json_bytes(membro.read()))
            lidos += 1
        except ImportError_ as exc:
            ignorados.append(f"{info.filename} ({exc})")

    if not lidos:
        raise ImportError_(
            "O zip não tem arquivos de histórico de áudio. "
            "Procure por `Streaming_History_Audio_*.json` — se o seu zip só tem "
            "`StreamingHistory0.json`, você baixou o histórico curto, não o estendido."
        )

    return {"entradas": entradas, "arquivos": lidos, "ignorados": ignorados}


def resumo(entradas: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Estatísticas para mostrar ao usuário depois do import."""
    if not entradas:
        return {"total": 0, "ms": 0, "inicio_ms": None, "fim_ms": None}
    return {
        "total": len(entradas),
        "ms": sum(e["ms_played"] for e in entradas),
        "inicio_ms": min(e["started_ms"] for e in entradas),
        "fim_ms": max(e["ended_ms"] for e in entradas),
    }
