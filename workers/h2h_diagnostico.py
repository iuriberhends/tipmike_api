# -*- coding: utf-8 -*-
r"""
workers/h2h_diagnostico.py — "o dado desta liga está bom?", em português.

Devolve o CONTRATO que o painel desenha. Toda a decisão (verde/amarelo/
vermelho) e todo o texto saem daqui — o front só pinta. Assim a mesma
explicação serve pro painel, pro log e pra qualquer relatório, e a tela
nunca diz uma coisa enquanto o script diz outra.

AS QUATRO PERGUNTAS
  1. Tem histórico suficiente?   (por janela de filtro, contando só o que
                                  existia ANTES de cada jogo)
  2. Os placares conferem?       (TipManager x nosso coletor, onde as duas
                                  cobrem o mesmo jogo)
  3. Os nomes estão do lado certo? (placar amarrado ao jogador certo)
  4. Os horários batem?          (as duas guardam fuso diferente; se a
                                  conversão falhar, o mesmo jogo conta 2x)

POR QUE NÃO CONSULTAMOS A TIPMANAGER AO VIVO
  O h2h_historico VEIO dela. Comparar os dois seria espelho, não testemunha:
  se o seeder gravou torto, erraria de novo do mesmo jeito. O teste que vale
  é cruzar as duas fontes INDEPENDENTES que já estão gravadas — a TM (do
  organizador do torneio) e o nosso coletor (do feed da casa).

DE ONDE VÊM AS CONSULTAS
  Copiadas do workers/backtest_runner.py, não inferidas: duas pernas em
  UNION ALL filtradas por SPORT + NICK (nunca por liga), com o ts do
  histórico convertido por `AT TIME ZONE 'America/Sao_Paulo'` — a coluna é
  timestamp SEM fuso gravada em horário de Brasília pelo seeder da TM.
"""
from __future__ import annotations

import bisect
from datetime import timedelta

# janelas de filtro que o painel oferece
JANELAS = (10, 20, 30, 50)
MIN_GOLEADA = 6           # mínimo de jogos do filtro de atropelo
JANELA_CASAMENTO_S = 2700 # 45min — a mesma do dedup do runner

# pisos de aprovação de cada checagem
PISO_HISTORICO = 80.0     # % de jogos com base pra MAIOR janela pedida
PISO_PLACAR = 97.0
TETO_ESPELHADO = 3.0


# ------------------------------------------------------------------ fuso --
# BLINDAGEM. As duas pernas voltam MISTURADAS: a do coletor vem de `ts_fim`
# (timestamptz -> chega COM fuso) e a da TM de `ts AT TIME ZONE
# 'America/Sao_Paulo'` (chega SEM). Comparar as duas — ou compará-las com o
# ts do parquet — estoura em "can't compare offset-naive and offset-aware".
# Aqui TUDO vira horário de Brasília SEM fuso, que é a escala em que o
# runner compara. Uma função só, usada em todo ponto de entrada, pra não
# sobrar caminho por onde um datetime com fuso escape.
try:
    from zoneinfo import ZoneInfo
    _BRT = ZoneInfo('America/Sao_Paulo')
except Exception:                                    # pragma: no cover
    _BRT = None


def _brt(dt):
    """datetime -> horário de Brasília, sem fuso. Idempotente e tolerante:
    None passa direto, valor sem fuso volta igual."""
    if dt is None or getattr(dt, 'tzinfo', None) is None:
        return dt
    if _BRT is not None:
        return dt.astimezone(_BRT).replace(tzinfo=None)
    return (dt - dt.utcoffset()).replace(tzinfo=None)  # cai pra UTC


