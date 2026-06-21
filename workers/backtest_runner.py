"""
workers/backtest_runner.py - Worker do backtest (v5)

v5 - Stats H2H usam jogos disponiveis ate N (em vez de exigir N exato):
- _calcular_stats_h2h: WR ult 20 com 12 jogos agora calcula com 12 (em vez de
  retornar None). Tambem grava wr_ult{N}_qtd e media_ult{N}_qtd indicando
  quantos jogos foram usados.
- _aplicar_filtros_complementares: valida min_partidas contra qtd ESPECIFICA
  daquela janela. Antes validava contra qtd_h2h global, o que era o mesmo,
  mas agora a diferenca eh que wr_ult{N}_qtd pode ser MENOR que qtd_h2h se
  jogos < N. Mas como min_partidas eh o piso aceitavel, isso permite
  "WR ult20, min=10" passar com 12 jogos.

v4 - Le filtrosHistAdicionados (formato antigo) alem de filtrosCompAdicionados:
- _normalizar_filtros_hist converte formato antigo {base, janela:"last_N", prob:[min,max]} pro novo
- _extrair_janelas_dos_filtros agora le dos 2 lugares
- _aplicar_filtros_complementares aceita filtros normalizados de ambas fontes
- Filtros hist com base=individual ou tipo!=all sao rejeitados (nao suportado)

v3 - Janelas H2H dinamicas:
- _calcular_stats_h2h aceita lista de janelas (qualquer N de 3-100)
- _aplicar_filtros_complementares usa janela exata do filtro em vez de mapeamento fixo
- Mantem janelas padrao 5/10/15/20 sempre presentes pra compatibilidade

v2 - Adiciona filtros estatisticos H2H:
- WR ult5/10/15, Media ult5/10/20, Gap, Tendencia, DIFF, Cenario
"""

from datetime import date, datetime, timedelta
from typing import Any, Optional
from decimal import Decimal
import json
import logging
import asyncio

from database import get_pool

logger = logging.getLogger(__name__)


ESPORTE_UI_PARA_BANCO = {
    'fifa':    'E-Football',
    'nba2k':   'E-Basketball',
    'ehockey': 'E-Hockey',
    'etennis': 'E-Tennis',
}


MERCADO_TIPOS_POR_CASA = {
    'betano': {
        'over_under_ft':        ['13', '157'],
        'asian_over_under_ft':  ['189'],
        'ml_ft':                ['1'],
        'btts_ft':              ['15'],
        'over_under_ft_player': ['84', '85'],
        'over_under_ht':        ['83'],
        'ml_ht':                ['60'],
    },
    'estrelabet': {
        'over_under_ft':        ['18'],
        'ml_ft':                ['1'],
        'btts_ft':              ['29'],
        'ah_ft':                ['16'],
        'over_under_ht':        ['68'],
        'ml_ht':                ['60'],
        'ah_ht':                ['66'],
        'double_chance_ft':     ['10'],
    },
    'superbet': {
        'over_under_ft':        ['OVER_UNDER'],
        'asian_over_under_ft':  ['OVER_UNDER'],
        'ml_ft':                ['MATCH_RESULT'],
        'btts_ft':              ['BTTS'],
        'ah_ft':                ['HANDICAP'],
        'correct_score':        ['CORRECT_SCORE'],
        'over_under_ht':        ['PERIOD_TOTAL'],
        'ml_ht':                ['PERIOD_RESULT'],
        'double_chance_ft':     ['DOUBLE_CHANCE'],
        'odd_even':             ['ODD_EVEN'],
    },
    'bet365': {
        'over_under_ft':        ['1450'],
        'ah_ft':                ['1446'],
        'ml_ft':                ['180032'],
    },
}


MERCADO_KEYWORDS = {
    'over_under_ft': [
        'total de gols', 'total - jogo', 'total - partida',
        'total de pontos', 'total ', 'over/under',
    ],
    'over_under_ht': [
        '1° tempo - total', '1¬ tempo - total', '1║ tempo - total',
        'primeiro tempo - total', '1st half - total', 'total ht',
    ],
    'asian_over_under_ft': [
        'asiatico (mais/menos)', 'total de gols asiatico', 'asian total',
    ],
    'asian_over_under_ht': ['asiatico - 1', 'asian total - 1'],
    'ah_ft': [
        'handicap asiatico', 'asian handicap', 'handicap (incl', 'handicap',
    ],
    'ah_ht': ['handicap - 1', '1║ tempo - handicap'],
    'eh_ft': ['handicap europeu', 'european handicap', 'handicap 3-way'],
    'over_under_ft_player': ['(esports) - total de gols', 'jogador - total', 'pontos jogador'],
    'over_under_ht_player': ['(esports) - total - 1'],
    'ml_ft': [
        'resultado final', 'resultado (1x2)', '1x2',
        'match winner', 'vencedor', 'para ganhar',
    ],
    'ml_ht': ['resultado - 1', '1║ tempo - resultado', 'half time', '1x2 ht'],
    'btts_ft': [
        'ambas equipes marcam', 'ambas as equipes marcam',
        'ambos marcam', 'both teams to score', 'btts',
    ],
    'double_chance_ft': ['chance dupla', 'dupla chance', 'double chance'],
    'odd_even': ['par/impar', 'impar/par', 'odd/even'],
    'correct_score': ['resultado correto', 'correct score', 'placar correto'],
}


def _matches_mercado(mercado_bot: str, tick_mercado: str, tick_mercado_tipo: str, casa: str = '') -> bool:
    if not mercado_bot:
        return True

    casa_lower = (casa or '').lower()
    mapping_casa = MERCADO_TIPOS_POR_CASA.get(casa_lower, {})

    if mercado_bot in mapping_casa:
        tipos_validos = mapping_casa[mercado_bot]
        if tick_mercado_tipo not in tipos_validos:
            return False
        if casa_lower == 'betano' and mercado_bot == 'over_under_ft':
            mercado_lower = (tick_mercado or '').lower()
            if '(esports)' in mercado_lower:
                return False
        return True

    keywords = MERCADO_KEYWORDS.get(mercado_bot, [])
    if not keywords:
        return True
    haystack = (
        (tick_mercado or '').lower() + ' ' +
        (tick_mercado_tipo or '').lower()
    )
    return any(kw in haystack for kw in keywords)


