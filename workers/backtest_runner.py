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
import re
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


def _lado_aposta(selecao: str) -> Optional[str]:
    """v9: deriva o lado ('over'/'under') da selecao do tick, pra calcular o WR
    do lado certo. Retorna None se nao for um mercado over/under."""
    s = _normalizar(selecao)
    if not s:
        return None
    if any(w in s for w in ('mais', 'over', 'acima')) or s.startswith('+') or s in ('sim', 'yes'):
        return 'over'
    if any(w in s for w in ('menos', 'under', 'abaixo')) or s.startswith('-') or s in ('nao', 'no'):
        return 'under'
    return None


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
        #   'all'         -> 0 (TODAS, usa todo o historico do par)
        #   'last_0'      -> 0 (TODAS, alias)
        #   'last_N'      -> N (quantidade: ultimos N jogos)
        #   'last_8h'/'last_7d' -> '8h'/'7d' (TEMPO: janela por horas/dias)
        # v10: janelas de TEMPO agora SAO suportadas (antes eram descartadas).
        # O _calcular_stats_h2h sabe processar tanto quantidade (int) quanto
        # tempo (token '8h'/'7d'), usando o ts de cada jogo + ts_ref da aposta.
        janela_str = str(fh.get('janela', '')).strip().lower()
        janela_norm = None  # int (qtd) OU str token de tempo ('8h')
        if janela_str == 'all':
            janela_norm = 0
        elif janela_str.startswith('last_'):
            resto = janela_str.replace('last_', '').strip()
            modo, _ = _parse_janela(resto)
            if modo == 'qtd':
                janela_norm = int(resto)
            elif modo == 'tempo':
                janela_norm = _janela_token(resto)  # '8h','24h','7d'...
            # resto invalido -> janela_norm None -> filtro descartado abaixo
        if janela_norm is None:
            continue
        if isinstance(janela_norm, int) and janela_norm < 0:
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
            'janela': janela_norm,
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


# ============================================================
# JANELAS: quantidade (int) OU tempo (string "24h"/"7d")
# ============================================================
# Uma janela de filtro pode ser:
#   - QUANTIDADE: int N -> ultimos N jogos (ex: 10 = ult10). 0 = TODAS.
#   - TEMPO: string "Nh"/"Nd" -> jogos nas ultimas N horas/dias (ex: "24h","7d").
# As janelas de tempo so do PAR (H2H) por enquanto. O ts de referencia e o
# momento da aposta (tick['ts'] no ao vivo, ts do tick no backtest) - mesmo
# cutoff temporal das janelas de quantidade, sem leak.
_RE_JANELA_TEMPO = re.compile(r'^\s*(\d+)\s*([hd])\s*$', re.IGNORECASE)


def _parse_janela(janela):
    """Normaliza uma janela. Retorna (modo, valor):
      ('qtd', n)            quantidade (int>=1, ou 0='todas')
      ('tempo', segundos)   tempo ('24h'->86400, '7d'->604800)
      (None, None)          invalida
    """
    if isinstance(janela, bool):  # bool e subclasse de int - barra antes
        return (None, None)
    if isinstance(janela, int):
        return ('qtd', janela) if (janela == 0 or janela >= 1) else (None, None)
    if janela is None:
        return (None, None)
    s = str(janela).strip().lower()
    m = _RE_JANELA_TEMPO.match(s)
    if m:
        num = int(m.group(1))
        if num <= 0:
            return (None, None)
        return ('tempo', num * 3600 if m.group(2) == 'h' else num * 86400)
    try:
        n = int(s)
        return ('qtd', n) if (n == 0 or n >= 1) else (None, None)
    except (TypeError, ValueError):
        return (None, None)


def _janela_token(janela):
    """Token canonico da janela pra montar a chave de stats:
      10 -> '10', '24h' -> '24h', '7d' -> '7d'. None se invalida."""
    modo, _ = _parse_janela(janela)
    if modo == 'tempo':
        return str(janela).strip().lower().replace(" ", "")
    if modo == 'qtd':
        return str(int(janela))
    return None