# =============================================================== consultas ==
SQL_JOGADORES = """
SELECT jogador, ts, fonte FROM (
    SELECT UPPER(jogador_a) AS jogador, ts_fim AS ts, 'coletor' AS fonte
      FROM h2h_matches
     WHERE bookmaker = $1 AND sport = $2 AND UPPER(jogador_a) = ANY($3::text[])
       AND score_a IS NOT NULL AND score_b IS NOT NULL
    UNION ALL
    SELECT UPPER(jogador_b), ts_fim, 'coletor'
      FROM h2h_matches
     WHERE bookmaker = $1 AND sport = $2 AND UPPER(jogador_b) = ANY($3::text[])
       AND score_a IS NOT NULL AND score_b IS NOT NULL
    UNION ALL
    SELECT UPPER(jogador_a), (ts AT TIME ZONE 'America/Sao_Paulo'), 'tm'
      FROM h2h_historico
     WHERE sport = $2 AND UPPER(jogador_a) = ANY($3::text[])
       AND score_home IS NOT NULL AND score_away IS NOT NULL
    UNION ALL
    SELECT UPPER(jogador_b), (ts AT TIME ZONE 'America/Sao_Paulo'), 'tm'
      FROM h2h_historico
     WHERE sport = $2 AND UPPER(jogador_b) = ANY($3::text[])
       AND score_home IS NOT NULL AND score_away IS NOT NULL
) u
"""

# Cruzamento das duas fontes, na escala de Brasília nos dois lados (a mesma
# que o runner usa). Comparar contra UTC dá pico em -3h — a diferença
# BRT/UTC — e parece erro de fuso quando é o esperado.
SQL_CRUZAMENTO = """
WITH c AS (
    SELECT UPPER(jogador_a) a, UPPER(jogador_b) b,
           (ts_fim AT TIME ZONE 'America/Sao_Paulo') ts, score_a, score_b
      FROM h2h_matches
     WHERE bookmaker = $1 AND sport = $2 AND ts_fim >= $3
       AND score_a IS NOT NULL AND score_b IS NOT NULL
), t AS (
    SELECT UPPER(jogador_a) a, UPPER(jogador_b) b, ts, score_home, score_away
      FROM h2h_historico
     WHERE sport = $2 AND ts >= $4
       AND score_home IS NOT NULL AND score_away IS NOT NULL
)
SELECT
    COUNT(*) AS n,
    COUNT(*) FILTER (WHERE CASE WHEN t.a = c.a
        THEN (t.score_home = c.score_a AND t.score_away = c.score_b)
        ELSE (t.score_home = c.score_b AND t.score_away = c.score_a) END) AS placar_ok,
    COUNT(*) FILTER (WHERE CASE WHEN t.a = c.a
        THEN (t.score_home = c.score_b AND t.score_away = c.score_a)
        ELSE (t.score_home = c.score_a AND t.score_away = c.score_b) END) AS espelhado,
    -- O coletor pega o tick de MAIOR soma de placar. Se a conexao caiu antes
    -- do fim, esse valor e' PARCIAL (menor que o oficial). Isso e' esperado,
    -- o proprio h2h_sync documenta, e o runner resolve preferindo a TM no
    -- dedup. Entao "coletor com total MENOR" NAO e' divergencia — so' quando
    -- os dois estao completos e mesmo assim discordam e' que tem problema.
    COUNT(*) FILTER (WHERE (c.score_a + c.score_b) < (t.score_home + t.score_away)
        AND NOT (CASE WHEN t.a = c.a
            THEN (t.score_home = c.score_a AND t.score_away = c.score_b)
            ELSE (t.score_home = c.score_b AND t.score_away = c.score_a) END)) AS coletor_parcial,
    -- lag tipico: o proprio h2h_sync diz 20-40min entre tick e TM, e o dedup
    -- do runner usa 45min por causa disso. Medir com 15min dava "nao batem"
    -- num sistema funcionando.
    COUNT(*) FILTER (WHERE ABS(EXTRACT(EPOCH FROM (t.ts - c.ts))) <= 2700) AS dentro_da_janela,
    ROUND(AVG(ABS(EXTRACT(EPOCH FROM (t.ts - c.ts)))) / 60.0) AS lag_medio_min
  FROM c JOIN t
    ON ((t.a = c.a AND t.b = c.b) OR (t.a = c.b AND t.b = c.a))
   -- JANELA ASSIMETRICA, e a direcao surpreende: a TipManager grava o
   -- horario de INICIO do jogo e a h2h_matches grava o `ts_fim` (ultimo
   -- tick). Como a partida dura ~20-25min, o registro da TM e' ANTERIOR ao
   -- nosso. Medido em 19/ago: nessa direcao sao 10.032 casamentos com 7.544
   -- placares identicos; na direcao oposta, 879 casamentos com 10 identicos
   -- — ou seja, casar "TM depois" pega a partida SEGUINTE do mesmo par (em
   -- liga de ciclo rapido o confronto se repete a cada 20-30min) e inventa
   -- divergencia onde nao tem.
   AND t.ts BETWEEN c.ts - ($5 * INTERVAL '1 second') AND c.ts + INTERVAL '10 minutes'
"""

