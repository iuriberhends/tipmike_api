"""
bot_executor.py - Worker de simulacao em tempo real

ARQUITETURA:
- Postgres LISTEN tick_novo (push real-time, latencia ~10-50ms)
- Carrega bots ativos em cache (refresh a cada 30s)
- Pra cada tick novo: busca o tick, aplica filtros de cada bot ativo
- Se passa: INSERT INTO apostas (modo='simulado', status='pendente')
- Loop paralelo: resolve apostas pendentes a cada 5min (busca placar final)

REUSA: todas as funcoes de filtragem do backtest_runner.py
       (mesma logica = simulacao consistente com backtest)

EXECUCAO:
- Roda como servico Windows via NSSM
- python bot_executor.py
- Logs: C:\\bot_executor.log

DEPENDENCIAS: asyncpg (ja instalado no venv da API)
"""

import asyncio
import json
import logging
import signal
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Any

import asyncpg

# Reusa funcoes do worker de backtest
# (assumindo que bot_executor.py fica em workers/, junto com backtest_runner.py)
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
    ESPORTE_UI_PARA_BANCO,
    MIN_H2H_DEFAULT,
)


# ============================================================
# TRADUTORES DE LIGA - converte IDs/codigos brutos -> nome humano
# ============================================================
# Espelha o que existe em routers/torneios.py v4. Mantem os 2 sincronizados.
# Cobre casos onde o coletor caiu no fallback (Superbet com IDs novos) ou
# nao traduz (Bet365 grava codigo cru sempre).
# ============================================================

SUPERBET_ID_TO_NAME = {
    # FIFA (E-Football)
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
    # NBA (E-Basketball)
    "75124": "Battle - NBA 1",
    "80566": "NBA League 2x4",
    "89069": "EAL - NextGen",
    "92679": "European Conference 4x5",
}

BET365_CODE_TO_NAME = {
    # FIFA (E-Football)
    "ESOC-GTL-12MP":   "GT Leagues - 2x6",
    "ESOCH2HGG-8MP":   "H2H GG League - 2x4",
    "ESOCBATVOL-6":    "Battle Volta - 2x3",
    "ESOCCERBATTLE":   "Battle - 2x4",
    # NBA (E-Basketball)
    "B-EBASKBLITZ4X5": "H2H GG League - 4x5",
    "B-EBASKBAT4X5":   "Battle - 5x5",
}


# Aliases de torneios renomeados pelas casas
SUPERBET_ALIASES = {
    "Live Arena": "Cyber Live Arena",
}


def traduzir_liga(bookmaker: str, liga: str) -> str:
    """Traduz ID/codigo bruto pra nome humano. Se nao tem mapeamento, devolve igual."""
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
BOT_CACHE_REFRESH_SEC = 30        # refresh dos bots ativos a cada 30s
RESOLVER_INTERVAL_SEC = 300        # resolve placares a cada 5min
H2H_CACHE_TTL_SEC = 300            # cache de H2H valida por 5min
STAKE_DEFAULT = Decimal('10.00')   # stake padrao em R$

# Logging - forca UTF-8 no stdout pra emojis nao quebrarem (Python 3.14 + Windows = cp1252)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger('bot_executor')


# ============================================================
# ESTADO GLOBAL
# ============================================================
class State:
    pool: Optional[asyncpg.Pool] = None
    bots_ativos: list = []                # cache dos bots
    bots_atualizado_em: Optional[datetime] = None
    bots_lock: Optional[asyncio.Lock] = None  # evita race condition ao refresh
    bots_assinatura: str = ""             # hash dos ids dos bots, pra logar so quando muda
    h2h_cache_por_casa: dict = {}         # {casa: H2HCache}
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
    """Serializa pra JSONB."""
    if value is None:
        return None
    if isinstance(value, (list, dict)) and len(value) == 0:
        return None
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False, default=str)


async def _carregar_bots_ativos():
    """
    Carrega bots com status='ativo' do banco. Cache por 30s.
    Usa lock pra evitar race condition (queries paralelas).
    Loga apenas quando a lista de bots ativos MUDA (entrou/saiu bot).
    """
    global state

    # Fast-path: cache valido, retorna sem lock
    agora = datetime.now()
    if (state.bots_atualizado_em
            and (agora - state.bots_atualizado_em).total_seconds() < BOT_CACHE_REFRESH_SEC):
        return state.bots_ativos

    # Slow-path: precisa atualizar - usa lock pra so 1 task fazer a query
    if state.bots_lock is None:
        state.bots_lock = asyncio.Lock()

    async with state.bots_lock:
        # Re-check apos pegar o lock (outra task pode ja ter atualizado)
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

            # Log apenas quando a lista MUDA (evita spam)
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
            return state.bots_ativos  # retorna cache antigo se falhar