def _extrair_janelas_dos_filtros(filtros_unificados: list) -> tuple[set, set]:
    """
    Extrai janelas customizadas (alem das padrao) dos filtros unificados.
    Retorna (janelas_wr, janelas_media).
    Recebe lista JA UNIFICADA (saida de _coletar_todos_filtros).
    """
    # Cada set guarda janelas de QUANTIDADE (int) E de TEMPO (string "24h"/"7d").
    # _calcular_stats_h2h sabe distinguir via _parse_janela.
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

        modo, _ = _parse_janela(janela)
        if modo is None:
            continue  # janela invalida, ignora
        if modo == 'qtd':
            j = int(janela)
            # janela 0 = 'TODAS'. 1..TETO_BUSCA aceito.
            if j != 0 and (j < 1 or j > H2HCache.TETO_BUSCA):
                continue
            chave = j
        else:
            # tempo: guarda o token normalizado ('24h','7d') no set
            chave = _janela_token(janela)
            if chave is None:
                continue

        if tipo == 'wr':
            janelas_wr.add(chave)
        elif tipo == 'media':
            janelas_media.add(chave)
        elif tipo in ('gap_media', 'gap'):
            # gap_media = media_ult{janela} - linha. Precisa que a media daquela
            # janela seja calculada -> adiciona a janela em janelas_media.
            janelas_media.add(chave)

    return janelas_wr, janelas_media


