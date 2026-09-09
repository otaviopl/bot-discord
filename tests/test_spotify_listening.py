"""Combinação das três fontes de tempo ouvido, sem contagem dupla."""

from bot.spotify_listening import (
    ESTIMADO,
    MEDIDO,
    Segment,
    clip,
    describe_sources,
    format_duration,
    merge_intervals,
    resolve_listening,
    segments_from_plays,
    subtract,
)

MIN = 60_000
BASE = 1_700_000_000_000


def imported(offset_min, ms_played, nome="M"):
    """No arquivo do Spotify o timestamp e o FIM; o inicio vem de ms_played."""
    inicio = BASE + int(offset_min * MIN)
    return {"started_ms": inicio, "ended_ms": inicio + ms_played,
            "ms_played": ms_played, "track_id": "t", "track_name": nome}


def measured(start_min, end_min, ms_played):
    return {"started_ms": BASE + start_min * MIN, "ended_ms": BASE + end_min * MIN,
            "ms_played": ms_played, "track_id": "t"}


def play(offset_min, duration_ms, track="t"):
    return {"played_at_ms": BASE + int(offset_min * MIN), "duration_ms": duration_ms,
            "track_id": track}


def play_at_s(offset_s, duration_ms, track="t"):
    """Offset em segundos, para casos onde o arredondamento de minutos atrapalha."""
    return {"played_at_ms": BASE + offset_s * 1000, "duration_ms": duration_ms,
            "track_id": track}


class TestIntervalos:
    def test_merge_une_sobrepostos_e_adjacentes(self):
        assert merge_intervals([(0, 10), (5, 15), (20, 30)]) == [(0, 15), (20, 30)]
        assert merge_intervals([(0, 10), (10, 20)]) == [(0, 20)]
        assert merge_intervals([]) == []

    def test_merge_nao_une_separados(self):
        assert merge_intervals([(0, 10), (11, 20)]) == [(0, 10), (11, 20)]

    def test_subtract_remove_pedaco_do_meio(self):
        seg = Segment(0, 100, 100, ESTIMADO)
        pedacos = subtract(seg, [(40, 60)])

        assert [(p.start_ms, p.end_ms) for p in pedacos] == [(0, 40), (60, 100)]
        assert sum(p.ms_played for p in pedacos) == 80  # proporcional ao que sobrou

    def test_subtract_cobertura_total_zera(self):
        assert subtract(Segment(0, 100, 100, ESTIMADO), [(0, 100)]) == []

    def test_subtract_sem_cobertura_preserva(self):
        seg = Segment(0, 100, 90, ESTIMADO)
        assert subtract(seg, []) == [seg]

    def test_clip_recorta_proporcionalmente(self):
        seg = Segment(0, 100, 100, MEDIDO)
        recortado = clip(seg, 50, 200)

        assert (recortado.start_ms, recortado.end_ms) == (50, 100)
        assert recortado.ms_played == 50

    def test_clip_fora_da_janela(self):
        assert clip(Segment(0, 100, 100, MEDIDO), 200, 300) is None


class TestCamadaEstimada:
    def test_faixa_pulada_conta_so_o_intervalo(self):
        """Trocou de música em 40s: conta 40s, não os 3min da faixa."""
        segs = segments_from_plays([play_at_s(0, 180_000), play_at_s(40, 180_000)])

        assert segs[0].ms_played == 40_000
        assert segs[1].ms_played == 180_000  # última da janela, usa a duração cheia

    def test_faixa_ouvida_inteira_conta_a_duracao(self):
        segs = segments_from_plays([play(0, 180_000), play(10, 200_000)])

        assert segs[0].ms_played == 180_000  # gap de 10min > duração, então cabe inteira

    def test_escuta_antiga_sem_duracao_usa_o_intervalo(self):
        segs = segments_from_plays([play(0, None), play(3, None)])

        assert len(segs) == 1  # a última, sem duração e sem próxima, é descartada
        assert segs[0].ms_played == 3 * MIN

    def test_intervalo_absurdo_sem_duracao_e_descartado(self):
        """Bot ficou dias fora: o buraco não vira 3 dias de escuta."""
        segs = segments_from_plays([play(0, None), play(60 * 24, None)])
        assert segs == []