def _get_h2h_cache(casa: str, esporte_banco: str) -> H2HCache:
    """Retorna H2HCache pra essa casa+esporte. Limpa cache global a cada 5min."""
    global state
    agora = datetime.now()
    if (state.h2h_cache_atualizado_em is None
            or (agora - state.h2h_cache_atualizado_em).total_seconds() > H2H_CACHE_TTL_SEC):
        # Reset do cache (evita memoria infinita)
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
    """
    Processa 1 tick: busca dados, aplica filtros de cada bot ativo,
    insere aposta_simulada se passar.
    """
    global state
    state.contador_ticks += 1

    # 1. Carrega bots ativos (cached)
    bots = await _carregar_bots_ativos()
    if not bots:
        return  # sem bots, nada a fazer

    # 2. Busca o tick
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
        return  # tick sumiu (raro, mas possivel se houver concorrencia)

    tick = dict(tick_row)

    # 3. Pra cada bot ativo, testa se este tick passa nos filtros
    for bot in bots:
        try:
            await _avaliar_e_apostar(bot, tick)
        except Exception as e:
            logger.exception(f"Erro avaliando bot {bot.get('id')} pra tick {tick_id}: {e}")



def _selecao_normalizada(selecao: str) -> Optional[str]:
    """
    Normaliza o texto da selecao do tick pra um lado canonico.
    Reconhece varios mercados:
    - Mais/Over/Acima/+/Sim -> 'over' ou 'sim'
    - Menos/Under/Abaixo/-/Nao -> 'under' ou 'nao'
    - Casa -> 'casa'
    - Empate -> 'empate'
    - Fora/Visitante -> 'fora'
    - Par -> 'par'
    - Impar -> 'impar'
    Retorna None se nao reconhecer (deixa passar).
    """
    if not selecao:
        return None
    s = selecao.lower().strip()
    # BTTS (Ambos Marcam): Sim/Nao
    if s in ('sim', 'yes'):
        return 'sim'
    if s in ('nao', 'não', 'no'):
        return 'nao'
    # Over/Under
    if any(w in s for w in ['mais', 'over', 'acima']) or s.startswith('+'):
        return 'over'
    if any(w in s for w in ['menos', 'under', 'abaixo']) or s.startswith('-'):
        return 'under'
    # ML: Casa/Empate/Fora
    if 'casa' in s or 'home' in s:
        return 'casa'
    if 'empate' in s or 'draw' in s:
        return 'empate'
    if 'fora' in s or 'visitante' in s or 'away' in s:
        return 'fora'
    # Par/Impar
    if s in ('par', 'even'):
        return 'par'
    if s in ('impar', 'ímpar', 'odd'):
        return 'impar'
    return None


# Mantido por compatibilidade (chamado por patch v1 antigo, se ainda existir)
def _selecao_eh_over_under(selecao: str) -> Optional[str]:
    lado = _selecao_normalizada(selecao)
    if lado in ('over', 'sim'):
        return 'over'
    if lado in ('under', 'nao'):
        return 'under'
    return None


