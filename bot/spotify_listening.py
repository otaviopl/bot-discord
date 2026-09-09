"""Tempo ouvido: combina as tres fontes disponiveis sem contar o mesmo periodo duas vezes.

A Spotify Web API nao expoe minutos ouvidos em nenhum endpoint, entao o numero e
sempre derivado. Ha tres fontes, da mais confiavel para a menos:

1. `importado` — `ms_played` real do Extended Streaming History que a pessoa baixa da
   conta. E o mesmo dado que alimenta o Wrapped, mas so cobre ate a data do arquivo.
2. `medido`   — amostragem do `progress_ms` do player enquanto o bot esta no ar.
   Pega pausa e faixa pulada de verdade, mas tem buraco quando o bot cai.
3. `estimado` — derivado do historico recente: duracao da faixa limitada ao intervalo
   ate a escuta seguinte. Custo zero, cobre o resto.

Cada fonte vira um intervalo de tempo. Fontes melhores sao aplicadas primeiro e o
periodo que elas cobrem e subtraido das piores, entao um mesmo minuto nunca entra duas
vezes e sempre entra pela melhor fonte disponivel.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Teto para escutas gravadas antes de a duracao passar a ser salva.
MAX_TRACK_MS = 15 * 60 * 1000

EXATO = "exato"
MEDIDO = "medido"
ESTIMADO = "estimado"


@dataclass(frozen=True)
class Segment:
    start_ms: int
    end_ms: int
    ms_played: int
    source: str

    @property
    def span_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


def merge_intervals(intervals: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Une intervalos que se tocam ou se sobrepoem."""
    if not intervals:
        return []

    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def subtract(
    segment: Segment, covered: Sequence[Tuple[int, int]]
) -> List[Segment]:
    """Devolve os pedacos de `segment` que ainda nao estao cobertos.

    O `ms_played` e repartido na proporcao do tempo que sobrou: se metade do intervalo
    ja estava coberta por uma fonte melhor, so metade do tempo ouvido e aproveitada.
    """
    if segment.span_ms <= 0:
        return [] if segment.ms_played <= 0 else [segment]

    pedacos: List[Tuple[int, int]] = []
    cursor = segment.start_ms

    for cov_start, cov_end in covered:
        if cov_end <= cursor:
            continue
        if cov_start >= segment.end_ms:
            break
        if cov_start > cursor:
            pedacos.append((cursor, min(cov_start, segment.end_ms)))
        cursor = max(cursor, cov_end)
        if cursor >= segment.end_ms:
            break

    if cursor < segment.end_ms:
        pedacos.append((cursor, segment.end_ms))

    resultado = []
    for start, end in pedacos:
        fracao = (end - start) / segment.span_ms
        ms = int(round(segment.ms_played * fracao))
        if ms > 0:
            resultado.append(Segment(start, end, ms, segment.source))
    return resultado


def clip(segment: Segment, window_start: int, window_end: int) -> Optional[Segment]:
    """Recorta o segmento a janela consultada, repartindo o tempo na proporcao."""
    start = max(segment.start_ms, window_start)
    end = min(segment.end_ms, window_end)
    if end <= start:
        return None
    if segment.span_ms <= 0:
        return Segment(start, end, segment.ms_played, segment.source)

    fracao = (end - start) / segment.span_ms
    ms = int(round(segment.ms_played * fracao))
    if ms <= 0:
        return None
    return Segment(start, end, ms, segment.source)


# --------------------------------------------------------------------------- #
# Conversao de cada fonte em segmentos
# --------------------------------------------------------------------------- #


def segments_from_imported(rows: Sequence[Dict[str, Any]]) -> List[Segment]:
    """O arquivo do Spotify ja traz o intervalo pronto: `ts` e o fim da reproducao e
    o inicio foi calculado para tras a partir de `ms_played` na hora do import."""
    segments = []
    for row in rows:
        ms = int(row["ms_played"] or 0)
        if ms <= 0:
            continue
        segments.append(
            Segment(int(row["started_ms"]), int(row["ended_ms"]), ms, EXATO)
        )
    return segments