def _parse_linha(linha_text: str) -> Optional[float]:
    if linha_text is None or linha_text == '':
        return None
    try:
        s = str(linha_text).strip()
        if '|' in s:
            s = s.split('|')[0]
        if s.startswith('+'):
            s = s[1:]
        return float(s)
    except (ValueError, TypeError):
        return None


def _normalizar(s: str) -> str:
    if s is None:
        return ''
    s = str(s).lower().strip()
    repl = {'á':'a','à':'a','ã':'a','â':'a','é':'e','ê':'e','í':'i','ó':'o','ô':'o','õ':'o','ú':'u','ç':'c'}
    for old, new in repl.items():
        s = s.replace(old, new)
    return s


def _resolve_resultado(mercado: str, selecao: str, linha: float,
                       score_home: int, score_away: int) -> Optional[str]:
    if score_home is None or score_away is None:
        return None
    mercados_com_linha = ('over_under_ft', 'over_under_ht', 'asian_over_under_ft',
                          'asian_over_under_ht', 'ah_ft', 'ah_ht', 'eh_ft',
                          'over_under_ft_player', 'over_under_ht_player')
    if mercado in mercados_com_linha and linha is None:
        return None

    sel = _normalizar(selecao)

    if mercado in ('over_under_ft', 'asian_over_under_ft', 'over_under_ht', 'asian_over_under_ht'):
        if mercado in ('over_under_ht', 'asian_over_under_ht'):
            return None

        total = score_home + score_away
        is_under = 'menos' in sel or 'under' in sel or 'abaixo' in sel or sel == 'under'
        is_over  = (not is_under) and ('mais' in sel or 'over' in sel or 'acima' in sel or sel == 'over')

        if is_over:
            if total > linha: return 'green'
            elif total < linha: return 'red'
            else: return 'void'
        elif is_under:
            if total < linha: return 'green'
            elif total > linha: return 'red'
            else: return 'void'
        return None

    if mercado == 'ah_ft':
        if 'home' in sel or 'casa' in sel or 'time a' in sel or '1' == sel:
            ajuste = score_home + linha - score_away
            if ajuste > 0: return 'green'
            elif ajuste < 0: return 'red'
            else: return 'void'
        elif 'away' in sel or 'visitante' in sel or 'fora' in sel or 'time b' in sel or '2' == sel:
            ajuste = score_away + linha - score_home
            if ajuste > 0: return 'green'
            elif ajuste < 0: return 'red'
            else: return 'void'
        return None

    if mercado == 'ml_ft':
        if score_home > score_away:
            vencedor = 'home'
        elif score_away > score_home:
            vencedor = 'away'
        else:
            vencedor = 'draw'
        if sel in ('1', 'home', 'casa') or 'home' in sel or 'casa' in sel:
            return 'green' if vencedor == 'home' else 'red'
        elif sel in ('2', 'away', 'fora') or 'away' in sel or 'visitante' in sel or 'fora' in sel:
            return 'green' if vencedor == 'away' else 'red'
        elif sel in ('x', 'draw', 'empate') or 'draw' in sel or 'empate' in sel:
            return 'green' if vencedor == 'draw' else 'red'
        return None

    if mercado == 'btts_ft':
        ambos_marcaram = score_home > 0 and score_away > 0
        is_sim = sel in ('sim', 'yes', '1') or 'sim' in sel or 'yes' in sel
        is_nao = sel in ('nao', 'no', '2') or 'nao' in sel or 'no' in sel or 'nπo' in sel
        if is_sim:
            return 'green' if ambos_marcaram else 'red'
        elif is_nao:
            return 'green' if not ambos_marcaram else 'red'
        return None

    return None


# ============================================================
# H2H CACHE
# ============================================================