class TestPrecedencia:
    def test_so_estimativa(self):
        r = resolve_listening([], [], [play(0, 180_000), play(10, 180_000)],
                              BASE, BASE + 60 * MIN)

        assert r["estimado_ms"] == 360_000
        assert r["exato_ms"] == 0
        assert r["preciso"] is False

    def test_importado_tem_precedencia_sobre_estimativa(self):
        """Mesmo minuto coberto pelas duas fontes conta uma vez, pela melhor."""
        r = resolve_listening(
            [imported(0, 3 * MIN)],
            [],
            [play(0, 3 * MIN)],
            BASE, BASE + 60 * MIN,
        )

        assert r["exato_ms"] == 3 * MIN
        assert r["estimado_ms"] == 0
        assert r["total_ms"] == 3 * MIN
        assert r["preciso"] is True

    def test_medido_tem_precedencia_sobre_estimativa(self):
        r = resolve_listening(
            [],
            [measured(0, 3, 3 * MIN)],
            [play(0, 3 * MIN)],
            BASE, BASE + 60 * MIN,
        )

        assert r["medido_ms"] == 3 * MIN
        assert r["estimado_ms"] == 0

    def test_importado_tem_precedencia_sobre_medido(self):
        r = resolve_listening(
            [imported(0, 3 * MIN)],
            [measured(0, 3, 3 * MIN)],
            [],
            BASE, BASE + 60 * MIN,
        )

        assert r["exato_ms"] == 3 * MIN
        assert r["medido_ms"] == 0

    def test_fontes_em_periodos_diferentes_somam(self):
        """Importado cobre o começo, medido o meio, estimativa o fim."""
        r = resolve_listening(
            [imported(0, 5 * MIN)],
            [measured(10, 15, 5 * MIN)],
            [play(20, 5 * MIN), play(25, 5 * MIN)],
            BASE, BASE + 60 * MIN,
        )

        assert r["exato_ms"] == 5 * MIN
        assert r["medido_ms"] == 5 * MIN
        assert r["estimado_ms"] == 10 * MIN
        assert r["total_ms"] == 20 * MIN
        assert r["preciso"] is False

    def test_sobreposicao_parcial_reparte(self):
        """Importado cobre metade do período do medido; só a outra metade conta."""
        r = resolve_listening(
            [imported(0, 5 * MIN)],
            [measured(0, 10, 10 * MIN)],
            [],
            BASE, BASE + 60 * MIN,
        )

        assert r["exato_ms"] == 5 * MIN
        assert r["medido_ms"] == 5 * MIN
        assert r["total_ms"] == 10 * MIN

    def test_segmentos_da_mesma_camada_nao_se_anulam(self):
        """Duas escutas seguidas na mesma camada somam, não se subtraem."""
        r = resolve_listening([], [], [play(0, 3 * MIN), play(3, 3 * MIN), play(6, 3 * MIN)],
                              BASE, BASE + 60 * MIN)

        assert r["estimado_ms"] == 9 * MIN

    def test_janela_vazia(self):
        r = resolve_listening([], [], [], BASE, BASE + 60 * MIN)

        assert r["total_ms"] == 0
        assert r["preciso"] is False

    def test_escuta_que_atravessa_a_borda_da_janela(self):
        """Faixa começou antes do mês virar: só a parte de dentro conta."""
        r = resolve_listening(
            [imported(-2, 4 * MIN)],  # começou 2min antes da janela, durou 4min
            [], [],
            BASE, BASE + 60 * MIN,
        )

        assert r["exato_ms"] == 2 * MIN


class TestFormatacao:
    def test_duracao_legivel(self):
        assert format_duration(0) == "0min"
        assert format_duration(47 * MIN) == "47min"
        assert format_duration(60 * MIN) == "1h"
        assert format_duration(192 * MIN) == "3h 12min"

    def test_composicao_e_explicita(self):
        texto = describe_sources(
            {"exato_ms": 60 * MIN, "medido_ms": 30 * MIN, "estimado_ms": 15 * MIN}
        )

        assert "1h do histórico do Spotify" in texto
        assert "30min medidos pelo bot" in texto
        assert "15min estimados" in texto

    def test_sem_dados(self):
        assert describe_sources(
            {"exato_ms": 0, "medido_ms": 0, "estimado_ms": 0}
        ) == "nada registrado"