def _calcular_stats_h2h(jogos: list, linha_atual: float,
                        janelas_wr: Optional[set] = None,
                        janelas_media: Optional[set] = None,
                        lado: Optional[str] = None,
                        ts_ref=None) -> dict:
    """
    Calcula stats H2H com janelas dinamicas.

    v5: usa MIN(qtd, N) jogos quando qtd < N (em vez de retornar None).
    Tambem grava 'wr_ult{N}_qtd' e 'media_ult{N}_qtd' indicando quantos jogos
    foram realmente usados, pra que _aplicar_filtros_complementares possa
    validar contra min_partidas do filtro.

    v9: parametro `lado` ('over'/'under'/None). O WR e calculado como % de
    jogos que VENCERIAM a aposta DAQUELE lado:
      - over  (ou None, legado): % com total > linha
      - under: % com total < linha
    Como as linhas sao sempre .5, total (inteiro) nunca empata na linha,
    entao wr_under = 1 - wr_over exatamente. Antes (ate v8) o WR era SEMPRE
    do over e usado pros dois lados - um under apitava olhando o WR do over.

    v10: janelas de TEMPO ("24h","7d"). Alem de janela por quantidade (ultimos
    N jogos), aceita janela por tempo: jogos cujo ts esta nas ultimas N horas/
    dias a partir de ts_ref. A chave de stats usa o token: wr_ult24h, wr_ult7d.
    ts_ref e o momento da aposta (tick['ts']); se nao vier, janelas de tempo
    sao puladas (retornam None) - nunca usa NOW() pra nao vazar no backtest.
    As janelas de quantidade continuam 100% iguais.
    """
    qtd = len(jogos)

    if janelas_wr is None:
        janelas_wr = set(JANELAS_PADRAO_WR)
    if janelas_media is None:
        janelas_media = set(JANELAS_PADRAO_MEDIA)

    eh_under = (str(lado).lower().strip() == 'under') if lado else False

    def wr(n):
        # v5: usa o que tiver (ate N). Se nao tem nada, retorna None.
        # n==0 = 'TODAS' -> usa todos os jogos disponiveis do par.
        if qtd <= 0:
            return None, 0
        usar = qtd if n == 0 else min(qtd, n)
        slice_ = jogos[:usar]
        # v9: conta o lado certo. under = total < linha; over = total > linha.
        if eh_under:
            passou = sum(1 for j in slice_ if j['total'] < linha_atual)
        else:
            passou = sum(1 for j in slice_ if j['total'] > linha_atual)
        return passou / usar, usar

    def media(n):
        if qtd <= 0:
            return None, 0
        usar = qtd if n == 0 else min(qtd, n)
        slice_ = jogos[:usar]
        return sum(j['total'] for j in slice_) / usar, usar

    # ---- v10: janelas de TEMPO ----
    # Seleciona jogos com ts em [ts_ref - segundos, ts_ref). Reusa o ts que ja
    # vem em cada jogo (mesmo cutoff temporal das janelas de quantidade).
    # BLINDADO: ts None, tipos de timestamp incompativeis (aware vs naive), e
    # qualquer erro de comparacao -> ignora o jogo problematico em vez de
    # derrubar a avaliacao da aposta. Dinheiro real: melhor amostra menor (ou
    # None) do que crash. ts_ref e o MESMO tick['ts'] que o get_jogos ja compara
    # com sucesso, entao na pratica os tipos batem; isto e defesa em profundidade.
    def _jogos_na_janela_tempo(segundos):
        if ts_ref is None:
            return None  # sem referencia temporal -> nao da pra calcular
        try:
            corte = ts_ref - timedelta(seconds=segundos)
        except (TypeError, ValueError, OverflowError):
            return None
        sel = []
        for j in jogos:
            t = j.get('ts')
            if t is None:
                continue
            try:
                if corte <= t < ts_ref:
                    sel.append(j)
            except TypeError:
                # ts incompativel com ts_ref (ex: aware vs naive). Pula o jogo.
                continue
        return sel

    def _total_ok(j):
        """total do jogo como numero, ou None se faltar/invalido."""
        t = j.get('total')
        if t is None:
            return None
        try:
            return float(t)
        except (TypeError, ValueError):
            return None

    def wr_tempo(segundos):
        sel = _jogos_na_janela_tempo(segundos)
        if not sel:
            return None, 0
        if linha_atual is None:
            return None, 0
        # ignora jogos sem 'total' valido (nao conta nem no numerador nem no denom)
        validos = [t for t in (_total_ok(j) for j in sel) if t is not None]
        if not validos:
            return None, 0
        if eh_under:
            passou = sum(1 for t in validos if t < linha_atual)
        else:
            passou = sum(1 for t in validos if t > linha_atual)
        # denominador = jogos VALIDOS (com total), nao o total bruto da janela
        return passou / len(validos), len(validos)

    def media_tempo(segundos):
        sel = _jogos_na_janela_tempo(segundos)
        if not sel:
            return None, 0
        validos = [t for t in (_total_ok(j) for j in sel) if t is not None]
        if not validos:
            return None, 0
        return sum(validos) / len(validos), len(validos)

    out: dict = {'qtd_h2h': qtd}

    for jan in janelas_wr:
        modo, valor = _parse_janela(jan)
        if modo == 'tempo':
            v, usados = wr_tempo(valor)
            tok = _janela_token(jan)
        elif modo == 'qtd':
            v, usados = wr(valor)
            tok = str(valor)
        else:
            continue
        out[f'wr_ult{tok}'] = v
        out[f'wr_ult{tok}_qtd'] = usados
    for jan in janelas_media:
        modo, valor = _parse_janela(jan)
        if modo == 'tempo':
            v, usados = media_tempo(valor)
            tok = _janela_token(jan)
        elif modo == 'qtd':
            v, usados = media(valor)
            tok = str(valor)
        else:
            continue
        out[f'media_ult{tok}'] = v
        out[f'media_ult{tok}_qtd'] = usados

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
            # v10: usa _janela_token (funciona p/ quantidade E tempo "24h")
            qtd_validar = qtd_global
            tok = _janela_token(janela)
            if tipo == 'wr' and tok is not None:
                qtd_validar = stats.get(f'wr_ult{tok}_qtd', qtd_global) or 0
            elif tipo == 'media' and tok is not None:
                qtd_validar = stats.get(f'media_ult{tok}_qtd', qtd_global) or 0

        if qtd_validar < min_partidas:
            return False, f'h2h_insuficiente_qtd_{qtd_validar}_min_{min_partidas}'

        valor = None

        if tipo == 'media' and janela is not None:
            tok = _janela_token(janela)
            valor = stats.get(f'media_ult{tok}') if tok is not None else None
        elif tipo == 'wr' and janela is not None:
            tok = _janela_token(janela)
            valor = stats.get(f'wr_ult{tok}') if tok is not None else None
        elif tipo in ('gap_media', 'gap'):
            # gap_media = media_ult{janela} - linha, EM CADA JANELA.
            # Se o filtro tem janela, usa a media daquela janela; senao cai no
            # gap padrao (media_ult20 - linha) por compatibilidade.
            linha_g = stats.get('linha_atual')
            tok = _janela_token(janela) if janela is not None else None
            if tok is not None:
                media_jan = stats.get(f'media_ult{tok}')
                if media_jan is not None and linha_g is not None:
                    valor = media_jan - linha_g
                else:
                    valor = None
            else:
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