def segments_from_measured(rows: Sequence[Dict[str, Any]]) -> List[Segment]:
    segments = []
    for row in rows:
        ms = int(row["ms_played"] or 0)
        if ms <= 0:
            continue
        segments.append(
            Segment(int(row["started_ms"]), int(row["ended_ms"]), ms, MEDIDO)
        )
    return segments


def segments_from_plays(rows: Sequence[Dict[str, Any]]) -> List[Segment]:
    """Camada 1: duracao da faixa limitada ao intervalo ate a escuta seguinte.

    Somar a duracao cheia superestimaria quem pula musica; quem trocou de faixa em 40s
    conta 40s. A ultima escuta da janela nao tem proxima, entao usa a duracao cheia.
    """
    segments = []
    for index, row in enumerate(rows):
        inicio = int(row["played_at_ms"])
        duracao = row.get("duration_ms")
        gap = None
        if index + 1 < len(rows):
            gap = int(rows[index + 1]["played_at_ms"]) - inicio

        if duracao is None:
            # Escuta antiga, sem duracao gravada: so o intervalo serve de pista.
            if gap is not None and 0 < gap <= MAX_TRACK_MS:
                ms = gap
            else:
                continue
        else:
            duracao = int(duracao)
            ms = min(duracao, gap) if gap is not None and gap > 0 else duracao

        if ms > 0:
            segments.append(Segment(inicio, inicio + ms, ms, ESTIMADO))
    return segments


# --------------------------------------------------------------------------- #
# Resolucao
# --------------------------------------------------------------------------- #


def resolve_listening(
    imported: Sequence[Dict[str, Any]],
    measured: Sequence[Dict[str, Any]],
    plays: Sequence[Dict[str, Any]],
    window_start: int,
    window_end: int,
) -> Dict[str, Any]:
    """Combina as fontes por precedencia e devolve o total mais a composicao."""
    totais = {EXATO: 0, MEDIDO: 0, ESTIMADO: 0}
    cobertura: List[Tuple[int, int]] = []

    camadas = (
        segments_from_imported(imported),
        segments_from_measured(measured),
        segments_from_plays(plays),
    )

    for camada in camadas:
        aceitos: List[Segment] = []
        for bruto in camada:
            recortado = clip(bruto, window_start, window_end)
            if recortado is None:
                continue
            aceitos.extend(subtract(recortado, cobertura))

        for segmento in aceitos:
            totais[segmento.source] += segmento.ms_played

        # Só depois de processar a camada inteira, para que segmentos da mesma
        # camada não se anulem entre si.
        cobertura = merge_intervals(
            cobertura + [(s.start_ms, s.end_ms) for s in aceitos]
        )

    total = sum(totais.values())
    return {
        "total_ms": total,
        "exato_ms": totais[EXATO],
        "medido_ms": totais[MEDIDO],
        "estimado_ms": totais[ESTIMADO],
        "preciso": totais[ESTIMADO] == 0 and total > 0,
    }


def format_duration(ms: int) -> str:
    """3h 12min, 47min, ou 0min."""
    minutos = int(round(ms / 60000))
    if minutos < 60:
        return f"{minutos}min"
    horas, resto = divmod(minutos, 60)
    return f"{horas}h {resto:02d}min" if resto else f"{horas}h"


def describe_sources(resultado: Dict[str, Any]) -> str:
    """Diz de onde veio o numero, sem passar estimativa por medicao."""
    partes = []
    if resultado["exato_ms"]:
        partes.append(f"{format_duration(resultado['exato_ms'])} do histórico do Spotify")
    if resultado["medido_ms"]:
        partes.append(f"{format_duration(resultado['medido_ms'])} medidos pelo bot")
    if resultado["estimado_ms"]:
        partes.append(f"{format_duration(resultado['estimado_ms'])} estimados")
    return " · ".join(partes) if partes else "nada registrado"
