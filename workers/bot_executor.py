"""
bot_executor.py - Worker de simulacao em tempo real (v9)

v9 - WR por LADO (over/under):
- Passa o lado da aposta pro _calcular_stats_h2h. O WR do under agora
  e % com total < linha (era sempre o do over). Como linhas sao .5,
  wr_under = 1 - wr_over exato. Corrige under apitando com WR do over.

v7 - Fix do filtro evitar_linhas_seq:
- Antes (v6): so bloqueava se ja existia aposta com MESMA linha exata.
  Bug: deixava apitar Over 1.5 depois de Over 2.5 no mesmo jogo.
- Agora: bloqueia se ja apitou QUALQUER linha do MESMO mercado_tipo no
  mesmo event_id. 1 aposta maxima por mercado por jogo.
- Contador renomeado de 'linha_repetida' pra 'mercado_repetido'.

v6 - Filtro evitar_linhas_seq:
- Le filtros.evitarLinhasSeq (default True)
- Se ativo: bloqueia aposta se ja existe aposta desse bot, nesse event_id,
  com MESMA linha e MESMO mercado_tipo. Conta rejeicao como 'linha_repetida'.
- Resolve issue do checkbox "Evitar linhas em sequencia" que nao tinha
  implementacao backend antes.

v5 - Suporte a filtrosHistAdicionados:
- Le filtros dos 2 lugares (filtrosCompAdicionados + filtrosHistAdicionados)
- Sempre calcula stats_h2h se TIVER QUALQUER filtro (comp ou hist)
- Aplica todos os filtros normalizados antes de aceitar a aposta
- Salva stats_h2h no banco mesmo pra filtros hist (telegram_notifier le)
- Janelas dinamicas (qualquer N do filtroHist tipo last_20, last_50)

v4 - Tradutor de liga (Superbet IDs + Bet365 codigos)

REUSA: todas as funcoes de filtragem do backtest_runner.py v4
"""

import asyncio
import json
import logging
import signal
import sys
import os
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Any

# Garante que a pasta PyCharmMiscProject (onde estao tm_backfill.py e os
# fase2_*.py) esteja no path, pro backfill ser importavel mesmo o executor
# rodando de tipmike_api/. Tenta o diretorio pai e um caminho fixo conhecido.
for _p in (
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    r"C:\Users\Administrator\PyCharmMiscProject",
):
    if _p and os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import asyncpg

from workers.backtest_runner import (
    _matches_mercado,
    _parse_linha,
    _normalizar,
    _resolve_resultado,
    H2HCache,
    _calcular_stats_h2h,
    _aplicar_filtros_complementares,
    _aplicar_filtro_cenario,
    _aplicar_filtro_diff_placar,
    _avaliar_filtros_basicos,
    _coletar_todos_filtros,
    _extrair_janelas_dos_filtros,
    ESPORTE_UI_PARA_BANCO,
    MIN_H2H_DEFAULT,
    # --- HC (handicap): mesmas funcoes do backtest, pra ao vivo NAO divergir ---
    _mercado_eh_hc,
    calcular_stat_hc,
    _ramo_hc_pct,
    _resolve_resultado_hc,
    _selecao_hc_valor,
    _num_seguro,
    _hc_blacklist_bloqueia,
)


# ============================================================
# TRADUTORES DE LIGA
# ============================================================

SUPERBET_ID_TO_NAME = {
    "49959": "Battle - Premier League",
    "49964": "Battle - Liga dos Campeões 1 2x4",
    "49965": "Battle - Internacional 1 2x4",
    "49968": "Battle - Europa League 2x4",
    "51264": "Battle - LaLiga 1 2x4",
    "61751": "GT - Liga dos Campeões 1",
    "61753": "GT - Conference League 2x6",
    "61755": "GT - Liga dos Campeões 3 2x6",
    "61756": "GT - Europa League 1 2x6",
    "61757": "GT - Bundesliga 2x6",
    "61758": "GT - Premier League 2x6",
    "67118": "Battle - Bundesliga 2x4",
    "67380": "EAL - Liga dos Campeões 2x5",
    "67383": "EAL - Premier League",
    "67400": "EAL - Série A",
    "67556": "EAL - Premier League 2x5",
    "67892": "EAL - Internacional 2x5",
    "71851": "Liga dos Campeões 2x6",
    "72619": "Battle - Volta Liga dos Campeões 2x3",
    "72621": "Battle - Volta Premier League 2x3",
    "72623": "Battle - Volta Bundesliga 2x3",
    "72624": "Battle - Volta Liga dos Campeões 2x3",
    "80560": "H2H - GG League 2x4",
    "81968": "Battle - Portugal Primera 2x4",
    "81987": "Battle - Portugal Primera 2x4",
    "81988": "Battle - Argentina Super League 2x6",
    "91005": "Tênis Esports",
    "91014": "NHL Esports",
    "91015": "NHL Esports League",
    "94993": "Cyber Live Arena",
    "97337": "Battle - Copa do Mundo 2x4",
    "97693": "Battle - Copa do Mundo B",
    "98257": "National Teams 3x4",
    "75124": "Battle - NBA 1",
    "80566": "NBA League 2x4",
    "89069": "EAL - NextGen",
    "92679": "European Conference 4x5",
}