SQL_FONTES = """
SELECT 'coletor' AS fonte, COUNT(*) AS jogos, MAX(ts_fim AT TIME ZONE 'America/Sao_Paulo') AS ate
  FROM h2h_matches WHERE bookmaker = $1 AND sport = $2
UNION ALL
SELECT 'tm', COUNT(*), MAX(ts AT TIME ZONE 'America/Sao_Paulo')
  FROM h2h_historico WHERE sport = $2
"""


# ================================================================ helpers ===
def _pct(parte, total):
    return round(parte / total * 100, 1) if total else 0.0


def _mil(n):
    return f'{int(n):,}'.replace(',', '.')


def _check(pergunta, valor, ok, detalhe, o_que_fazer=None):
    return {'pergunta': pergunta, 'valor': valor, 'ok': bool(ok),
            'detalhe': detalhe, 'o_que_fazer': o_que_fazer}


def montar_veredito(checagens):
    """Verde se todas passam. Amarelo se SÓ a de histórico falha (dá pra
    garimpar com janela menor). Vermelho se placar/nomes/horários falham —
    aí o número mente e não adianta ajustar filtro."""
    falhas = [c for c in checagens if not c['ok']]
    if not falhas:
        return 'confiavel', 'Pode garimpar.'
    graves = [c for c in falhas if c['pergunta'] != 'Tem histórico suficiente?']
    if graves:
        return 'nao_use', ('O dado tem problema de conteúdo, não de quantidade. '
                           'Ajustar filtro não resolve.')
    return 'atencao', ('O dado está correto, mas o histórico é curto para as '
                       'janelas maiores.')