async def _avaliar_e_apostar(bot: dict, tick: dict):
    """
    Aplica todos os filtros do bot ao tick. Se passar, registra aposta simulada.
    Mesma logica do backtest_runner._aplicar_filtros + cenario + diff + comp.
    """
    global state

    # 0. Filtros de pre-condicao basicos (casa, esporte, liga)
    casa_bot = bot.get('casa', '').lower()
    if (tick.get('bookmaker') or '').lower() != casa_bot:
        return

    esporte_bot = bot.get('esporte', '')
    sport_banco = ESPORTE_UI_PARA_BANCO.get(esporte_bot, esporte_bot)
    if (tick.get('sport') or '') != sport_banco:
        return

    # v4: traduz tick.liga (ID/codigo bruto) pra nome humano antes do match.
    # Cobre Bet365 (sempre grava codigo cru) e Superbet (alguns IDs caem fallback).
    # Bot guarda torneios com nome humano vindo do dropdown, entao precisa bater.
    liga_traduzida = traduzir_liga(
        (tick.get('bookmaker') or '').lower(),
        tick.get('liga') or ''
    )

    # Torneios (whitelist)
    torneios_whitelist = bot.get('torneios') or []
    if torneios_whitelist:
        liga_tick = liga_traduzida.lower()
        if not any(t.lower() in liga_tick for t in torneios_whitelist):
            return

    # Torneios excluir (blacklist)
    torneios_blacklist = bot.get('torneios_excluir') or []
    if torneios_blacklist:
        liga_tick = liga_traduzida.lower()
        if any(t.lower() in liga_tick for t in torneios_blacklist):
            return

    # 0.5. Filtro de LADOS (array) - bot pode operar 1+ lados, ou nenhum (= ambos)
    filtros = bot.get('filtros') or {}
    lados_bot = filtros.get('lados')
    # Compatibilidade com patch v1: se nao tem 'lados' mas tem 'lado' string, converte
    if lados_bot is None and filtros.get('lado'):
        lado_str = filtros.get('lado').lower()
        if lado_str == 'ambos':
            lados_bot = []
        else:
            lados_bot = [lado_str]
    # Normaliza pra lista de strings lowercase
    if lados_bot and isinstance(lados_bot, list) and len(lados_bot) > 0:
        lados_bot_norm = [str(l).lower().strip() for l in lados_bot if l]
        selecao_lado = _selecao_normalizada(tick.get('selecao'))
        # Se reconhecemos o lado do tick E ele NAO esta nos lados aceitos, rejeita
        if selecao_lado is not None and selecao_lado not in lados_bot_norm:
            state.contador_rejeicoes['lado'] = state.contador_rejeicoes.get('lado', 0) + 1
            return
    # Se lados_bot eh None, [] ou nao-lista, aceita qualquer (default ambos)

    # 1. Filtros basicos (linha, odds, mercado, blacklist/whitelist pares)
    passou, motivo = _avaliar_filtros_basicos(tick, bot)
    if not passou:
        state.contador_rejeicoes['basico'] = state.contador_rejeicoes.get('basico', 0) + 1
        return

    # 2. Filtros do JSONB filtros
    filtros = bot.get('filtros') or {}
    cenario_ativo = filtros.get('cenarioPartidaAtivo', False)
    cenario_partida = filtros.get('cenarioPartida') if cenario_ativo else None
    diff_ativo = filtros.get('diferencaPlacarAtivo', False)
    diff_min = filtros.get('diferencaPlacar', 0) if diff_ativo else 0
    filtros_comp = filtros.get('filtrosCompAdicionados') or []

    if cenario_partida:
        if not _aplicar_filtro_cenario(tick, cenario_partida):
            state.contador_rejeicoes['cenario'] = state.contador_rejeicoes.get('cenario', 0) + 1
            return

    if diff_ativo and diff_min > 0:
        if not _aplicar_filtro_diff_placar(tick, diff_min):
            state.contador_rejeicoes['diff'] = state.contador_rejeicoes.get('diff', 0) + 1
            return

    # 3. Filtros complementares (H2H stats)
    stats_dict = None
    if filtros_comp:
        ja = tick.get('jogador_a')
        jb = tick.get('jogador_b')
        if not ja or not jb:
            state.contador_rejeicoes['sem_par'] = state.contador_rejeicoes.get('sem_par', 0) + 1
            return

        h2h_cache = _get_h2h_cache(casa_bot, sport_banco)
        jogos_h2h = await h2h_cache.get_jogos(ja, jb, tick['ts'])
        linha_num = _parse_linha(tick.get('linha')) or 0
        stats_dict = _calcular_stats_h2h(jogos_h2h, linha_num)
        stats_dict['linha_atual'] = linha_num

        passou_comp, motivo = _aplicar_filtros_complementares(stats_dict, filtros_comp)
        if not passou_comp:
            if 'h2h_insuficiente' in motivo:
                state.contador_rejeicoes['h2h_insuf'] = state.contador_rejeicoes.get('h2h_insuf', 0) + 1
            else:
                state.contador_rejeicoes['comp'] = state.contador_rejeicoes.get('comp', 0) + 1
            return

    # 4. Cap de apostas por jogo (verifica se ja tem aposta nesse evento pelo bot)
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

    # ✅ PASSOU EM TODOS OS FILTROS - INSERE APOSTA
    motivo = _montar_motivo(bot, tick, stats_dict)
    await _registrar_aposta(bot, tick, stats_dict, motivo, liga_traduzida)