class H2HCache:
    LIMITE_JOGOS_POR_PAR = 100      # janela padrao/maxima dos filtros normais
    TETO_BUSCA = 5000               # teto absoluto do _buscar (suporta 'todas')

    def __init__(self, pool, casa: str, esporte_banco: str):
        self._pool = pool
        self._casa = casa
        self._esporte = esporte_banco
        self._cache: dict = {}

    @staticmethod
    def _normalizar_par(ja: str, jb: str) -> tuple:
        a = (ja or '').strip()
        b = (jb or '').strip()
        return tuple(sorted([a, b]))

    # v7 FIX (18/06/2026): margem ao-vivo. Se o ultimo tick de um jogo
    # (vindo de 'ticks') foi ha menos de MARGEM_AO_VIVO_MIN minutos ANTES
    # do tick avaliado, o jogo ainda estava AO VIVO (placar parcial) e NAO
    # deve contar no WR. Jogos de h2h_historico (placar final) nunca filtram.
    MARGEM_AO_VIVO_MIN = 15

    async def get_jogos(self, ja: str, jb: str, antes_de_ts, event_id_excluir=None) -> list:
        """
        Retorna jogos do par com ts < antes_de_ts.

        v5: aceita event_id_excluir pra remover o jogo atual da lista H2H
        (evita contar o placar parcial do jogo em andamento no calculo do WR).
        v7 (FIX): exclui jogos que vieram de TICKS e ainda estavam ao vivo
        no momento da aposta (ultimo tick < MARGEM_AO_VIVO_MIN antes de
        antes_de_ts). Esses tem placar PARCIAL e poluiam o WR. Jogos de
        h2h_historico (placar final real da TM/CSV) nunca sao filtrados.
        Usa antes_de_ts (nao NOW()) -> funciona igual em backtest e ao vivo.
        """
        par = self._normalizar_par(ja, jb)
        if not par[0] or not par[1]:
            return []

        if par not in self._cache:
            self._cache[par] = await self._buscar(par[0], par[1])

        corte_ao_vivo = antes_de_ts - timedelta(minutes=self.MARGEM_AO_VIVO_MIN)

        jogos = []
        for j in self._cache[par]:
            if j['ts'] >= antes_de_ts:
                continue
            # v7: se o jogo veio de ticks e o ultimo tick dele foi recente
            # demais (ainda ao vivo no momento da aposta), descarta.
            if j.get('fonte') == 'tick':
                ult = j.get('ultimo_tick_ts') or j['ts']
                if ult >= corte_ao_vivo:
                    continue
            jogos.append(j)

        if event_id_excluir is not None:
            eid_str = str(event_id_excluir)
            jogos = [j for j in jogos if str(j.get('event_id')) != eid_str]
        return jogos

    async def _buscar(self, j1: str, j2: str) -> list:
        # v7: o lado ticks traz tambem o ts do ultimo tick (ultimo_tick_ts)
        # e marca fonte='tick' vs fonte='hist', pra get_jogos saber se o jogo
        # estava ao vivo no momento da aposta e filtrar so os de tick.
        sql = """
        SELECT event_id, ts, jogador_a, jogador_b, score_home, score_away,
               ultimo_tick_ts, fonte
        FROM (
            SELECT event_id, ts, jogador_a, jogador_b, score_home, score_away,
                   ts AS ultimo_tick_ts, 'tick' AS fonte
            FROM (
                SELECT DISTINCT ON (event_id)
                    event_id, ts, jogador_a, jogador_b, score_home, score_away
                FROM ticks
                WHERE bookmaker = $1
                  AND sport = $2
                  AND ((jogador_a = $3 AND jogador_b = $4)
                    OR (jogador_a = $4 AND jogador_b = $3))
                  AND score_home IS NOT NULL
                  AND score_away IS NOT NULL
                ORDER BY event_id, ts DESC
            ) ticks_distinct

            UNION ALL

            SELECT event_id, ts, jogador_a, jogador_b, score_home, score_away,
                   NULL::timestamptz AS ultimo_tick_ts, 'hist' AS fonte
            FROM h2h_historico
            WHERE sport = $2
              AND ((UPPER(jogador_a) = UPPER($3) AND UPPER(jogador_b) = UPPER($4))
                OR (UPPER(jogador_a) = UPPER($4) AND UPPER(jogador_b) = UPPER($3)))
              AND score_home IS NOT NULL
              AND score_away IS NOT NULL
        ) combinado
        ORDER BY ts DESC
        LIMIT $5
        """
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    sql, self._casa, self._esporte, j1, j2, self.TETO_BUSCA
                )
        except Exception as e:
            logger.exception(f"[h2h] Erro buscando par ({j1}, {j2}): {e}")
            return []

        jogos = []
        for r in rows:
            sh = r['score_home']
            sa = r['score_away']
            jogos.append({
                'event_id': r['event_id'],
                'ts': r['ts'],
                'jogador_a': r['jogador_a'],
                'jogador_b': r['jogador_b'],
                'score_home': sh,
                'score_away': sa,
                'total': (sh or 0) + (sa or 0),
                'fonte': r['fonte'],
                'ultimo_tick_ts': r['ultimo_tick_ts'],
            })

        # v8: dedup ENTRE fontes (tick x hist). O mesmo jogo pode estar no
        # ticks (ts = quando o coletor capturou) E no h2h_historico (ts = horario
        # oficial da TM), com lag de ~20-40min e placar possivelmente invertido
        # (perspectiva A/B trocada). Sem isso, o jogo conta 2x e infla a amostra.
        # Criterio: mesmo PLACAR NORMALIZADO (ordenado, pega inversao) dentro de
        # uma janela de tempo, e SO entre fontes diferentes (tick vs hist).
        # Mantem o do historico (placar oficial TM) e descarta o do tick.
        # Jogos da MESMA fonte com mesmo placar ficam preservados (jogos reais
        # distintos) - mesma logica da limpeza do banco.
        JANELA_DEDUP_MIN = 45  # lag tipico tick<->TM
        jogos.sort(key=lambda x: x['ts'])  # mais antigo primeiro
        manter = []
        for jg in jogos:
            placar_norm = tuple(sorted([jg['score_home'] or 0, jg['score_away'] or 0]))
            achou = None
            for m in manter:
                if m.get('_descartado'):
                    continue
                if m['fonte'] == jg['fonte']:
                    continue  # so dedup entre fontes diferentes
                m_norm = tuple(sorted([m['score_home'] or 0, m['score_away'] or 0]))
                if m_norm != placar_norm:
                    continue
                dt = abs((jg['ts'] - m['ts']).total_seconds()) / 60.0
                if dt <= JANELA_DEDUP_MIN:
                    achou = m
                    break
            if achou is not None:
                # mesmo jogo na outra fonte: mantem o 'hist', descarta o 'tick'
                if achou['fonte'] == 'hist':
                    jg['_descartado'] = True
                else:
                    achou['_descartado'] = True
                    jg['_descartado'] = False
                manter.append(jg)
            else:
                jg['_descartado'] = False
                manter.append(jg)

        jogos = [j for j in manter if not j.get('_descartado')]
        for j in jogos:
            j.pop('_descartado', None)
        jogos.sort(key=lambda x: x['ts'], reverse=True)
        return jogos

    @property
    def stats_cache(self):
        return {
            'pares_carregados': len(self._cache),
            'jogos_total': sum(len(v) for v in self._cache.values()),
        }


# ============================================================
# NORMALIZACAO E EXTRAÇAO DE FILTROS (v4)
# ============================================================