def _num_seguro(v):
    """Coage qualquer valor a float de forma segura, tratando os tipos que
    chegam do parquet/banco/snapshot. Retorna (float, None) em sucesso ou
    (None, motivo) em falha. Cobre:
      - None -> ausente
      - string numerica ('1.85', '+2.5', 'away|0.5') -> parseia (igual linha)
      - string vazia / nao-numerica -> invalido
      - NaN (float('nan') ou numpy.nan) -> invalido (NaN quebra comparacoes)
      - int/float/Decimal/numpy number -> float()
    Dinheiro real: prefere reportar 'invalido' a deixar passar NaN (que faz
    toda comparacao virar False silenciosamente) ou quebrar com str<float.
    """
    if v is None:
        return None, 'ausente'
    # ja numerico?
    if isinstance(v, bool):
        # bool e int em python; nao deveria ser odd/linha -> trata como invalido
        return None, 'tipo_bool'
    if isinstance(v, (int, float, Decimal)):
        try:
            f = float(v)
        except (TypeError, ValueError, OverflowError):
            return None, 'nao_convertivel'
        if f != f:  # NaN (NaN != NaN é True)
            return None, 'nan'
        return f, None
    # string (ou numpy str, ou outro) -> tenta parsear como a linha faz
    try:
        s = str(v).strip()
    except Exception:
        return None, 'nao_stringificavel'
    if not s:
        return None, 'vazio'
    # trata formatos da casa: 'away|0.5' -> '0.5'; '+2.5' -> '2.5'
    if '|' in s:
        s = s.split('|')[0]
    if s.startswith('+'):
        s = s[1:]
    try:
        f = float(s)
    except (TypeError, ValueError):
        return None, f'nao_numerico({s[:20]})'
    if f != f:
        return None, 'nan'
    return f, None