def _montar_motivo(bot: dict, tick: dict, stats: Optional[dict]) -> str:
    """
    Monta resumo curto (1 linha) dos filtros que bateram pra apitar a tip.
    Exemplo de saida:
      "WR10=100% · média10=4.8 · gap=+1.7 · Q2 · L=2.5"

    Mapeamento confirmado contra dados reais do stats_h2h:
      filtro {tipo:"media", janela:10}  -> stats['media_ult10']
      filtro {tipo:"wr",    janela:5}   -> stats['wr_ult5']
      filtro {tipo:"tendencia"}         -> stats['tendencia']
      filtro {tipo:"gap"}               -> stats['gap']
      filtro {tipo:"qtd_h2h"}           -> stats['qtd_h2h']

    Adiciona cenario, diff de placar e linha pra contexto.
    """
    partes = []
    filtros = bot.get('filtros') or {}

    # --- 1. Stats H2H (so se o bot configurou filtros complementares) ---
    if stats:
        filtros_comp = filtros.get('filtrosCompAdicionados') or []
        for fc in filtros_comp:
            if not isinstance(fc, dict):
                continue
            tipo = (fc.get('tipo') or '').lower().strip()
            janela = fc.get('janela')
            if not tipo:
                continue

            # Resolve a chave do stats_dict
            if tipo in ('media', 'wr') and janela:
                stat_key = f"{tipo}_ult{janela}"
                rotulo = f"{'WR' if tipo == 'wr' else 'média'}{janela}"
            else:
                # gap, tendencia, qtd_h2h, etc - sem janela
                stat_key = tipo
                rotulo = tipo

            valor = stats.get(stat_key)
            if valor is None:
                continue

            # Formata segundo o tipo
            try:
                v = float(valor)
                if tipo == 'wr':
                    # 0.0-1.0 -> porcentagem
                    partes.append(f"{rotulo}={v * 100:.0f}%")
                elif tipo == 'gap':
                    partes.append(f"gap={v:+.1f}")
                elif tipo == 'qtd_h2h':
                    partes.append(f"H2H={int(v)}")
                elif tipo == 'tendencia':
                    partes.append(f"tend={v:+.2f}")
                else:
                    # media e outros numericos
                    partes.append(f"{rotulo}={v:.1f}")
            except (TypeError, ValueError):
                partes.append(f"{rotulo}={valor}")

    # --- 2. Cenario de partida (Q1/Q2/Q3/Q4 ou HT/FT) ---
    if filtros.get('cenarioPartidaAtivo'):
        cen = filtros.get('cenarioPartida')
        if cen:
            partes.append(str(cen))
    else:
        # Tenta inferir periodo do tick (live_time tipo "Q2 5:30")
        lt = tick.get('live_time') or ''
        if lt:
            primeiro = str(lt).strip().split()[0] if str(lt).strip() else ''
            if primeiro and any(c.isalpha() for c in primeiro):
                partes.append(primeiro)

    # --- 3. Diff de placar ---
    if filtros.get('diferencaPlacarAtivo'):
        diff_min = filtros.get('diferencaPlacar')
        if diff_min:
            partes.append(f"diff>={diff_min}")

    # --- 4. Linha (sempre, pra contexto) ---
    linha_num = _parse_linha(tick.get('linha'))
    if linha_num is not None:
        partes.append(f"L={linha_num}")

    if not partes:
        return "filtros basicos OK"

    return " · ".join(partes)


async def _registrar_aposta(bot: dict, tick: dict, stats: Optional[dict], motivo: Optional[str] = None, liga_traduzida: Optional[str] = None):
    """INSERT INTO apostas (modo='simulado', status='pendente')"""
    global state

    linha_num = _parse_linha(tick.get('linha'))

    try:
        async with state.pool.acquire() as conn:
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
                ) VALUES (
                    $1, 'simulado', 'pendente',
                    $2, $3, $4, $5,
                    $6, $7,
                    $8, $9, $10, $11, $11,
                    $12, $13,
                    $14, $14,
                    $14, $15,
                    $16, $17, $18, $19,
                    $20::jsonb, $21,
                    NOW()
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
            )

        if row:
            state.contador_apostas += 1
            logger.info(
                f"✅ APOSTA #{row['id']} | bot={bot['nome']} | "
                f"{tick.get('jogador_a')} vs {tick.get('jogador_b')} | "
                f"linha={linha_num} {tick.get('selecao')} @ {tick.get('odds')} | "
                f"motivo: {motivo or '-'}"
            )
    except Exception as e:
        logger.exception(f"Erro registrando aposta bot={bot['id']} tick={tick.get('id')}: {e}")