def _normalizar_filtros_hist(filtros_hist: list) -> list:
    """
    Converte filtrosHistAdicionados (formato antigo) pro mesmo formato
    do filtrosCompAdicionados, pra unificar o processamento.

    Formato origem (filtrosHistAdicionados):
      {
        "base": "match" | "individual",
        "janela": "last_10",
        "prob": [70, 100],
        "tipo": "all" | "same_grade" | "specific_teams",
        "versao": "all",
        "minPartidas": 10
      }

    Formato destino:
      {
        "tipo": "wr",
        "janela": 10,
        "min": 0.7,           # 70/100 (decimal)
        "max": 1.0,           # 100/100 (decimal)
        "minAtivo": True,
        "maxAtivo": False,    # max=1.0 (100%) eh "nao filtrado"
        "hist_base": "match",
        "hist_tipo": "all",
        "hist_min_partidas": 10,
        "_origem": "hist",
      }
    """
    normalizados = []
    for fh in filtros_hist or []:
        if not isinstance(fh, dict):
            continue

        # Parse janela. Aceita:
        #   'all'      -> 0 (TODAS, usa todo o historico do par)
        #   'last_0'   -> 0 (TODAS, alias)
        #   'last_N'   -> N
        # Janelas de tempo (last_1h, last_7d, current_championship, same_day)
        # NAO sao suportadas pelo H2H simples -> descartadas.
        janela_str = str(fh.get('janela', '')).strip().lower()
        janela_num = None
        if janela_str == 'all':
            janela_num = 0
        elif janela_str.startswith('last_'):
            resto = janela_str.replace('last_', '')
            try:
                janela_num = int(resto)   # so converte se for numero puro (rejeita 1h, 7d)
            except ValueError:
                pass
        # aceita 0 (TODAS) explicitamente; rejeita None ou negativo
        if janela_num is None or janela_num < 0:
            continue

        # prob: [min, max] em % (0-100)
        prob = fh.get('prob') or [0, 100]
        if not isinstance(prob, list) or len(prob) < 2:
            prob = [0, 100]
        prob_min = float(prob[0]) if prob[0] is not None else 0
        prob_max = float(prob[1]) if prob[1] is not None else 100

        # Converte % (0-100) pra decimal (0.0-1.0) usado nos stats
        min_v = prob_min / 100.0
        max_v = prob_max / 100.0

        # Min/Max so "ativos" se nao forem o extremo (0% ou 100% = sem filtro real)
        min_ativo = prob_min > 0
        max_ativo = prob_max < 100

        normalizados.append({
            'tipo': 'wr',
            'janela': janela_num,
            'min': min_v,
            'max': max_v,
            'minAtivo': min_ativo,
            'maxAtivo': max_ativo,
            'hist_base': fh.get('base', 'match'),
            'hist_tipo': fh.get('tipo', 'all'),
            'hist_min_partidas': fh.get('minPartidas'),
            '_origem': 'hist',
        })

    return normalizados


def _coletar_todos_filtros(filtros: dict) -> list:
    """
    Pega filtros dos 2 lugares e retorna lista unificada.
    Filtros hist com base!=match ou tipo!=all sao SKIPADOS (nao suportado ainda
    pelo backend de H2H simples - exigiria sub-query por liga/jogador/times).

    Retorna sempre lista (pode ser vazia).
    """
    filtros_comp = filtros.get('filtrosCompAdicionados') or []
    filtros_hist = filtros.get('filtrosHistAdicionados') or []
    filtros_hist_norm = _normalizar_filtros_hist(filtros_hist)

    # Skipa filtros hist nao suportados (base=individual ou tipo!=all)
    # Eles nao geram rejeicao silenciosa - sao tratados como "nao calculado"
    # mas o bot DEVE rejeitar o tick? Decisao: nao aplica = nao filtra
    # (igual ao comportamento atual antes do v4)
    filtros_hist_suportados = []
    for f in filtros_hist_norm:
        base = f.get('hist_base')
        tipo = f.get('hist_tipo')
        if base == 'match' and tipo == 'all':
            filtros_hist_suportados.append(f)
        else:
            # Loga warning - filtro sera ignorado pelo executor
            logger.warning(
                f"FiltroHist nao suportado ignorado: base={base}, tipo={tipo}, "
                f"janela={f.get('janela')}"
            )

    return list(filtros_comp) + filtros_hist_suportados


# ============================================================
# CALCULO DE STATS H2H
# ============================================================

MIN_H2H_DEFAULT = 5

# Janelas padrao SEMPRE calculadas (compatibilidade backwards)
JANELAS_PADRAO_WR = (5, 10, 15)
JANELAS_PADRAO_MEDIA = (5, 10, 20)


def _extrair_janelas_dos_filtros(filtros_unificados: list) -> tuple[set, set]:
    """
    Extrai janelas customizadas (alem das padrao) dos filtros unificados.
    Retorna (janelas_wr, janelas_media).
    Recebe lista JA UNIFICADA (saida de _coletar_todos_filtros).
    """
    janelas_wr = set(JANELAS_PADRAO_WR)
    janelas_media = set(JANELAS_PADRAO_MEDIA)

    if not filtros_unificados:
        return janelas_wr, janelas_media

    for f in filtros_unificados:
        if not isinstance(f, dict):
            continue
        tipo = (f.get('tipo') or '').lower().strip()
        janela = f.get('janela')
        if janela is None:
            continue
        try:
            j = int(janela)
        except (TypeError, ValueError):
            continue
        # janela 0 = 'TODAS' (usa todo o historico do par). Sentinela aceito.
        # Qualquer j entre 1 e TETO_BUSCA tambem e aceito (antes limitava a 100).
        if j != 0 and (j < 1 or j > H2HCache.TETO_BUSCA):
            continue

        if tipo == 'wr':
            janelas_wr.add(j)
        elif tipo == 'media':
            janelas_media.add(j)

    return janelas_wr, janelas_media