def _avaliar_filtros_basicos(tick: dict, bot: dict) -> tuple[bool, str]:
    """
    Avalia os filtros basicos do tick. BLINDADO: cada comparacao numerica coage
    os dois lados (tick e bot) com _num_seguro e, se algum nao converter, retorna
    motivo CLARO em vez de quebrar (str<float) ou passar NaN silenciosamente.
    Filosofia (dinheiro real): tick com campo invalido (odd/linha que nao da pra
    ler) = NAO apostar, com motivo legivel. Nunca crash, nunca comparacao furada.
    """
    # --- LINHA ---
    linha = _parse_linha(tick.get('linha'))
    if linha is None:
        return False, 'linha_invalida'

    lmin = bot.get('linha_min')
    if lmin is not None:
        lmin_f, err = _num_seguro(lmin)
        if err is not None:
            return False, f'bot.linha_min_{err}'  # config do bot ruim -> reporta
        if linha < lmin_f:
            return False, f'linha_{linha}_lt_min_{lmin_f}'
    lmax = bot.get('linha_max')
    if lmax is not None:
        lmax_f, err = _num_seguro(lmax)
        if err is not None:
            return False, f'bot.linha_max_{err}'
        if linha > lmax_f:
            return False, f'linha_{linha}_gt_max_{lmax_f}'

    # --- ODD ---
    odd_f, err = _num_seguro(tick.get('odds'))
    if err is not None:
        # odd do tick invalida (ausente, NaN, string, score_update odd=0->valido
        # mas <min). 'ausente'/'vazio'/'nan' -> tick sem odd real, nao aposta.
        return False, f'odd_{err}'
    omin = bot.get('odd_min')
    if omin is not None:
        omin_f, err = _num_seguro(omin)
        if err is not None:
            return False, f'bot.odd_min_{err}'
        if odd_f < omin_f:
            return False, f'odd_{odd_f}_lt_min_{omin_f}'
    omax = bot.get('odd_max')
    if omax is not None:
        omax_f, err = _num_seguro(omax)
        if err is not None:
            return False, f'bot.odd_max_{err}'
        if odd_f > omax_f:
            return False, f'odd_{odd_f}_gt_max_{omax_f}'

    # --- MERCADO ---
    # _matches_mercado compara mercado_tipo (string) com mapping. Coage o tipo a
    # string pra nao falhar se vier numero do parquet (18 vs '18').
    mtipo = tick.get('mercado_tipo')
    mtipo_str = '' if mtipo is None else str(mtipo).strip()
    if not _matches_mercado(bot.get('mercado', ''), tick.get('mercado', ''),
                            mtipo_str, bot.get('casa', '')):
        return False, f'mercado_nao_bate(tipo={mtipo_str[:12]})'

    # --- BLACKLIST / WHITELIST (strings, nao quebram, mas blinda .lower()) ---
    blacklist_pares = bot.get('blacklist_pares') or []
    if blacklist_pares:
        ja = (tick.get('jogador_a') or '').lower()
        jb = (tick.get('jogador_b') or '').lower()
        ta = (tick.get('time_a') or '').lower()
        tb = (tick.get('time_b') or '').lower()
        for entry in blacklist_pares:
            if not isinstance(entry, dict):
                continue
            j1 = (entry.get('j1') or '').lower()
            j2 = (entry.get('j2') or '').lower()
            t1 = (entry.get('t1') or '').lower()
            t2 = (entry.get('t2') or '').lower()
            if j1 and (j1 == ja or j1 == jb): return False, f'blacklist_{j1}'
            if j2 and (j2 == ja or j2 == jb): return False, f'blacklist_{j2}'
            if t1 and (t1 == ta or t1 == tb): return False, f'blacklist_time_{t1}'
            if t2 and (t2 == ta or t2 == tb): return False, f'blacklist_time_{t2}'

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

            # v10 (peca 3): fonte dos ticks. Se upload_id vier preenchido, le do
            # ARQUIVO parquet; senao, le do BANCO por periodo (comportamento atual).
            # job_row pode nao ter a coluna (migration nao rodada) -> trata como None.
            try:
                upload_id = job_row['upload_id']
            except (KeyError, IndexError):
                upload_id = None

            fonte_ticks = 'arquivo' if upload_id else 'banco'

            await conn.execute(
                "UPDATE backtest_jobs SET status='rodando', progresso=5, "
                "progresso_msg=$2 WHERE id=$1",
                job_id,
                f"Buscando ticks ({fonte_ticks})",
            )

        filtros = bot.get('filtros') or {}
        cenario_ativo = filtros.get('cenarioPartidaAtivo', False)
        cenario_partida = filtros.get('cenarioPartida') if cenario_ativo else None
        diff_ativo = filtros.get('diferencaPlacarAtivo', False)
        diff_min = filtros.get('diferencaPlacar', 0) if diff_ativo else 0

        # FIX (over-entry): replica o evitarLinhasSeq do bot_executor (default True).
        # AO VIVO o bot aposta 1 vez por mercado_tipo por jogo (trava qualquer 2a
        # linha do mesmo mercado no mesmo event_id). Sem isso, o backtest apostava
        # TODA linha (Over 0.5, 1.5, 2.5...7.5) do mesmo jogo -> ~9x mais apostas e
        # WR colapsando pra taxa-base (as linhas altas perdem). Agora bate com o vivo.
        evitar_linhas_seq = filtros.get('evitarLinhasSeq', True)
        mercado_apostado_evt: set = set()  # {(event_id, mercado_tipo)} ja apostados

        # FIX (lado / falso-positivo): replica o filtro de LADO do bot_executor
        # (linhas 374-385). AO VIVO o bot so aposta os lados configurados em
        # filtros.lados / filtros.lado (ex.: ['over']). SEM esse filtro o backtest
        # gerava candidato pro OVER **e** pro UNDER da mesma linha/jogo e apostava
        # os dois -> WR colado em ~55% (cara-ou-coroa por construcao), MASCARANDO a
        # performance real do lado configurado (o over puro do #32 da 39%/-27%).
        # 'ambos' (ou ausencia) => lados_bot_norm=None => nao filtra (aceita os dois).
        lados_bot = filtros.get('lados')
        if lados_bot is None and filtros.get('lado'):
            _lado_str = str(filtros.get('lado')).lower().strip()
            lados_bot = [] if _lado_str == 'ambos' else [_lado_str]
        lados_bot_norm = None
        if lados_bot and isinstance(lados_bot, list) and len(lados_bot) > 0:
            lados_bot_norm = [str(l).lower().strip() for l in lados_bot if l]

        # v4: coleta filtros dos 2 lugares (comp + hist normalizado)
        filtros_unificados = _coletar_todos_filtros(filtros)
        janelas_wr, janelas_media = _extrair_janelas_dos_filtros(filtros_unificados)

        if filtros_unificados:
            tipos_resumo = [f"{f.get('tipo')}_ult{f.get('janela')}" for f in filtros_unificados]
            logger.info(f"[backtest] Filtros unificados: {tipos_resumo}")

        async with pool.acquire() as conn:
            torneios = bot.get('torneios') or []
            torneios_excluir = bot.get('torneios_excluir') or []

            # === v10 (peca 3): FONTE = ARQUIVO ===
            if upload_id:
                try:
                    from workers.backtest_upload import (parse_ticks_parquet,
                                                         caminho_do_upload,
                                                         BacktestUploadError)
                except ImportError as e:
                    raise RuntimeError(
                        "modulo backtest_upload nao encontrado no worker"
                    ) from e

                try:
                    caminho = caminho_do_upload(upload_id)
                    ticks = parse_ticks_parquet(caminho, bot=bot)
                except BacktestUploadError as e:
                    # erro previsivel de arquivo: marca job erro com msg clara
                    raise RuntimeError(f"Falha no arquivo de ticks: {e}") from e

                total_ticks = len(ticks)
                logger.info(f"[backtest] Job {job_id}: {total_ticks} ticks do ARQUIVO")

                await conn.execute(
                    "UPDATE backtest_jobs SET progresso=15, "
                    "progresso_msg=$2, total_ticks_avaliados=$3 WHERE id=$1",
                    job_id,
                    f"Lidos {total_ticks} ticks do arquivo. Aplicando filtros...",
                    total_ticks,
                )

                if total_ticks == 0:
                    await conn.execute(
                        """
                        UPDATE backtest_jobs SET
                            status='concluido', progresso=100,
                            progresso_msg='Arquivo sem ticks apos filtros do bot',
                            total_apostas=0, green=0, red=0, void_count=0,
                            pnl=0, roi=0, win_rate=0, drawdown_max=0, max_streak_red=0,
                            dias_verdes=0, dias_total=0,
                            equity_curve='[]'::jsonb, apostas_detalhe='[]'::jsonb,
                            pnl_por_dia='[]'::jsonb, concluido_em=NOW()
                        WHERE id=$1
                        """,
                        job_id,
                    )
                    return

            # === FONTE = BANCO (comportamento atual) ===
            else:
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
        _placar_ts: dict = {}   # evt -> ts do tick que deu o placar (pega o MAIS RECENTE)

        for t in ticks:
            evt = t['event_id']
            sh = t['score_home']
            sa = t['score_away']
            if sh is not None and sa is not None:
                # FIX (dado falso): placar FINAL = score do tick com MAIOR ts do
                # evento - exatamente como o telegram_notifier resolve em producao
                # (SELECT ... ORDER BY ts DESC LIMIT 1). Antes pegava o "ultimo na
                # ordem do SQL" (event_id, mercado_id, linha, selecao_id, ts), que
                # NAO e o ultimo no tempo: se o ultimo mercado/selecao do evento
                # parou de atualizar antes do fim, o placar 'final' saia errado e
                # o backtest resolvia GREEN/RED contra um placar parcial.
                tts = t['ts']
                if evt not in _placar_ts or tts >= _placar_ts[evt]:
                    placar_final[evt] = (sh, sa)
                    _placar_ts[evt] = tts

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
            'sem_placar': 0, 'sem_resultado': 0, 'lado': 0,
        }
        # Detalhe das rejeicoes BASICAS por sub-motivo (odd_lt_min, mercado_nao_bate,
        # linha_invalida, odd_ausente, etc). Permanente, vai pro relatorio - assim
        # 'basico=N' nunca mais e uma caixa-preta: a UI mostra a quebra.
        rej_basico_detalhe: dict = {}
        # v10: contadores de QUALIDADE (nao rejeitam, mas reportam no relatorio).
        # Filosofia: num backtest de dinheiro real, o numero precisa vir com
        # ressalva honesta. Esconder que X apostas tiveram h2h fraco = mentir.
        qualidade = {
            'apostas_h2h_fraco': 0,   # apostou mas com poucos jogos h2h (<min saudavel)
            'eventos_sem_placar_final': 0,  # tick sem placar pra resolver
            'ticks_odds_invalida': 0,  # odds NaN/<=1 (score_update etc) - pulados
        }
        H2H_MIN_SAUDAVEL = 10  # abaixo disso, marca como amostra fraca

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

            # filtro de LADO (over/under) - igual ao executor (linhas 374-385).
            # Tick cujo lado nao esta na lista do bot e cortado ANTES de tudo:
            # nao consome cap_jogo nem trava mercado, exatamente como ao vivo.
            if lados_bot_norm is not None:
                _sel_lado = _lado_aposta(tick.get('selecao'))
                if _sel_lado is not None and _sel_lado not in lados_bot_norm:
                    rej['lado'] += 1
                    continue

            if max_apostas_partida is not None and apostas_por_evento.get(evt, 0) >= max_apostas_partida:
                rej['cap_jogo'] += 1
                continue

            # evitarLinhasSeq: 1 aposta por mercado_tipo por jogo (igual ao vivo).
            # ticks_ordenados esta em ordem de ts, entao o 1o aceitado por
            # (evento, mercado_tipo) e o mais cedo no tempo = o que o bot ao vivo
            # teria apostado (primeiro tick que passa, depois trava o mercado).
            if evitar_linhas_seq:
                _mtipo_evt = tick.get('mercado_tipo')
                if (evt, _mtipo_evt) in mercado_apostado_evt:
                    rej['mercado_repetido'] = rej.get('mercado_repetido', 0) + 1
                    continue

            passou, _motivo_basico = _avaliar_filtros_basicos(tick, bot)
            if not passou:
                rej['basico'] += 1
                # Agrupa o SUB-MOTIVO da rejeicao basica (sem os valores, so a
                # categoria) pra reportar no relatorio: assim a UI mostra POR QUE
                # os ticks caem no basico (odd_lt_min, mercado_nao_bate, etc) em
                # vez de so 'basico=N'. Normaliza tirando numeros pra agrupar.
                _cat = re.sub(r'[0-9.]+', '#', _motivo_basico)
                rej_basico_detalhe[_cat] = rej_basico_detalhe.get(_cat, 0) + 1
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

                # v10: get_jogos toca o banco - blinda. Se falhar a busca do h2h
                # de UM tick, nao derruba o backtest inteiro: conta e pula esse.
                try:
                    jogos_h2h = await h2h_cache.get_jogos(
                        ja, jb, tick['ts'], event_id_excluir=tick.get('event_id'))
                except Exception as e:
                    logger.warning(
                        f"[backtest] job {job_id}: falha h2h {ja}x{jb}: {e}")
                    rej['sem_par'] += 1
                    continue

                linha_num = _parse_linha(tick.get('linha')) or 0
                stats = _calcular_stats_h2h(jogos_h2h, linha_num, janelas_wr, janelas_media,
                                            lado=_lado_aposta(tick.get('selecao')),
                                            ts_ref=tick['ts'])
                stats['linha_atual'] = linha_num

                passou_comp, motivo = _aplicar_filtros_complementares(stats, filtros_unificados)
                if not passou_comp:
                    if 'h2h_insuficiente' in motivo:
                        rej['h2h_insuf'] += 1
                    else:
                        rej['comp'] += 1
                    continue

                # v10: passou nos filtros, mas com amostra h2h fraca? Marca pra
                # reportar (NAO rejeita - so transparencia). qtd_h2h vem do stats.
                qtd_h2h = stats.get('qtd_h2h', 0) or 0
                if qtd_h2h < H2H_MIN_SAUDAVEL:
                    qualidade['apostas_h2h_fraco'] += 1

            placar = placar_final.get(evt)
            if not placar:
                rej['sem_placar'] += 1
                qualidade['eventos_sem_placar_final'] += 1
                continue
            score_home, score_away = placar

            linha_num = _parse_linha(tick.get('linha'))
            mercado_bot = bot.get('mercado', '')
            resultado = _resolve_resultado(
                mercado_bot, tick.get('selecao', ''),
                linha_num, score_home, score_away,
            )
            if resultado is None:
                # Mercados de 1o tempo (HT) nao tem como ser resolvidos com o placar
                # FINAL - precisariam do placar do intervalo (nao disponivel aqui).
                # Em vez de cair mudo em 'sem_resultado', conta num balde proprio
                # pra UI deixar claro POR QUE deu 0 (e nao parecer bug de "nada").
                if mercado_bot in ('over_under_ht', 'asian_over_under_ht',
                                    'over_under_ht_player', 'ml_ht', 'ah_ht', 'asian_over_under_ht'):
                    rej['mercado_ht_sem_suporte'] = rej.get('mercado_ht_sem_suporte', 0) + 1
                else:
                    rej['sem_resultado'] += 1
                continue

            apostas_por_evento[evt] = apostas_por_evento.get(evt, 0) + 1
            if evitar_linhas_seq:
                mercado_apostado_evt.add((evt, tick.get('mercado_tipo')))
            candidatas.append({
                'tick': tick,
                'linha_num': linha_num,
                'score_home': score_home,
                'score_away': score_away,
                'resultado': resultado,
                'stats': stats,
            })

        rej_str = ', '.join(f'{k}={v}' for k, v in rej.items() if v > 0) or 'nenhuma'
        # monta o detalhe do basico (top sub-motivos) pra log e relatorio
        basico_det_str = ''
        if rej_basico_detalhe:
            top = sorted(rej_basico_detalhe.items(), key=lambda x: -x[1])
            basico_det_str = ' [basico: ' + ', '.join(f'{k}={v}' for k, v in top[:8]) + ']'

        logger.info(
            f"[backtest] Job {job_id}: {len(candidatas)} candidatas. "
            f"Rejeicoes: {rej_str}{basico_det_str}. H2H cache: {h2h_cache.stats_cache}"
        )

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE backtest_jobs SET progresso=80, progresso_msg=$2 WHERE id=$1",
                job_id, f"{len(candidatas)} validadas. Rej: {rej_str[:120]}{basico_det_str[:180]}",
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

        # v10: monta avisos de QUALIDADE (transparencia do resultado).
        # Num backtest de dinheiro real, o numero precisa vir com ressalvas.
        avisos = []
        if total_apostas > 0:
            frac_fraco = qualidade['apostas_h2h_fraco'] / total_apostas
            if qualidade['apostas_h2h_fraco'] > 0:
                avisos.append(
                    f"{qualidade['apostas_h2h_fraco']} apostas "
                    f"({frac_fraco:.0%}) com h2h fraco (<{H2H_MIN_SAUDAVEL} jogos)"
                )
        if qualidade['eventos_sem_placar_final'] > 0:
            avisos.append(
                f"{qualidade['eventos_sem_placar_final']} sinais sem placar final "
                f"(nao resolvidos)"
            )

        partes_msg = []
        if total_apostas == 0:
            # 0 apostas: mostra a quebra do basico (a UI ve POR QUE deu 0).
            partes_msg.append(f"Concluido. Rej: {rej_resumo}{basico_det_str}")
        else:
            partes_msg.append("Concluido")
        if avisos:
            partes_msg.append("RESSALVAS: " + "; ".join(avisos))
        msg_final = ' | '.join(partes_msg)

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