# ============================================================== diagnostico =
async def diagnosticar(pool, *, casa: str, sport: str, nicks: list,
                       inicio, fim, janelas=JANELAS, jogos_periodo=None):
    """`jogos_periodo`: lista de (jogador_a, jogador_b, ts) do parquet/ticks —
    os jogos que o backtest vai processar. Sem ela, a checagem de histórico é
    pulada (e o veredito sai sem ela)."""
    nicks = [n.upper() for n in nicks]
    inicio, fim = _brt(inicio), _brt(fim)
    if jogos_periodo:
        jogos_periodo = [(a, b, _brt(t)) for a, b, t in jogos_periodo]
    checagens, jogadores, filtros = [], [], []

    async with pool.acquire() as conn:
        linhas = await conn.fetch(SQL_JOGADORES, casa, sport, nicks)
        fontes_raw = await conn.fetch(SQL_FONTES, casa, sport)
        cruz = await conn.fetchrow(
            SQL_CRUZAMENTO, casa, sport,
            inicio - timedelta(days=90), inicio - timedelta(days=90),
            JANELA_CASAMENTO_S)

    # ---- por jogador: quando cada jogo aconteceu ----
    por_jog = {}
    for r in linhas:
        t = _brt(r['ts'])
        if t is not None:
            por_jog.setdefault(r['jogador'], []).append(t)
    for n in nicks:
        ts = sorted(por_jog.get(n, []))
        por_jog[n] = ts
        antes = bisect.bisect_left(ts, inicio)
        jogadores.append({'nick': n, 'antes': antes,
                          'ultimo': ts[-1].date().isoformat() if ts else None})
    jogadores.sort(key=lambda x: x['antes'])

    # ---- 1. tem histórico suficiente? ----
    if jogos_periodo:
        n_jogos = len(jogos_periodo)
        cont = {w: 0 for w in janelas}
        cont['goleada'] = 0
        for ja, jb, ts in jogos_periodo:
            pior = min(bisect.bisect_left(por_jog.get(ja.upper(), []), ts),
                       bisect.bisect_left(por_jog.get(jb.upper(), []), ts))
            for w in janelas:
                if pior >= w:
                    cont[w] += 1
            if pior >= MIN_GOLEADA:
                cont['goleada'] += 1
        for w in janelas:
            p = _pct(cont[w], n_jogos)
            filtros.append({'nome': f'Filtro dos últimos {w} confrontos',
                            'pct': p, 'ok': p >= PISO_HISTORICO})
        p = _pct(cont['goleada'], n_jogos)
        filtros.append({'nome': 'Filtro de goleada', 'pct': p,
                        'ok': p >= PISO_HISTORICO})
        maior = max(janelas)
        pm = _pct(cont[maior], n_jogos)
        oqf = None
        if pm < PISO_HISTORICO:
            bons = [w for w in janelas if _pct(cont[w], n_jogos) >= PISO_HISTORICO]
            oqf = (f'{100 - pm:.0f}% dos jogos não têm confrontos bastante para o '
                   f'filtro de {maior}. ' +
                   (f'Use filtros de até {max(bons)} confrontos, '
                    if bons else 'Nenhuma janela tem base suficiente. ') +
                   'ou escolha um período mais recente, quando o histórico já '
                   'acumulou.')
        checagens.append(_check(
            'Tem histórico suficiente?', f'{pm:.0f}%', pm >= PISO_HISTORICO,
            f'{_mil(cont[maior])} de {_mil(n_jogos)} jogos têm base para o '
            f'filtro de {maior} confrontos', oqf))

    # ---- 2, 3 e 4: as duas fontes concordam? ----
    n = int(cruz['n'] or 0)
    if n:
        parcial = int(cruz['coletor_parcial'] or 0)
        # parcial do coletor NAO e' erro — o runner ja prefere a TM.
        ok_pl = _pct(int(cruz['placar_ok']) + parcial, n)
        esp = _pct(cruz['espelhado'], n)
        prox = _pct(cruz['dentro_da_janela'], n)
        lag = int(cruz['lag_medio_min'] or 0)
        checagens.append(_check(
            'Os placares conferem?', f'{ok_pl:.0f}%', ok_pl >= PISO_PLACAR,
            f'{_mil(cruz["placar_ok"])} de {_mil(n)} jogos com o placar '
            f'idêntico nas duas' +
            (f'; em {_mil(parcial)} o nosso coletor ficou com placar parcial '
             f'(caiu antes do fim) — nesses o sistema usa o da TipManager'
             if parcial else ''),
            None if ok_pl >= PISO_PLACAR else
            'Uma das duas fontes está gravando placar errado. Não use nenhum '
            'número desta liga até descobrir qual.'))
        checagens.append(_check(
            'Os nomes estão do lado certo?', f'{100 - esp:.0f}%',
            esp <= TETO_ESPELHADO,
            f'{_mil(cruz["espelhado"])} jogos com o placar trocado de lado',
            None if esp <= TETO_ESPELHADO else
            'O placar está amarrado ao jogador errado. Toda conta de handicap '
            'sai invertida. Pare tudo e avise o suporte.'))
        checagens.append(_check(
            'Os horários das duas fontes batem?', 'sim' if prox >= 80 else 'não',
            prox >= 80,
            f'{prox:.0f}% dos jogos em comum caem dentro da janela de 45 minutos '
            f'que o sistema usa para saber que é a mesma partida '
            f'(diferença média de {lag} min — a TipManager publica depois do jogo)',
            None if prox >= 60 else
            'As duas fontes registram o mesmo jogo em horários diferentes. O '
            'sistema não percebe que é a mesma partida e conta duas vezes.'))
    else:
        checagens.append(_check(
            'Os placares conferem?', '—', True,
            'As duas fontes não cobrem nenhum jogo em comum neste período, '
            'então não dá para conferir uma contra a outra.'))

    veredito, resumo = montar_veredito(checagens)
    NOME = {'tm': 'TipManager (placar oficial)', 'coletor': f'Nosso coletor ({casa})'}
    fontes = [{'nome': NOME.get(r['fonte'], r['fonte']), 'jogos': int(r['jogos'] or 0),
               'ate': _brt(r['ate']).date().isoformat() if r['ate'] else None}
              for r in fontes_raw]
    return {
        'veredito': veredito, 'resumo': resumo, 'checagens': checagens,
        'fontes': fontes, 'sobreposicao': n, 'filtros': filtros,
        'jogadores': jogadores,
        'periodo': {'de': inicio.date().isoformat(), 'ate': fim.date().isoformat()},
    }