def _calcular_stats_h2h(jogos: list, linha_atual: float,
                        janelas_wr: Optional[set] = None,
                        janelas_media: Optional[set] = None) -> dict:
    """
    Calcula stats H2H com janelas dinamicas.

    v5: usa MIN(qtd, N) jogos quando qtd < N (em vez de retornar None).
    Tambem grava 'wr_ult{N}_qtd' e 'media_ult{N}_qtd' indicando quantos jogos
    foram realmente usados, pra que _aplicar_filtros_complementares possa
    validar contra min_partidas do filtro.
    """
    qtd = len(jogos)

    if janelas_wr is None:
        janelas_wr = set(JANELAS_PADRAO_WR)
    if janelas_media is None:
        janelas_media = set(JANELAS_PADRAO_MEDIA)

    def wr(n):
        # v5: usa o que tiver (ate N). Se nao tem nada, retorna None.
        # n==0 = 'TODAS' -> usa todos os jogos disponiveis do par.
        if qtd <= 0:
            return None, 0
        usar = qtd if n == 0 else min(qtd, n)
        slice_ = jogos[:usar]
        passou = sum(1 for j in slice_ if j['total'] > linha_atual)
        return passou / usar, usar

    def media(n):
        if qtd <= 0:
            return None, 0
        usar = qtd if n == 0 else min(qtd, n)
        slice_ = jogos[:usar]
        return sum(j['total'] for j in slice_) / usar, usar

    out: dict = {'qtd_h2h': qtd}

    for n in janelas_wr:
        v, usados = wr(n)
        out[f'wr_ult{n}'] = v
        out[f'wr_ult{n}_qtd'] = usados
    for n in janelas_media:
        v, usados = media(n)
        out[f'media_ult{n}'] = v
        out[f'media_ult{n}_qtd'] = usados

    # Gap = media_ult20 - linha
    m20 = out.get('media_ult20')
    out['gap'] = (m20 - linha_atual) if m20 is not None else None

    # Tendencia = media_ult5 - media_ult20
    m5 = out.get('media_ult5')
    out['tendencia'] = (m5 - m20) if (m5 is not None and m20 is not None) else None

    return out


def _aplicar_filtros_complementares(stats: dict, filtros_unificados: list, min_h2h: int = MIN_H2H_DEFAULT) -> tuple[bool, str]:
    """
    Aplica filtros unificados (comp + hist normalizado).
    Pre-condicao: _calcular_stats_h2h foi chamado COM as janelas dos filtros.

    v6 FIX (14/05/2026): filtros hist (origem='hist', vindos do
    filtrosHistAdicionados) usam minPartidas como MATURIDADE DA
    AMOSTRA H2H TOTAL (qtd_h2h), nao como tamanho efetivo usado
    na janela.

    Exemplo de uso pretendido pelo usuario:
      janela=last_5, minPartidas=10
      Significado: "analiso WR nas ultimas 5 partidas, MAS so
      processo se o par tem >=10 partidas no historico total"
      (filtro de maturidade — evita pares com pouco historico).

    Antes (v5): validava qtd_validar contra wr_ult{N}_qtd
    (max=N), o que tornava "janela=5 + min=10" matematicamente
    impossivel (qtd_validar maximo=5 < 10 sempre). Bot 17 com
    filtros [last_10/min=10, last_5/min=10] rejeitava 100% dos
    ticks pelo segundo filtro.

    Comportamento agora:
      - origem='hist': valida contra qtd_h2h GLOBAL.
        Ex: last_5+min=10 passa se par tem >=10 jogos no historico.
      - origem='comp' (padrao se nao tiver _origem): mantem
        comportamento v5 (qtd da janela). Filtros comp usam
        min_h2h default=5 que sempre <= janela, entao nao geram
        contradicao matematica.
    """
    if not filtros_unificados:
        return True, ''

    qtd_global = stats.get('qtd_h2h', 0)

    for f in filtros_unificados:
        tipo = (f.get('tipo') or '').lower().strip()
        janela = f.get('janela')
        min_v = f.get('min') if f.get('minAtivo') else None
        max_v = f.get('max') if f.get('maxAtivo') else None
        origem = f.get('_origem', 'comp')

        # Filtros hist tem min_partidas proprio; senao usa default
        min_partidas = f.get('hist_min_partidas') or min_h2h
        try:
            min_partidas = int(min_partidas)
        except (TypeError, ValueError):
            min_partidas = min_h2h

        # v6 FIX: filtros HIST validam minPartidas contra qtd_h2h GLOBAL
        # (= maturidade do par). Filtros COMP continuam validando
        # contra qtd da janela (compat backwards v5).
        if origem == 'hist':
            qtd_validar = qtd_global
        else:
            # v5 (comp): qtd a validar depende do tipo+janela do filtro
            qtd_validar = qtd_global
            if tipo == 'wr' and janela is not None:
                try:
                    qtd_validar = stats.get(f'wr_ult{int(janela)}_qtd', qtd_global) or 0
                except (TypeError, ValueError):
                    pass
            elif tipo == 'media' and janela is not None:
                try:
                    qtd_validar = stats.get(f'media_ult{int(janela)}_qtd', qtd_global) or 0
                except (TypeError, ValueError):
                    pass

        if qtd_validar < min_partidas:
            return False, f'h2h_insuficiente_qtd_{qtd_validar}_min_{min_partidas}'

        valor = None

        if tipo == 'media' and janela is not None:
            try:
                valor = stats.get(f'media_ult{int(janela)}')
            except (TypeError, ValueError):
                valor = None
        elif tipo == 'wr' and janela is not None:
            try:
                valor = stats.get(f'wr_ult{int(janela)}')
            except (TypeError, ValueError):
                valor = None
        elif tipo in ('gap_media', 'gap'):
            valor = stats.get('gap')
        elif tipo == 'tendencia':
            valor = stats.get('tendencia')
        elif tipo == 'gap_linha':
            media = stats.get('media_ult20')
            if media is not None:
                valor = abs(media - (stats.get('linha_atual') or 0))
        elif tipo == 'qtd_h2h':
            valor = stats.get('qtd_h2h')

        if valor is None:
            return False, f'stat_{tipo}_ult{janela}_indisponivel'

        if min_v is not None and valor < float(min_v):
            return False, f'{tipo}_ult{janela}_lt_min'
        if max_v is not None and valor > float(max_v):
            return False, f'{tipo}_ult{janela}_gt_max'

    return True, ''