# ============================================================
# RESOLVER (preenche placar final + resultado das apostas pendentes)
# ============================================================
async def _resolver_apostas_pendentes():
    """
    Roda a cada 5min. Pra cada aposta pendente, busca o placar final do evento
    no MikeDB (ultimo tick com placar valido). Se jogo terminou, resolve.
    """
    global state
    try:
        async with state.pool.acquire() as conn:
            # Busca apostas pendentes mais antigas que 15min (e-sport FIFA 2x6 = 12min jogo)
            apostas = await conn.fetch("""
                SELECT a.id, a.bot_id, a.event_id, a.bookmaker, a.mercado,
                       a.linha, a.selecao, a.lado, a.odd, a.stake
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
                # Busca placar final (ultimo tick com placar valido pro evento)
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
                    continue  # sem placar ainda

                # Confere se jogo realmente terminou (sem ticks novos ha pelo menos 30min)
                ultimo_tick = await conn.fetchval("""
                    SELECT MAX(ts) FROM ticks
                    WHERE event_id = $1 AND bookmaker = $2
                """, ap['event_id'], ap['bookmaker'])

                if not ultimo_tick:
                    continue
                if (datetime.now(ultimo_tick.tzinfo) - ultimo_tick).total_seconds() < 180:
                    continue  # jogo ainda em andamento

                # Resolve resultado
                resultado = _resolve_resultado(
                    ap['mercado'], ap['selecao'] or ap['lado'],
                    float(ap['linha']) if ap['linha'] else None,
                    placar['score_home'], placar['score_away']
                )

                if resultado is None:
                    # Mercado nao suportado pra resolucao automatica
                    continue

                # Calcula PnL
                stake = float(ap['stake'])
                odd = float(ap['odd'])
                if resultado == 'green':
                    pnl = stake * (odd - 1)
                elif resultado == 'red':
                    pnl = -stake
                else:  # void
                    pnl = 0

                # UPDATE
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
                    round(pnl / stake, 4)  # lucro em unidades
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
    """
    Loop principal: escuta NOTIFY tick_novo e processa cada tick.
    Reconecta automaticamente se a conexao cair.
    """
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

            # Mantem conexao viva
            while not state.parar:
                await asyncio.sleep(10)
                # Heartbeat: confirma conexao
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
    """Loop de resolucao de apostas pendentes - a cada 5min."""
    while not state.parar:
        try:
            await _resolver_apostas_pendentes()
        except Exception as e:
            logger.exception(f"Erro no loop_resolver: {e}")
        await asyncio.sleep(RESOLVER_INTERVAL_SEC)


async def loop_stats():
    """Imprime estatisticas a cada 60s."""
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
    logger.info("Bot Executor iniciando...")
    logger.info("=" * 60)

    # Inicializa lock (precisa estar dentro de event loop)
    state.bots_lock = asyncio.Lock()

    # Pool principal pra queries (fetch tick, insert aposta, resolve, etc)
    state.pool = await asyncpg.create_pool(
        DB_DSN,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )
    logger.info(f"✅ Pool de conexoes criado (min=2, max=10)")

    # Carrega bots ativos
    await _carregar_bots_ativos()

    # Inicia 3 loops em paralelo
    tasks = [
        asyncio.create_task(loop_listener()),
        asyncio.create_task(loop_resolver()),
        asyncio.create_task(loop_stats()),
    ]

    # Signal handlers pra shutdown limpo
    def shutdown_handler(signum, frame):
        logger.info(f"Recebido sinal {signum}, parando...")
        state.parar = True

    try:
        signal.signal(signal.SIGINT, shutdown_handler)
        signal.signal(signal.SIGTERM, shutdown_handler)
    except Exception:
        pass  # Windows pode nao suportar todos sinais

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