BET365_CODE_TO_NAME = {
    "ESOC-GTL-12MP":   "GT Leagues - 2x6",
    "ESOCH2HGG-8MP":   "H2H GG League - 2x4",
    "ESOCBATVOL-6":    "Battle Volta - 2x3",
    "ESOCCERBATTLE":   "Battle - 2x4",
    "B-EBASKBLITZ4X5": "H2H GG League - 4x5",
    "B-EBASKBAT4X5":   "Battle - 5x5",
}

SUPERBET_ALIASES = {
    "Live Arena": "Cyber Live Arena",
}


def traduzir_liga(bookmaker: str, liga: str) -> str:
    if not liga:
        return liga
    if bookmaker == 'superbet':
        if liga in SUPERBET_ID_TO_NAME:
            return SUPERBET_ID_TO_NAME[liga]
        if liga in SUPERBET_ALIASES:
            return SUPERBET_ALIASES[liga]
    if bookmaker == 'bet365' and liga in BET365_CODE_TO_NAME:
        return BET365_CODE_TO_NAME[liga]
    return liga


# ============================================================
# CONFIG
# ============================================================
DB_DSN = "postgresql://postgres:mikedb0702@localhost:5432/mikedb"
CHANNEL = "tick_novo"
BOT_CACHE_REFRESH_SEC = 30
RESOLVER_INTERVAL_SEC = 300
H2H_CACHE_TTL_SEC = 300
STAKE_DEFAULT = Decimal('10.00')

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger('bot_executor')


# ============================================================
# ESTADO GLOBAL
# ============================================================
class State:
    pool: Optional[asyncpg.Pool] = None
    bots_ativos: list = []
    bots_atualizado_em: Optional[datetime] = None
    bots_lock: Optional[asyncio.Lock] = None
    bots_assinatura: str = ""
    h2h_cache_por_casa: dict = {}
    h2h_cache_atualizado_em: Optional[datetime] = None
    contador_ticks: int = 0
    contador_apostas: int = 0
    contador_rejeicoes: dict = {}
    parar: bool = False


state = State()


# ============================================================
# HELPERS
# ============================================================
def _to_jsonb(value):
    if value is None:
        return None
    if isinstance(value, (list, dict)) and len(value) == 0:
        return None
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False, default=str)