def _aplicar_filtro_cenario(tick: dict, cenario: str) -> bool:
    sh = tick.get('score_home')
    sa = tick.get('score_away')
    if sh is None or sa is None:
        return False

    if cenario == 'casa_vencendo':
        return sh > sa
    if cenario == 'casa_perdendo':
        return sh < sa
    if cenario == 'empate':
        return sh == sa
    if cenario == 'casa_ou_empate':
        return sh >= sa
    if cenario == 'visitante_ou_empate':
        return sa >= sh
    if cenario == 'casa_ou_visitante':
        return sh != sa
    return True


def _aplicar_filtro_diff_placar(tick: dict, diff_min: int) -> bool:
    sh = tick.get('score_home')
    sa = tick.get('score_away')
    if sh is None or sa is None:
        return False
    return abs(sh - sa) >= diff_min


def _avaliar_filtros_basicos(tick: dict, bot: dict) -> tuple[bool, str]:
    linha = _parse_linha(tick.get('linha'))
    if linha is None:
        return False, 'linha invalida'

    if bot.get('linha_min') is not None and linha < float(bot['linha_min']):
        return False, f'linha {linha} < min'
    if bot.get('linha_max') is not None and linha > float(bot['linha_max']):
        return False, f'linha {linha} > max'

    odd = tick.get('odds')
    if odd is None:
        return False, 'odd ausente'
    if bot.get('odd_min') is not None and odd < float(bot['odd_min']):
        return False, f'odd {odd} < min'
    if bot.get('odd_max') is not None and odd > float(bot['odd_max']):
        return False, f'odd {odd} > max'

    if not _matches_mercado(bot.get('mercado', ''), tick.get('mercado', ''), tick.get('mercado_tipo', ''), bot.get('casa', '')):
        return False, 'mercado nao bate'

    blacklist_pares = bot.get('blacklist_pares') or []
    if blacklist_pares:
        ja = (tick.get('jogador_a') or '').lower()
        jb = (tick.get('jogador_b') or '').lower()
        ta = (tick.get('time_a') or '').lower()
        tb = (tick.get('time_b') or '').lower()
        for entry in blacklist_pares:
            j1 = (entry.get('j1') or '').lower()
            j2 = (entry.get('j2') or '').lower()
            t1 = (entry.get('t1') or '').lower()
            t2 = (entry.get('t2') or '').lower()
            if j1 and (j1 == ja or j1 == jb): return False, f'blacklist {j1}'
            if j2 and (j2 == ja or j2 == jb): return False, f'blacklist {j2}'
            if t1 and (t1 == ta or t1 == tb): return False, f'blacklist time {t1}'
            if t2 and (t2 == ta or t2 == tb): return False, f'blacklist time {t2}'

    whitelist_pares = bot.get('whitelist_pares') or []
    if whitelist_pares:
        ja = (tick.get('jogador_a') or '').lower()
        jb = (tick.get('jogador_b') or '').lower()
        ta = (tick.get('time_a') or '').lower()
        tb = (tick.get('time_b') or '').lower()
        match = False
        for entry in whitelist_pares:
            j1 = (entry.get('j1') or '').lower()
            j2 = (entry.get('j2') or '').lower()
            t1 = (entry.get('t1') or '').lower()
            t2 = (entry.get('t2') or '').lower()
            j1_ok = not j1 or (j1 == ja or j1 == jb)
            j2_ok = not j2 or (j2 == ja or j2 == jb)
            t1_ok = not t1 or (t1 == ta or t1 == tb)
            t2_ok = not t2 or (t2 == ta or t2 == tb)
            if (j1 or j2 or t1 or t2) and j1_ok and j2_ok and t1_ok and t2_ok:
                match = True
                break
        if not match:
            return False, 'fora da whitelist'

    return True, ''


# ============================================================
# WORKER PRINCIPAL
# ============================================================