async def _carregar_bots_ativos():
    global state

    agora = datetime.now()
    if (state.bots_atualizado_em
            and (agora - state.bots_atualizado_em).total_seconds() < BOT_CACHE_REFRESH_SEC):
        return state.bots_ativos

    if state.bots_lock is None:
        state.bots_lock = asyncio.Lock()

    async with state.bots_lock:
        agora = datetime.now()
        if (state.bots_atualizado_em
                and (agora - state.bots_atualizado_em).total_seconds() < BOT_CACHE_REFRESH_SEC):
            return state.bots_ativos

        try:
            async with state.pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT * FROM bots WHERE status = 'ativo'
                """)

            bots = []
            for r in rows:
                d = dict(r)
                for jf in ('torneios', 'torneios_excluir', 'whitelist_pares',
                           'blacklist_pares', 'whitelist_cenarios', 'filtros'):
                    v = d.get(jf)
                    if isinstance(v, str):
                        try:
                            d[jf] = json.loads(v)
                        except Exception:
                            pass
                for nf in ('linha_min', 'linha_max', 'odd_min', 'odd_max'):
                    v = d.get(nf)
                    if isinstance(v, Decimal):
                        d[nf] = float(v)
                bots.append(d)

            state.bots_ativos = bots
            state.bots_atualizado_em = agora

            assinatura = ','.join(sorted(str(b['id']) for b in bots))
            if assinatura != state.bots_assinatura:
                if bots:
                    logger.info(f"Bots ativos: {len(bots)} -> {[b['nome'] for b in bots]}")
                else:
                    logger.info("Nenhum bot ativo")
                state.bots_assinatura = assinatura

            return bots
        except Exception as e:
            logger.exception(f"Erro carregando bots ativos: {e}")
            return state.bots_ativos


def _get_h2h_cache(casa: str, esporte_banco: str) -> H2HCache:
    global state
    agora = datetime.now()
    if (state.h2h_cache_atualizado_em is None
            or (agora - state.h2h_cache_atualizado_em).total_seconds() > H2H_CACHE_TTL_SEC):
        state.h2h_cache_por_casa = {}
        state.h2h_cache_atualizado_em = agora

    chave = f"{casa}::{esporte_banco}"
    if chave not in state.h2h_cache_por_casa:
        state.h2h_cache_por_casa[chave] = H2HCache(state.pool, casa, esporte_banco)
    return state.h2h_cache_por_casa[chave]


# ============================================================
# PROCESSAMENTO DE TICK
# ============================================================
async def _processar_tick(tick_id: int):
    global state
    state.contador_ticks += 1

    bots = await _carregar_bots_ativos()
    if not bots:
        return

    try:
        async with state.pool.acquire() as conn:
            tick_row = await conn.fetchrow("""
                SELECT id, ts, bookmaker, sport, liga, event_id,
                       jogador_a, jogador_b, time_a, time_b,
                       score_home, score_away, live_time,
                       mercado, mercado_id, mercado_tipo, linha, selecao, selecao_id,
                       odds, evento
                FROM ticks WHERE id = $1
            """, tick_id)
    except Exception as e:
        logger.exception(f"Erro buscando tick {tick_id}: {e}")
        return

    if not tick_row:
        return

    tick = dict(tick_row)

    for bot in bots:
        try:
            await _avaliar_e_apostar(bot, tick)
        except Exception as e:
            logger.exception(f"Erro avaliando bot {bot.get('id')} pra tick {tick_id}: {e}")


def _selecao_normalizada(selecao: str) -> Optional[str]:
    if not selecao:
        return None
    s = selecao.lower().strip()
    if s in ('sim', 'yes'):
        return 'sim'
    if s in ('nao', 'não', 'no'):
        return 'nao'
    if any(w in s for w in ['mais', 'over', 'acima']) or s.startswith('+'):
        return 'over'
    if any(w in s for w in ['menos', 'under', 'abaixo']) or s.startswith('-'):
        return 'under'
    if 'casa' in s or 'home' in s:
        return 'casa'
    if 'empate' in s or 'draw' in s:
        return 'empate'
    if 'fora' in s or 'visitante' in s or 'away' in s:
        return 'fora'
    if s in ('par', 'even'):
        return 'par'
    if s in ('impar', 'ímpar', 'odd'):
        return 'impar'
    return None


def _selecao_eh_over_under(selecao: str) -> Optional[str]:
    lado = _selecao_normalizada(selecao)
    if lado in ('over', 'sim'):
        return 'over'
    if lado in ('under', 'nao'):
        return 'under'
    return None


async def _avaliar_e_apostar(bot: dict, tick: dict):
    """v5: unifica filtros comp + hist e SEMPRE calcula stats_h2h se tiver filtros."""
    global state

    casa_bot = bot.get('casa', '').lower()
    if (tick.get('bookmaker') or '').lower() != casa_bot:
        return

    esporte_bot = bot.get('esporte', '')
    sport_banco = ESPORTE_UI_PARA_BANCO.get(esporte_bot, esporte_bot)
    if (tick.get('sport') or '') != sport_banco:
        return

    liga_traduzida = traduzir_liga(
        (tick.get('bookmaker') or '').lower(),
        tick.get('liga') or ''
    )

    torneios_whitelist = bot.get('torneios') or []
    if torneios_whitelist:
        liga_tick = liga_traduzida.lower()
        if not any(t.lower() in liga_tick for t in torneios_whitelist):
            return

    torneios_blacklist = bot.get('torneios_excluir') or []
    if torneios_blacklist:
        liga_tick = liga_traduzida.lower()
        if any(t.lower() in liga_tick for t in torneios_blacklist):
            return

    filtros = bot.get('filtros') or {}
    lados_bot = filtros.get('lados')
    if lados_bot is None and filtros.get('lado'):
        lado_str = filtros.get('lado').lower()
        if lado_str == 'ambos':
            lados_bot = []
        else:
            lados_bot = [lado_str]
    if lados_bot and isinstance(lados_bot, list) and len(lados_bot) > 0:
        lados_bot_norm = [str(l).lower().strip() for l in lados_bot if l]
        selecao_lado = _selecao_normalizada(tick.get('selecao'))
        if selecao_lado is not None and selecao_lado not in lados_bot_norm:
            state.contador_rejeicoes['lado'] = state.contador_rejeicoes.get('lado', 0) + 1
            return

    # ===== filtro de LADO do HANDICAP (+ / -) =====
    # Pro HC, o "lado" nao e over/under, e o SINAL do handicap: '+' (zebra
    # recebe) ou '-' (favorito da). O bot define isso em filtros['hc_lado']
    # ('+', '-' ou 'ambos'/None). Se definido, rejeita o tick do lado errado.
    # Ex: hc_lado='+' -> so aposta Kiev (+5.5), nunca Bangkok (-5.5).
    if _mercado_eh_hc(bot.get('mercado', '')):
        hc_lado = (filtros.get('hc_lado') or '').strip()
        if hc_lado in ('+', '-'):
            val = _selecao_hc_valor(tick.get('selecao'))
            if val is not None:
                sinal_tick = '+' if val > 0 else '-'
                if sinal_tick != hc_lado:
                    state.contador_rejeicoes['lado'] = state.contador_rejeicoes.get('lado', 0) + 1
                    return

    passou, motivo = _avaliar_filtros_basicos(tick, bot)
    if not passou:
        state.contador_rejeicoes['basico'] = state.contador_rejeicoes.get('basico', 0) + 1
        return

    cenario_ativo = filtros.get('cenarioPartidaAtivo', False)
    cenario_partida = filtros.get('cenarioPartida') if cenario_ativo else None
    diff_ativo = filtros.get('diferencaPlacarAtivo', False)
    diff_min = filtros.get('diferencaPlacar', 0) if diff_ativo else 0

    # v5: unifica filtros comp + hist
    filtros_unificados = _coletar_todos_filtros(filtros)

    if cenario_partida:
        if not _aplicar_filtro_cenario(tick, cenario_partida):
            state.contador_rejeicoes['cenario'] = state.contador_rejeicoes.get('cenario', 0) + 1
            return

    if diff_ativo and diff_min > 0:
        if not _aplicar_filtro_diff_placar(tick, diff_min):
            state.contador_rejeicoes['diff'] = state.contador_rejeicoes.get('diff', 0) + 1
            return

    # v5: SEMPRE calcula stats_h2h se tiver qualquer filtro
    stats_dict = None
    if filtros_unificados:
        ja = tick.get('jogador_a')
        jb = tick.get('jogador_b')
        if not ja or not jb:
            state.contador_rejeicoes['sem_par'] = state.contador_rejeicoes.get('sem_par', 0) + 1
            return

        h2h_cache = _get_h2h_cache(casa_bot, sport_banco)
        jogos_h2h = await h2h_cache.get_jogos(ja, jb, tick['ts'], event_id_excluir=tick.get('event_id'))
        linha_num = _parse_linha(tick.get('linha')) or 0

        mercado_bot_hc = bot.get('mercado', '')
        # ===== RAMO HANDICAP (ah_ft) - MESMA logica do backtest (nao diverge) =====
        if _mercado_eh_hc(mercado_bot_hc):
            stats_dict = calcular_stat_hc(jogos_h2h, tick.get('selecao', ''), ja, jb)
            stats_dict['linha_atual'] = linha_num
            stats_dict['qtd_h2h'] = stats_dict.get('hc_pct_qtd', 0)

            # config do filtro HC: campos dedicados > filtro historico da UI > default
            hc_min = bot.get('hc_pct_min', bot.get('hc_wr_min'))
            hc_max = bot.get('hc_pct_max')
            hc_min_part = bot.get('hc_min_partidas')
            if hc_min is None and hc_max is None:
                for _f in (filtros_unificados or []):
                    if (_f.get('tipo') or '').lower() == 'wr':
                        _mn = _f.get('min') if _f.get('minAtivo') else None
                        _mx = _f.get('max') if _f.get('maxAtivo') else None
                        if _mn is not None:
                            _v, _e = _num_seguro(_mn)
                            hc_min = (_v / 100.0) if (_e is None and _v > 1) else _v
                        if _mx is not None:
                            _v, _e = _num_seguro(_mx)
                            hc_max = (_v / 100.0) if (_e is None and _v > 1) else _v
                        if hc_min_part is None:
                            hc_min_part = _f.get('hist_min_partidas')
                        break
            if hc_min is None and hc_max is None:
                hc_min = 0.87
            try:
                hc_min_part = int(hc_min_part if hc_min_part is not None else 20)
            except (TypeError, ValueError):
                hc_min_part = 20

            # FILTROS 6 e 7: blacklist de zebra / favorito (HC).
            _blz = (bot.get('filtros') or {}).get('blacklist_zebra')
            _blf = (bot.get('filtros') or {}).get('blacklist_favorito')
            _bloq, _mot_bl = _hc_blacklist_bloqueia(
                tick.get('selecao', ''), ja, jb, _blz, _blf)
            if _bloq:
                state.contador_rejeicoes['hc_blacklist'] = state.contador_rejeicoes.get('hc_blacklist', 0) + 1
                return

            passou_hc, motivo_hc = _ramo_hc_pct(stats_dict, hc_min, hc_max, hc_min_part)
            if not passou_hc:
                if 'insuf' in motivo_hc:
                    state.contador_rejeicoes['h2h_insuf'] = state.contador_rejeicoes.get('h2h_insuf', 0) + 1
                else:
                    state.contador_rejeicoes['comp'] = state.contador_rejeicoes.get('comp', 0) + 1
                return

        # ===== RAMO OVER/UNDER (comportamento original, intacto) =====
        else:
            janelas_wr, janelas_media = _extrair_janelas_dos_filtros(filtros_unificados)
            # v9: passa o LADO (over/under) pra calcular o WR do lado APOSTADO.
            # Antes o WR era sempre do over e usado nos 2 lados - um under apitava
            # olhando o WR do over (ex: over 65% -> under apitava como se fosse 65%,
            # quando o under real era 35%). Agora o filtro checa o numero certo.
            lado_aposta = _selecao_eh_over_under(tick.get('selecao'))
            # v10: ts_ref = tick['ts'] (momento da aposta) p/ janelas de tempo (24h/7d).
            stats_dict = _calcular_stats_h2h(jogos_h2h, linha_num, janelas_wr, janelas_media, lado=lado_aposta, ts_ref=tick.get('ts'))
            stats_dict['linha_atual'] = linha_num

            passou_comp, motivo = _aplicar_filtros_complementares(stats_dict, filtros_unificados)
            if not passou_comp:
                if 'h2h_insuficiente' in motivo:
                    state.contador_rejeicoes['h2h_insuf'] = state.contador_rejeicoes.get('h2h_insuf', 0) + 1
                else:
                    state.contador_rejeicoes['comp'] = state.contador_rejeicoes.get('comp', 0) + 1
                return

    # v7: evitar linhas em sequencia - bloqueia se ja apitou QUALQUER linha
    # do MESMO mercado_tipo nesse event_id (1 aposta maxima por mercado por jogo).
    # Antes (v6): tinha `AND linha = $3` que so bloqueava a MESMA linha exata,
    # permitindo apitar Over 1.5 depois de Over 2.5 no mesmo jogo.
    # Default = true (legado: bots sem flag definida -> ativo).
    evitar_linhas_seq = filtros.get('evitarLinhasSeq', True)
    if evitar_linhas_seq:
        mercado_tipo_atual = tick.get('mercado_tipo')
        async with state.pool.acquire() as conn:
            ja_apostou = await conn.fetchval("""
                SELECT COUNT(*) FROM apostas
                WHERE bot_id = $1
                  AND event_id = $2
                  AND modo = 'simulado'
                  AND (mercado_tipo = $3 OR ($3 IS NULL AND mercado_tipo IS NULL))
            """, bot['id'], tick.get('event_id'), mercado_tipo_atual)
        if ja_apostou and ja_apostou > 0:
            state.contador_rejeicoes['mercado_repetido'] = state.contador_rejeicoes.get('mercado_repetido', 0) + 1
            return

    max_apostas = bot.get('max_apostas_partida')
    if max_apostas:
        async with state.pool.acquire() as conn:
            count = await conn.fetchval("""
                SELECT COUNT(*) FROM apostas
                WHERE bot_id = $1 AND event_id = $2 AND modo = 'simulado'
            """, bot['id'], tick.get('event_id'))
        if count and count >= max_apostas:
            state.contador_rejeicoes['cap_jogo'] = state.contador_rejeicoes.get('cap_jogo', 0) + 1
            return

    motivo = _montar_motivo(bot, tick, stats_dict, filtros_unificados)
    await _registrar_aposta(bot, tick, stats_dict, motivo, liga_traduzida)


def _montar_motivo(bot: dict, tick: dict, stats: Optional[dict], filtros_unificados: list) -> str:
    """v5: usa filtros_unificados (comp + hist normalizado)."""
    partes = []
    filtros = bot.get('filtros') or {}

    if stats and filtros_unificados:
        for fc in filtros_unificados:
            if not isinstance(fc, dict):
                continue
            tipo = (fc.get('tipo') or '').lower().strip()
            janela = fc.get('janela')
            origem = fc.get('_origem', 'comp')
            if not tipo:
                continue

            if tipo in ('media', 'wr') and janela is not None:
                stat_key = f"{tipo}_ult{janela}"
                if origem == 'hist':
                    rotulo = f"WR{janela}H2H" if tipo == 'wr' else f"média{janela}H2H"
                else:
                    rotulo = f"{'WR' if tipo == 'wr' else 'média'}{janela}"
            else:
                stat_key = tipo
                rotulo = tipo

            # gap_media/gap: gap = media_ult{janela} - linha, EM CADA JANELA.
            # (antes pegava 'gap' fixo de 20 ou stat_key inexistente 'gap_media')
            if tipo in ('gap_media', 'gap'):
                linha_g = stats.get('linha_atual')
                if janela is not None:
                    media_jan = stats.get(f"media_ult{janela}")
                    if media_jan is not None and linha_g is not None:
                        partes.append(f"gap{janela}={media_jan - linha_g:+.1f}")
                else:
                    g = stats.get('gap')
                    if g is not None:
                        partes.append(f"gap={float(g):+.1f}")
                continue

            valor = stats.get(stat_key)
            if valor is None:
                continue

            try:
                v = float(valor)
                if tipo == 'wr':
                    partes.append(f"{rotulo}={v * 100:.0f}%")
                elif tipo == 'gap':
                    partes.append(f"gap={v:+.1f}")
                elif tipo == 'qtd_h2h':
                    partes.append(f"H2H={int(v)}")
                elif tipo == 'tendencia':
                    partes.append(f"tend={v:+.2f}")
                else:
                    partes.append(f"{rotulo}={v:.1f}")
            except (TypeError, ValueError):
                partes.append(f"{rotulo}={valor}")

    if filtros.get('cenarioPartidaAtivo'):
        cen = filtros.get('cenarioPartida')
        if cen:
            partes.append(str(cen))
    else:
        lt = tick.get('live_time') or ''
        if lt:
            primeiro = str(lt).strip().split()[0] if str(lt).strip() else ''
            if primeiro and any(c.isalpha() for c in primeiro):
                partes.append(primeiro)

    if filtros.get('diferencaPlacarAtivo'):
        diff_min = filtros.get('diferencaPlacar')
        if diff_min:
            partes.append(f"diff>={diff_min}")

    # pro HC, mostra a linha REAL (com sinal, da selecao), nao a coluna do tick
    if _mercado_eh_hc(bot.get('mercado', '')):
        linha_num = _selecao_hc_valor(tick.get('selecao'))
        if linha_num is None:
            linha_num = _parse_linha(tick.get('linha'))
    else:
        linha_num = _parse_linha(tick.get('linha'))
    if linha_num is not None:
        partes.append(f"L={linha_num}")

    if not partes:
        return "filtros basicos OK"

    return " · ".join(partes)


async def _registrar_aposta(bot: dict, tick: dict, stats: Optional[dict], motivo: Optional[str] = None, liga_traduzida: Optional[str] = None):
    global state

    # LINHA GRAVADA NA APOSTA.
    # Pro HANDICAP, a coluna 'linha' do tick NAO e confiavel: a superbet manda o
    # sinal invertido/ausente (ex: selecao '(13.5)' com coluna '-13.50'). O valor
    # verdadeiro (com sinal do lado apostado) esta na SELECAO. Grava o valor certo
    # pra que Telegram, historico e analises leiam a linha real da aposta.
    # Fallback pra coluna se a selecao nao tiver valor (blindado).
    if _mercado_eh_hc(bot.get('mercado', '')):
        linha_num = _selecao_hc_valor(tick.get('selecao'))
        if linha_num is None:
            linha_num = _parse_linha(tick.get('linha'))
    else:
        linha_num = _parse_linha(tick.get('linha'))

    # mesmo flag do _avaliar_e_apostar, recalculado aqui (escopo proprio).
    # Default true (legado). Usado na guarda atomica do INSERT contra over+under.
    _filtros_bot = bot.get('filtros') or {}
    if isinstance(_filtros_bot, str):
        try:
            _filtros_bot = json.loads(_filtros_bot)
        except Exception:
            _filtros_bot = {}
    evitar_linhas_seq = _filtros_bot.get('evitarLinhasSeq', True)

    try:
        async with state.pool.acquire() as conn:
            # v8: INSERT ... SELECT ... WHERE NOT EXISTS fecha a RACE CONDITION
            # do over+under. Antes, a checagem evitar_linhas_seq rodava ANTES da
            # gravacao; quando os 2 ticks (over E under) do mesmo mercado/jogo
            # chegavam quase juntos, ambos viam "0 apostas" e os 2 entravam
            # (1 GREEN + 1 RED garantidos, lucro zero, msg duplicada = flood).
            # Agora a verificacao e ATOMICA: o 2o lado bate no NOT EXISTS e nao
            # entra. So aplica quando evitar_linhas_seq esta ativo (parametro $22).
            row = await conn.fetchrow("""
                INSERT INTO apostas (
                    bot_id, modo, status,
                    casa, esporte, torneio, event_id,
                    jogador_a, jogador_b,
                    mercado, mercado_tipo, linha, selecao, lado,
                    odd, stake,
                    placar_a_entrada, placar_b_entrada,
                    score_home_no_momento, score_away_no_momento,
                    live_time, tick_id, bookmaker, liga,
                    stats_h2h, motivo,
                    apostado_em
                )
                SELECT
                    $1, 'simulado', 'pendente',
                    $2, $3, $4, $5,
                    $6, $7,
                    $8, $9, $10, $11, $11,
                    $12, $13,
                    $14, $15,
                    $14, $15,
                    $16, $17, $18, $19,
                    $20::jsonb, $21,
                    NOW()
                WHERE (NOT ($22::boolean) OR NOT EXISTS (
                    SELECT 1 FROM apostas
                    WHERE bot_id = $1
                      AND event_id = $5
                      AND modo = 'simulado'
                      AND (mercado_tipo = $9 OR ($9 IS NULL AND mercado_tipo IS NULL))
                ))
                  -- FIX dedup por linha: NUNCA aposta a mesma (evento+mercado+linha)
                  -- 2x, mesmo com evitarLinhasSeq=false. Permite linhas DIFERENTES
                  -- (13.5, 14.5, ...) mas bloqueia a repetida (duas 13.5).
                  AND NOT EXISTS (
                    SELECT 1 FROM apostas
                    WHERE bot_id = $1
                      AND event_id = $5
                      AND modo = 'simulado'
                      AND (mercado_tipo = $9 OR ($9 IS NULL AND mercado_tipo IS NULL))
                      AND linha = $10
                )
                ON CONFLICT (bot_id, tick_id) WHERE tick_id IS NOT NULL DO NOTHING
                RETURNING id
            """,
                bot['id'],
                bot.get('casa'), bot.get('esporte'),
                liga_traduzida or tick.get('liga'), tick.get('event_id'),
                tick.get('jogador_a'), tick.get('jogador_b'),
                bot.get('mercado'), tick.get('mercado_tipo'),
                linha_num, tick.get('selecao'),
                float(tick['odds']) if tick.get('odds') else None,
                STAKE_DEFAULT,
                tick.get('score_home'), tick.get('score_away'),
                tick.get('live_time'),
                tick.get('id'), tick.get('bookmaker'), liga_traduzida or tick.get('liga'),
                _to_jsonb(stats), motivo,
                bool(evitar_linhas_seq),
            )

        if row:
            state.contador_apostas += 1
            logger.info(
                f"✅ APOSTA #{row['id']} | bot={bot['nome']} | "
                f"{tick.get('jogador_a')} vs {tick.get('jogador_b')} | "
                f"linha={linha_num} {tick.get('selecao')} @ {tick.get('odds')} | "
                f"motivo: {motivo or '-'}"
            )
            # v10: backfill sob demanda - enfileira o par pra completar o h2h
            # na DB via TM. Fire-and-forget (nao trava o apito). Blindado.
            try:
                from tm_backfill import enfileirar_par
                enfileirar_par(
                    bot.get('casa'), bot.get('esporte'),
                    tick.get('jogador_a'), tick.get('jogador_b'),
                    bot_id=bot.get('id'))
            except Exception:
                pass  # backfill nunca afeta a aposta
    except Exception as e:
        logger.exception(f"Erro registrando aposta bot={bot['id']} tick={tick.get('id')}: {e}")


# ============================================================
# RESOLVER
# ============================================================
async def _resolver_apostas_pendentes():
    global state
    try:
        async with state.pool.acquire() as conn:
            apostas = await conn.fetch("""
                SELECT a.id, a.bot_id, a.event_id, a.bookmaker, a.mercado,
                       a.linha, a.selecao, a.lado, a.odd, a.stake,
                       a.jogador_a, a.jogador_b
                FROM apostas a
                WHERE a.status = 'pendente'
                  AND a.modo = 'simulado'
                  AND a.apostado_em < NOW() - INTERVAL '15 minutes'
                ORDER BY a.apostado_em
                LIMIT 500
            """)

            if not apostas:
                return

            logger.info(f"Resolvendo {len(apostas)} apostas pendentes...")
            resolvidas = 0

            for ap in apostas:
                placar = await conn.fetchrow("""
                    SELECT score_home, score_away
                    FROM ticks
                    WHERE event_id = $1
                      AND bookmaker = $2
                      AND score_home IS NOT NULL
                      AND score_away IS NOT NULL
                    ORDER BY ts DESC
                    LIMIT 1
                """, ap['event_id'], ap['bookmaker'])

                if not placar:
                    continue

                ultimo_tick = await conn.fetchval("""
                    SELECT MAX(ts) FROM ticks
                    WHERE event_id = $1 AND bookmaker = $2
                """, ap['event_id'], ap['bookmaker'])

                if not ultimo_tick:
                    continue
                if (datetime.now(ultimo_tick.tzinfo) - ultimo_tick).total_seconds() < 180:
                    continue

                # ===== resolucao HANDICAP por NICK (isolada) =====
                if _mercado_eh_hc(ap['mercado']):
                    resultado = _resolve_resultado_hc(
                        ap['selecao'] or ap['lado'],
                        ap.get('jogador_a'), ap.get('jogador_b'),
                        placar['score_home'], placar['score_away']
                    )
                else:
                    resultado = _resolve_resultado(
                        ap['mercado'], ap['selecao'] or ap['lado'],
                        float(ap['linha']) if ap['linha'] else None,
                        placar['score_home'], placar['score_away']
                    )

                if resultado is None:
                    continue

                stake = float(ap['stake'])
                odd = float(ap['odd'])
                if resultado == 'green':
                    pnl = stake * (odd - 1)
                elif resultado == 'red':
                    pnl = -stake
                else:
                    pnl = 0

                await conn.execute("""
                    UPDATE apostas SET
                        status = 'resolvida',
                        resultado = $2,
                        placar_final_a = $3,
                        placar_final_b = $4,
                        pnl = $5,
                        lucro_unidades = $6,
                        resolvido_em = NOW()
                    WHERE id = $1
                """, ap['id'], resultado,
                    placar['score_home'], placar['score_away'],
                    round(pnl, 2),
                    round(pnl / stake, 4)
                )
                resolvidas += 1

            if resolvidas:
                logger.info(f"✅ Resolvidas: {resolvidas}/{len(apostas)}")

    except Exception as e:
        logger.exception(f"Erro resolvendo apostas: {e}")


# ============================================================
# LOOPS
# ============================================================
async def loop_listener():
    while not state.parar:
        try:
            conn = await asyncpg.connect(DB_DSN)

            async def callback(connection, pid, channel, payload):
                try:
                    tick_id = int(payload)
                    asyncio.create_task(_processar_tick(tick_id))
                except Exception as e:
                    logger.exception(f"Erro no callback de NOTIFY: {e}")

            await conn.add_listener(CHANNEL, callback)
            logger.info(f"✅ LISTEN {CHANNEL} ativo - aguardando ticks live...")

            while not state.parar:
                await asyncio.sleep(10)
                try:
                    await conn.execute("SELECT 1")
                except Exception:
                    logger.warning("Conexao listener morreu, reconectando...")
                    break

            await conn.remove_listener(CHANNEL, callback)
            await conn.close()

        except Exception as e:
            logger.exception(f"Erro no listener: {e}")
            if not state.parar:
                logger.info("Reconectando em 5s...")
                await asyncio.sleep(5)


async def loop_resolver():
    while not state.parar:
        try:
            await _resolver_apostas_pendentes()
        except Exception as e:
            logger.exception(f"Erro no loop_resolver: {e}")
        await asyncio.sleep(RESOLVER_INTERVAL_SEC)


async def loop_stats():
    while not state.parar:
        await asyncio.sleep(60)
        rej_str = ', '.join(f'{k}={v}' for k, v in state.contador_rejeicoes.items() if v > 0) or '-'
        logger.info(
            f"📊 ticks_processados={state.contador_ticks} | "
            f"apostas_geradas={state.contador_apostas} | "
            f"bots_ativos={len(state.bots_ativos)} | "
            f"rejeicoes={rej_str}"
        )


# ============================================================
# MAIN
# ============================================================
async def main():
    logger.info("=" * 60)
    logger.info("Bot Executor iniciando (v7 - fix evitar_linhas_seq: 1 aposta max por mercado/jogo)...")
    logger.info("=" * 60)

    state.bots_lock = asyncio.Lock()

    state.pool = await asyncpg.create_pool(
        DB_DSN,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )
    logger.info(f"✅ Pool de conexoes criado (min=2, max=10)")

    await _carregar_bots_ativos()

    # v10: worker de backfill sob demanda (preenche h2h do par que apita).
    # Blindado: se o modulo nao existir ou falhar, o executor segue normal.
    try:
        from tm_backfill import iniciar_worker
        await iniciar_worker(pool=state.pool)
        logger.info("✅ Worker de backfill TM iniciado (preenche par que apita)")
    except Exception as e:
        logger.warning(f"Backfill TM nao iniciou (segue sem ele): {e}")

    tasks = [
        asyncio.create_task(loop_listener()),
        asyncio.create_task(loop_resolver()),
        asyncio.create_task(loop_stats()),
    ]

    def shutdown_handler(signum, frame):
        logger.info(f"Recebido sinal {signum}, parando...")
        state.parar = True

    try:
        signal.signal(signal.SIGINT, shutdown_handler)
        signal.signal(signal.SIGTERM, shutdown_handler)
    except Exception:
        pass

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        logger.info("Fechando pool...")
        await state.pool.close()
        logger.info("Bot Executor parado.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrompido pelo usuario.")