async def executar_backtest(job_id: int):
    pool = get_pool()
    logger.info(f"[backtest] Iniciando job {job_id}")

    try:
        async with pool.acquire() as conn:
            job_row = await conn.fetchrow(
                "SELECT * FROM backtest_jobs WHERE id = $1", job_id
            )
            if not job_row:
                logger.error(f"[backtest] Job {job_id} nao encontrado")
                return

            bot = job_row['bot_snapshot']
            if isinstance(bot, str):
                bot = json.loads(bot)

            data_inicio = job_row['data_inicio']
            data_fim = job_row['data_fim']
            stake_modo = job_row['stake_modo']
            stake_valor = float(job_row['stake_valor'])
            banca_inicial = float(job_row['banca_inicial'] or 1000)

            await conn.execute(
                "UPDATE backtest_jobs SET status='rodando', progresso=5, "
                "progresso_msg='Buscando ticks no periodo' WHERE id=$1",
                job_id,
            )

        filtros = bot.get('filtros') or {}
        cenario_ativo = filtros.get('cenarioPartidaAtivo', False)
        cenario_partida = filtros.get('cenarioPartida') if cenario_ativo else None
        diff_ativo = filtros.get('diferencaPlacarAtivo', False)
        diff_min = filtros.get('diferencaPlacar', 0) if diff_ativo else 0

        # v4: coleta filtros dos 2 lugares (comp + hist normalizado)
        filtros_unificados = _coletar_todos_filtros(filtros)
        janelas_wr, janelas_media = _extrair_janelas_dos_filtros(filtros_unificados)

        if filtros_unificados:
            tipos_resumo = [f"{f.get('tipo')}_ult{f.get('janela')}" for f in filtros_unificados]
            logger.info(f"[backtest] Filtros unificados: {tipos_resumo}")

        async with pool.acquire() as conn:
            torneios = bot.get('torneios') or []
            torneios_excluir = bot.get('torneios_excluir') or []

            sql = """
                SELECT id, ts, bookmaker, sport, liga, event_id,
                       jogador_a, jogador_b, time_a, time_b,
                       score_home, score_away, live_time,
                       mercado, mercado_id, mercado_tipo, linha, selecao, selecao_id,
                       odds
                FROM ticks
                WHERE bookmaker = $1
                  AND ts >= $2::timestamp
                  AND ts < $3::timestamp + INTERVAL '1 day'
            """
            params: list = [bot.get('casa'), data_inicio, data_fim]
            n = 4

            if bot.get('esporte'):
                sport_banco = ESPORTE_UI_PARA_BANCO.get(bot['esporte'], bot['esporte'])
                sql += f" AND sport = ${n}"
                params.append(sport_banco)
                n += 1

            if torneios:
                ors = []
                for t in torneios:
                    ors.append(f"liga ILIKE ${n}")
                    params.append(f"%{t}%")
                    n += 1
                sql += f" AND ({' OR '.join(ors)})"

            if torneios_excluir:
                ands = []
                for t in torneios_excluir:
                    ands.append(f"liga NOT ILIKE ${n}")
                    params.append(f"%{t}%")
                    n += 1
                sql += f" AND ({' AND '.join(ands)})"

            sql += " ORDER BY event_id, mercado_id, linha, selecao_id, ts ASC"
            logger.info(f"[backtest] SQL params count: {len(params)}")

            ticks = await conn.fetch(sql, *params)
            total_ticks = len(ticks)
            logger.info(f"[backtest] Job {job_id}: {total_ticks} ticks brutos")

            await conn.execute(
                "UPDATE backtest_jobs SET progresso=15, "
                "progresso_msg=$2, total_ticks_avaliados=$3 WHERE id=$1",
                job_id, f"Encontrados {total_ticks} ticks. Aplicando filtros...", total_ticks,
            )

            if total_ticks == 0:
                await conn.execute(
                    """
                    UPDATE backtest_jobs SET
                        status='concluido', progresso=100,
                        progresso_msg='Nenhum tick no periodo',
                        total_apostas=0, green=0, red=0, void_count=0,
                        pnl=0, roi=0, win_rate=0, drawdown_max=0, max_streak_red=0,
                        dias_verdes=0, dias_total=0,
                        equity_curve='[]'::jsonb, apostas_detalhe='[]'::jsonb, pnl_por_dia='[]'::jsonb,
                        concluido_em=NOW()
                    WHERE id=$1
                    """,
                    job_id,
                )
                return

        primeiros: dict = {}
        placar_final: dict = {}

        for t in ticks:
            evt = t['event_id']
            sh = t['score_home']
            sa = t['score_away']
            if sh is not None and sa is not None:
                placar_final[evt] = (sh, sa)

            chave = (t['event_id'], t['mercado_id'] or '', t['linha'] or '', t['selecao_id'] or '')
            if chave not in primeiros:
                primeiros[chave] = dict(t)

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE backtest_jobs SET progresso=30, progresso_msg='Calculando stats H2H' WHERE id=$1",
                job_id,
            )

        sport_banco = ESPORTE_UI_PARA_BANCO.get(bot.get('esporte', ''), bot.get('esporte', ''))
        h2h_cache = H2HCache(pool, bot.get('casa', ''), sport_banco)

        max_apostas_partida = bot.get('max_apostas_partida')
        apostas_por_evento: dict = {}
        candidatas = []

        rej = {
            'cap_jogo': 0, 'basico': 0, 'cenario': 0, 'diff': 0,
            'h2h_insuf': 0, 'comp': 0, 'sem_par': 0,
            'sem_placar': 0, 'sem_resultado': 0,
        }

        ticks_ordenados = sorted(primeiros.values(), key=lambda x: x['ts'])
        total_candidatos = len(ticks_ordenados)

        for i, tick in enumerate(ticks_ordenados):
            if i > 0 and i % 200 == 0:
                pct = 30 + int(40 * i / total_candidatos)
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE backtest_jobs SET progresso=$1, progresso_msg=$2 WHERE id=$3",
                        pct, f"Aplicando filtros ({i}/{total_candidatos})", job_id,
                    )

            evt = tick['event_id']

            if max_apostas_partida is not None and apostas_por_evento.get(evt, 0) >= max_apostas_partida:
                rej['cap_jogo'] += 1
                continue

            passou, _ = _avaliar_filtros_basicos(tick, bot)
            if not passou:
                rej['basico'] += 1
                continue

            if cenario_partida:
                if not _aplicar_filtro_cenario(tick, cenario_partida):
                    rej['cenario'] += 1
                    continue

            if diff_ativo and diff_min > 0:
                if not _aplicar_filtro_diff_placar(tick, diff_min):
                    rej['diff'] += 1
                    continue

            # v4: aplica filtros unificados (comp + hist normalizado)
            stats = None
            if filtros_unificados:
                ja = tick.get('jogador_a')
                jb = tick.get('jogador_b')
                if not ja or not jb:
                    rej['sem_par'] += 1
                    continue

                jogos_h2h = await h2h_cache.get_jogos(ja, jb, tick['ts'], event_id_excluir=tick.get('event_id'))
                linha_num = _parse_linha(tick.get('linha')) or 0
                stats = _calcular_stats_h2h(jogos_h2h, linha_num, janelas_wr, janelas_media)
                stats['linha_atual'] = linha_num

                passou_comp, motivo = _aplicar_filtros_complementares(stats, filtros_unificados)
                if not passou_comp:
                    if 'h2h_insuficiente' in motivo:
                        rej['h2h_insuf'] += 1
                    else:
                        rej['comp'] += 1
                    continue

            placar = placar_final.get(evt)
            if not placar:
                rej['sem_placar'] += 1
                continue
            score_home, score_away = placar

            linha_num = _parse_linha(tick.get('linha'))
            resultado = _resolve_resultado(
                bot.get('mercado', ''), tick.get('selecao', ''),
                linha_num, score_home, score_away,
            )
            if resultado is None:
                rej['sem_resultado'] += 1
                continue

            apostas_por_evento[evt] = apostas_por_evento.get(evt, 0) + 1
            candidatas.append({
                'tick': tick,
                'linha_num': linha_num,
                'score_home': score_home,
                'score_away': score_away,
                'resultado': resultado,
                'stats': stats,
            })

        rej_str = ', '.join(f'{k}={v}' for k, v in rej.items() if v > 0) or 'nenhuma'
        logger.info(
            f"[backtest] Job {job_id}: {len(candidatas)} candidatas. "
            f"Rejeicoes: {rej_str}. H2H cache: {h2h_cache.stats_cache}"
        )

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE backtest_jobs SET progresso=80, progresso_msg=$2 WHERE id=$1",
                job_id, f"{len(candidatas)} validadas. Rej: {rej_str[:200]}",
            )

        banca = banca_inicial
        banca_pico = banca_inicial
        green = red = void_count = 0
        streak_red_atual = 0
        max_streak_red = 0
        apostas_detalhe = []
        equity_curve = []
        pnl_por_dia: dict = {}

        candidatas.sort(key=lambda x: x['tick']['ts'])

        for i, c in enumerate(candidatas):
            tick = c['tick']
            odd = float(tick['odds'])
            resultado = c['resultado']

            if stake_modo == 'fixo':
                stake = stake_valor
            else:
                stake = banca_pico * (stake_valor / 100.0)
                stake = round(stake, 2)

            if resultado == 'green':
                pnl_aposta = stake * (odd - 1)
                green += 1
                streak_red_atual = 0
            elif resultado == 'red':
                pnl_aposta = -stake
                red += 1
                streak_red_atual += 1
                if streak_red_atual > max_streak_red:
                    max_streak_red = streak_red_atual
            else:
                pnl_aposta = 0
                void_count += 1
                streak_red_atual = 0

            banca += pnl_aposta
            if banca > banca_pico:
                banca_pico = banca

            apostas_detalhe.append({
                'n': i + 1,
                'event_id': tick['event_id'],
                'ts': tick['ts'].isoformat() if hasattr(tick['ts'], 'isoformat') else str(tick['ts']),
                'jogador_a': tick['jogador_a'],
                'jogador_b': tick['jogador_b'],
                'mercado': tick['mercado'],
                'linha': c['linha_num'],
                'selecao': tick['selecao'],
                'odd': odd,
                'stake': stake,
                'resultado': resultado,
                'pnl': round(pnl_aposta, 2),
                'banca_apos': round(banca, 2),
                'score_final': f"{c['score_home']}-{c['score_away']}",
            })

            equity_curve.append({
                'n': i + 1,
                'banca': round(banca, 2),
                'pnl_acum': round(banca - banca_inicial, 2),
                'ts': tick['ts'].isoformat() if hasattr(tick['ts'], 'isoformat') else str(tick['ts']),
            })

            dia_key = tick['ts'].date().isoformat() if hasattr(tick['ts'], 'date') else str(tick['ts'])[:10]
            if dia_key not in pnl_por_dia:
                pnl_por_dia[dia_key] = {'data': dia_key, 'apostas': 0, 'pnl': 0.0}
            pnl_por_dia[dia_key]['apostas'] += 1
            pnl_por_dia[dia_key]['pnl'] = round(pnl_por_dia[dia_key]['pnl'] + pnl_aposta, 2)

        total_apostas = green + red + void_count
        total_stake = sum(a['stake'] for a in apostas_detalhe) if apostas_detalhe else 0
        pnl_total = banca - banca_inicial

        roi = (pnl_total / total_stake) if total_stake > 0 else 0
        win_rate = (green / (green + red)) if (green + red) > 0 else 0
        drawdown_max = max(0, banca_pico - min((p['banca'] for p in equity_curve), default=banca_inicial))

        pnl_por_dia_lista = sorted(pnl_por_dia.values(), key=lambda d: d['data'])
        dias_verdes = sum(1 for d in pnl_por_dia_lista if d['pnl'] > 0)
        dias_total = len(pnl_por_dia_lista)

        rej_resumo = ', '.join(f'{k}={v}' for k, v in rej.items() if v > 0) or 'nenhuma'
        msg_final = f'Concluido. Rej: {rej_resumo}' if total_apostas == 0 else 'Concluido'
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE backtest_jobs SET
                    status='concluido', progresso=100,
                    progresso_msg=$16,
                    total_apostas=$2, green=$3, red=$4, void_count=$5,
                    pnl=$6, roi=$7, win_rate=$8, drawdown_max=$9, max_streak_red=$10,
                    dias_verdes=$11, dias_total=$12,
                    equity_curve=$13::jsonb, apostas_detalhe=$14::jsonb, pnl_por_dia=$15::jsonb,
                    concluido_em=NOW()
                WHERE id=$1
                """,
                job_id, total_apostas, green, red, void_count,
                round(pnl_total, 2), round(roi, 4), round(win_rate, 4),
                round(drawdown_max, 2), max_streak_red,
                dias_verdes, dias_total,
                json.dumps(equity_curve, default=str),
                json.dumps(apostas_detalhe, default=str),
                json.dumps(pnl_por_dia_lista, default=str),
                msg_final[:500],
            )

        logger.info(
            f"[backtest] Job {job_id} concluido: {total_apostas} apostas, "
            f"ROI {roi*100:.2f}%, WR {win_rate*100:.2f}%, PnL {pnl_total:.2f}"
        )

    except Exception as e:
        logger.exception(f"[backtest] Erro no job {job_id}: {e}")
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE backtest_jobs SET status='erro', erro=$2, concluido_em=NOW() WHERE id=$1",
                    job_id, str(e)[:500],
                )
        except Exception as e2:
            logger.exception(f"[backtest] Falha ao salvar erro: {e2}")
