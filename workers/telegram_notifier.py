"""
telegram_notifier.py - Worker de envio de tips pro Telegram
v8 - ANTI-FLOOD: ao resolver aposta, EDITA a mensagem original
     (editMessageText) adicionando um footer "GREEN/RED/DEVOLVIDA"
     ao inves de postar uma mensagem nova.
     Persiste telegram_message_id / telegram_chat_id / telegram_message_text
     em apostas no momento do envio inicial.
     Fallback: se a aposta nao tiver telegram_message_id (apostas antigas
     anteriores a migration 012), volta a mandar mensagem nova.
v7 - fix _buscar_ultimo_confronto: pega ULTIMO tick de cada event_id (placar final
     mais proximo possivel) e exclui o jogo atual da aposta
v6 - le tambem filtrosHistAdicionados (formato antigo) alem de filtrosCompAdicionados
v5 - layout detalhado com filtros do bot (valor atual) + ultimo confronto + resumo aprovado
v4 - adiciona linha "Motivo" na mensagem (lida da coluna apostas.motivo)
v3 - fix: usa coluna 'resultado' (green/red/void) em vez de 'status' (resolvida)

O resolver de apostas seta:
  - status = 'resolvida' (generico)
  - resultado = 'green'/'red'/'void' (veredito real)
"""
import asyncio
import json
import logging
import re
import os
import signal
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

import asyncpg
import httpx

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

try:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parent.parent / '.env'
    if env_path.exists():
        load_dotenv(env_path)
        print(f"[INFO] Carregado .env de {env_path}")
    else:
        print(f"[WARN] .env nao encontrado em {env_path}")
except ImportError:
    print("[WARN] python-dotenv nao instalado")

DB_DSN = "postgresql://postgres:mikedb0702@localhost:5432/mikedb"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"

RATE_LIMIT_DELAY_SEC = 1.1
MAX_RETRIES = 5
RETRY_BASE_SEC = 2
HTTP_TIMEOUT_SEC = 15

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger('telegram_notifier')


class State:
    pool: Optional[asyncpg.Pool] = None
    http: Optional[httpx.AsyncClient] = None
    canal_last_send: dict = {}
    shutdown: bool = False


state = State()


# ============================================================
# HELPERS DE FORMATACAO
# ============================================================
def _emoji_esporte(esporte: str) -> str:
    e = (esporte or '').lower()
    if 'fifa' in e or 'soccer' in e:
        return '⚽'
    if 'nba' in e or 'basket' in e:
        return '🏀'
    if 'hockey' in e:
        return '🏒'
    if 'tennis' in e or 'tenis' in e:
        return '🎾'
    return '🎮'


def _format_decimal(v, casas=2):
    if v is None:
        return '-'
    try:
        d = Decimal(str(v))
        return f'{d:.{casas}f}'
    except Exception:
        return str(v)


def _formatar_data_hora(ts) -> str:
    """Format datetime pra '05/05 14:36:36'"""
    if ts is None:
        return ''
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        except Exception:
            return ts
    try:
        return ts.strftime('%d/%m %H:%M:%S')
    except Exception:
        return str(ts)


def _parse_json_field(v):
    """Bot.filtros/stats_h2h podem vir como str JSON ou dict ja parseado."""
    if v is None:
        return {}
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return v


# ============================================================
# RESUMO DOS FILTROS APROVADOS
# ============================================================
CENARIO_LABEL = {
    'casa_vencendo':       'CASA VENCENDO',
    'casa_perdendo':       'CASA PERDENDO',
    'empate':              'EMPATE',
    'casa_ou_empate':      'CASA OU EMPATE',
    'visitante_ou_empate': 'FORA OU EMPATE',
    'casa_ou_visitante':   'SEM EMPATE',
    'alvo_vencendo':       'ALVO VENCENDO',
    'alvo_perdendo':       'ALVO PERDENDO',
    'oponente_vencendo':   'OPONENTE VENCENDO',
    'favorito':            'FAVORITO',
    'azarao':              'AZARAO',
}


def _resumir_filtros_aprovados(bot_row: dict, aposta: dict) -> str:
    filtros = _parse_json_field(bot_row.get('filtros_jsonb') or aposta.get('bot_filtros'))
    tags = []

    lmin = bot_row.get('linha_min')
    lmax = bot_row.get('linha_max')
    if lmin is not None and lmax is not None:
        tags.append(f"LINHA {_format_decimal(lmin)} A {_format_decimal(lmax)}")
    elif lmin is not None:
        tags.append(f"LINHA ≥ {_format_decimal(lmin)}")
    elif lmax is not None:
        tags.append(f"LINHA ≤ {_format_decimal(lmax)}")

    omin = bot_row.get('odd_min')
    omax = bot_row.get('odd_max')
    if omin is not None and omax is not None:
        tags.append(f"ODD {_format_decimal(omin, 2)} A {_format_decimal(omax, 2)}")

    if filtros.get('cenarioPartidaAtivo'):
        cen = filtros.get('cenarioPartida')
        label = CENARIO_LABEL.get(cen, str(cen).upper() if cen else '')
        if label:
            tags.append(label)

    if filtros.get('diferencaPlacarAtivo'):
        diff = filtros.get('diferencaPlacar', 0)
        if diff:
            tags.append(f"DIF {diff}+")

    return ' · '.join(tags)


# ============================================================
# JANELAS: quantidade (int) OU tempo (string "8h"/"7d")
# Mesma logica do backtest_runner - aceita janela de tempo alem de qtd.
# ============================================================
_RE_JANELA_TEMPO = re.compile(r'^\s*(\d+)\s*([hd])\s*$', re.IGNORECASE)


def _parse_janela(janela):
    """Retorna (modo, valor): ('qtd', n) | ('tempo', segundos) | (None, None)."""
    if isinstance(janela, bool):
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
    """Token da janela pra chave de stats: 10->'10', '8h'->'8h', '7d'->'7d'."""
    modo, _ = _parse_janela(janela)
    if modo == 'tempo':
        return str(janela).strip().lower().replace(" ", "")
    if modo == 'qtd':
        return str(int(janela))
    return None


# ============================================================
# FILTROS COMPLEMENTARES
# ============================================================
TIPO_LABEL = {
    'media': 'Média',
    'wr':    'WR',
    'gap':   'Gap',
    'gap_linha':  'Gap (linha)',
    'gap_media':  'Gap (média)',
    'tendencia':  'Tendência',
    'qtd_h2h':    'H2H',
}


def _normalizar_filtros_hist(filtros_hist: list) -> list:
    normalizados = []
    for fh in filtros_hist or []:
        if not isinstance(fh, dict):
            continue
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
                janela_norm = _janela_token(resto)  # '8h','24h','7d'
        if janela_norm is None:
            continue
        if isinstance(janela_norm, int) and janela_norm < 0:
            continue

        prob = fh.get('prob') or [0, 100]
        if not isinstance(prob, list) or len(prob) < 2:
            prob = [0, 100]
        prob_min, prob_max = prob[0], prob[1]

        min_v = float(prob_min) / 100.0 if prob_min is not None else None
        max_v = float(prob_max) / 100.0 if prob_max is not None else None
        min_ativo = min_v is not None and prob_min > 0
        max_ativo = max_v is not None and prob_max < 100

        normalizados.append({
            'tipo': 'wr',
            'janela': janela_norm,
            'min': min_v,
            'max': max_v,
            'minAtivo': min_ativo,
            'maxAtivo': max_ativo,
            'hist_base': fh.get('base', 'match'),
            'hist_tipo': fh.get('tipo', 'all'),
            'min_partidas': fh.get('minPartidas'),
            '_origem': 'hist',
        })

    return normalizados


def _formatar_filtros_complementares(bot_row: dict, aposta: dict) -> list[str]:
    filtros = _parse_json_field(bot_row.get('filtros_jsonb') or aposta.get('bot_filtros'))
    filtros_comp = filtros.get('filtrosCompAdicionados') or []
    filtros_hist = filtros.get('filtrosHistAdicionados') or []
    filtros_hist_norm = _normalizar_filtros_hist(filtros_hist)
    todos_filtros = list(filtros_comp) + filtros_hist_norm

    if not todos_filtros:
        return []

    stats = _parse_json_field(aposta.get('stats_h2h'))
    if not stats:
        return []

    linhas = []
    for fc in todos_filtros:
        if not isinstance(fc, dict):
            continue
        tipo = (fc.get('tipo') or '').lower().strip()
        janela = fc.get('janela')
        min_v = fc.get('min') if fc.get('minAtivo') else None
        max_v = fc.get('max') if fc.get('maxAtivo') else None
        origem = fc.get('_origem', 'comp')
        hist_base = fc.get('hist_base')
        hist_tipo = fc.get('hist_tipo')

        if not tipo:
            continue

        nao_suportado = False
        nao_suportado_motivo = ''
        if origem == 'hist':
            if hist_base != 'match':
                nao_suportado = True
                nao_suportado_motivo = f'base={hist_base}'
            elif hist_tipo != 'all':
                nao_suportado = True
                nao_suportado_motivo = f'tipo={hist_tipo}'

        janela_valida = True
        tok = _janela_token(janela) if janela is not None else None
        if tipo == 'media' and janela is not None:
            if tok is None:
                janela_valida = False
                stat_key = None
            else:
                stat_key = f'media_ult{tok}'
        elif tipo == 'wr' and janela is not None:
            if tok is None:
                janela_valida = False
                stat_key = None
            else:
                stat_key = f'wr_ult{tok}'
        elif tipo == 'gap_media':
            stat_key = 'gap'
        elif tipo == 'gap_linha':
            stat_key = 'gap_linha_calc'
        else:
            stat_key = tipo

        if not janela_valida or nao_suportado:
            valor = None
        elif stat_key == 'gap_linha_calc':
            media = stats.get('media_ult20')
            linha = stats.get('linha_atual')
            valor = abs(float(media) - float(linha)) if (media is not None and linha is not None) else None
        else:
            valor = stats.get(stat_key)

        rotulo_tipo = TIPO_LABEL.get(tipo, tipo.capitalize())
        if origem == 'hist' and hist_base == 'match':
            rotulo_tipo = f'WR H2H'
        if janela is not None and tipo in ('media', 'wr'):
            modo_j, _ = _parse_janela(janela)
            if modo_j == 'qtd' and int(janela) == 0:
                rotulo = f"{rotulo_tipo} Todas"
            else:
                # qtd: "últ 10" | tempo: "últ 7d"
                rotulo = f"{rotulo_tipo} últ {_janela_token(janela)}"
        else:
            rotulo = rotulo_tipo

        cond_partes = []
        if min_v is not None and max_v is not None:
            cond_partes.append(f"({_fmt_filtro_valor(tipo, min_v)}-{_fmt_filtro_valor(tipo, max_v)})")
        elif min_v is not None:
            cond_partes.append(f"≥ {_fmt_filtro_valor(tipo, min_v)}")
        elif max_v is not None:
            cond_partes.append(f"≤ {_fmt_filtro_valor(tipo, max_v)}")
        cond_txt = ' '.join(cond_partes)

        if nao_suportado:
            valor_txt = f'não suportado ({nao_suportado_motivo})'
            check = '⚠️'
        elif not janela_valida:
            valor_txt = 'não calculado'
            check = '⚠️'
        elif valor is None:
            valor_txt = 'sem H2H'
            check = '⚠️'
        else:
            valor_txt = _fmt_filtro_valor(tipo, valor)
            passou = True
            try:
                v_num = float(valor)
                if min_v is not None and v_num < float(min_v):
                    passou = False
                if max_v is not None and v_num > float(max_v):
                    passou = False
            except Exception:
                pass
            check = '✅' if passou else '❌'

        if cond_txt:
            linhas.append(f"   • {rotulo} {cond_txt} → {valor_txt} {check}")
        else:
            linhas.append(f"   • {rotulo} → {valor_txt}")

    return linhas


def _fmt_filtro_valor(tipo: str, v) -> str:
    try:
        f = float(v)
    except Exception:
        return str(v)

    if tipo == 'wr':
        if 0 <= f <= 1:
            return f"{f * 100:.0f}%"
        return f"{f:.0f}%"
    if tipo == 'qtd_h2h':
        return f"{int(f)}"
    if tipo == 'gap' or tipo.startswith('gap_'):
        return f"{f:+.1f}"
    if tipo == 'tendencia':
        return f"{f:+.2f}"
    return f"{f:.1f}"


# ============================================================
# ULTIMO CONFRONTO (placar FINAL do ultimo jogo do par)
# ============================================================
async def _buscar_ultimo_confronto(jogador_a: str, jogador_b: str, bookmaker: str,
                                    sport: str, antes_de_ts,
                                    event_id_excluir=None) -> Optional[str]:
    """
    Retorna placar do ULTIMO jogo finalizado entre jogador_a e jogador_b (ex: '0-4').

    v7: 2 fixes:
    1. Exclui o event_id da aposta atual (nao retorna placar live do jogo em andamento)
    2. Pega o ULTIMO tick de cada event_id (placar mais proximo do final do jogo),
       em vez do tick "mais recente antes da aposta" (que pode ser de jogo em andamento)
    """
    if not jogador_a or not jogador_b or not bookmaker:
        return None
    try:
        async with state.pool.acquire() as conn:
            # 1. Acha o event_id anterior mais recente do par (excluindo o atual)
            event_row = await conn.fetchrow("""
                SELECT event_id, MAX(ts) AS ultimo_ts
                FROM ticks
                WHERE bookmaker = $1
                  AND ($2::text IS NULL OR sport = $2)
                  AND ((jogador_a = $3 AND jogador_b = $4)
                    OR (jogador_a = $4 AND jogador_b = $3))
                  AND score_home IS NOT NULL
                  AND score_away IS NOT NULL
                  AND ($5::text IS NULL OR event_id != $5)
                GROUP BY event_id
                ORDER BY MAX(ts) DESC
                LIMIT 1
            """, bookmaker, sport, jogador_a, jogador_b,
                str(event_id_excluir) if event_id_excluir else None)

            if not event_row:
                return None

            event_id_anterior = event_row['event_id']

            # 2. Pega o ULTIMO tick com placar desse evento (mais proximo do fim do jogo)
            placar_row = await conn.fetchrow("""
                SELECT score_home, score_away
                FROM ticks
                WHERE event_id = $1
                  AND bookmaker = $2
                  AND score_home IS NOT NULL
                  AND score_away IS NOT NULL
                ORDER BY ts DESC
                LIMIT 1
            """, event_id_anterior, bookmaker)

            if not placar_row:
                return None

            return f"{placar_row['score_home']}-{placar_row['score_away']}"
    except Exception as e:
        logger.exception(f"erro buscando ultimo confronto: {e}")
        return None


# ============================================================
# MONTA MENSAGEM NOVA (LAYOUT v5)
# ============================================================
async def montar_msg_aposta_nova(aposta: dict) -> str:
    emoji = _emoji_esporte(aposta.get('bot_esporte') or aposta.get('esporte', ''))
    bot_nome = (aposta.get('bot_nome') or 'Bot').upper()
    apostado_em = _formatar_data_hora(aposta.get('apostado_em'))

    casa = (aposta.get('casa') or aposta.get('bot_casa') or '?').upper()
    liga = aposta.get('liga') or aposta.get('torneio') or '?'

    jogador_a = aposta.get('jogador_a') or '?'
    jogador_b = aposta.get('jogador_b') or '?'

    selecao = aposta.get('selecao') or aposta.get('lado') or '?'
    linha = aposta.get('linha')
    linha_str = str(linha) if linha is not None else ''
    if linha_str and linha_str in selecao:
        linha_txt = ''
    else:
        linha_txt = f' {linha}' if linha is not None else ''
    odd = _format_decimal(aposta.get('odd'))

    minuto = aposta.get('minuto_entrada')
    periodo = aposta.get('periodo_entrada')
    if minuto is not None:
        tempo_txt = f"{minuto}'"
        if periodo:
            tempo_txt += f" ({periodo})"
    else:
        tempo_txt = aposta.get('live_time') or '-'

    placar_a = aposta.get('placar_a_entrada')
    placar_b = aposta.get('placar_b_entrada')
    if placar_a is None:
        placar_a = aposta.get('score_home_no_momento')
    if placar_b is None:
        placar_b = aposta.get('score_away_no_momento')
    placar = f'{placar_a}-{placar_b}' if placar_a is not None else '-'

    stake = _format_decimal(aposta.get('stake'))

    resumo_aprovado = _resumir_filtros_aprovados(aposta, aposta)

    # v7: passa event_id pra excluir o jogo atual da busca
    ultimo_confronto = await _buscar_ultimo_confronto(
        jogador_a, jogador_b,
        aposta.get('bookmaker') or aposta.get('bot_casa'),
        aposta.get('sport') or aposta.get('bot_sport_banco'),
        aposta.get('apostado_em') or datetime.now(),
        event_id_excluir=aposta.get('event_id'),
    )
    ultimo_txt = f"\n⏳ Último Confronto: <b>{ultimo_confronto}</b>" if ultimo_confronto else ''

    # Total de Confrontos (qtd_h2h do stats_h2h) - quantos jogos do par o bot usou
    stats_msg = _parse_json_field(aposta.get('stats_h2h'))
    qtd_h2h = stats_msg.get('qtd_h2h') if stats_msg else None
    total_confrontos_txt = ''
    if qtd_h2h is not None:
        try:
            total_confrontos_txt = f"\n📚 Total de Confrontos: <b>{int(qtd_h2h)}</b>"
        except (TypeError, ValueError):
            pass

    filtros_linhas = _formatar_filtros_complementares(aposta, aposta)
    filtros_bloco = ''
    if filtros_linhas:
        filtros_bloco = '\n\n📊 <b>Filtros do bot (valor atual):</b>\n' + '\n'.join(filtros_linhas)

    cabecalho = f'{emoji} <b>{bot_nome}</b>'
    if apostado_em:
        cabecalho += f'\n<i>{apostado_em}</i>'

    msg = (
        f'{cabecalho}\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'<b>{selecao}{linha_txt} @ {odd}</b>\n'
        f'<i>{casa} · {liga}</i>\n\n'
        f'⚔️ {jogador_a} vs {jogador_b}'
    )
    if resumo_aprovado:
        msg += f'\n📋 {resumo_aprovado}'

    msg += (
        f'\n\n🕐 Tempo: {tempo_txt}\n'
        f'🔢 Placar: {placar}'
        f'{ultimo_txt}'
        f'{total_confrontos_txt}'
        f'{filtros_bloco}'
        f'\n\n💰 Stake: R${stake}\n'
        f'🆔 #{aposta.get("id")}'
    )
    return msg


# ============================================================
# FOOTER DE RESOLUCAO (concatenado na msg original via edit)
# ============================================================
def _footer_resolucao(aposta: dict) -> str:
    veredito = (aposta.get('resultado') or '').lower()
    if not veredito or veredito == 'pendente':
        veredito = (aposta.get('status') or '').lower()

    if veredito in ('green', 'ganhou'):
        emoji_status = '✅'
        rotulo = 'GREEN'
    elif veredito in ('red', 'perdeu'):
        emoji_status = '❌'
        rotulo = 'RED'
    elif veredito in ('void', 'devolvido', 'devolvida', 'cancelado'):
        emoji_status = '⚪'
        rotulo = 'DEVOLVIDA'
    else:
        emoji_status = 'ℹ️'
        rotulo = veredito.upper() if veredito else '?'

    placar_a = aposta.get('placar_final_a')
    placar_b = aposta.get('placar_final_b')
    if placar_a is None:
        placar_a = aposta.get('placar_a_entrada')
    if placar_b is None:
        placar_b = aposta.get('placar_b_entrada')
    placar_txt = f'{placar_a}-{placar_b}' if placar_a is not None else '?'

    pnl_raw = aposta.get('pnl')
    if pnl_raw is None:
        pnl_raw = aposta.get('lucro_unidades')
    if pnl_raw is not None:
        try:
            pnl_dec = Decimal(str(pnl_raw))
            sinal = '+' if pnl_dec >= 0 else ''
            pnl_txt = f'{sinal}R${_format_decimal(pnl_dec)}'
        except Exception:
            pnl_txt = '-'
    else:
        pnl_txt = '-'

    motivo = (aposta.get('motivo') or '').strip()
    motivo_linha = f'\n🧠 {motivo}' if motivo else ''

    linha_status = (
        f'{emoji_status} <b>{rotulo}</b> · '
        f'Placar final <b>{placar_txt}</b> · '
        f'PnL <b>{pnl_txt}</b>'
    )

    return '\n\n━━━━━━━━━━━━━━━━━━\n' + linha_status + motivo_linha


# ============================================================
# MONTA MENSAGEM RESOLVIDA (fallback - mensagem nova standalone)
# ============================================================
def montar_msg_aposta_resolvida(aposta: dict) -> str:
    veredito = (aposta.get('resultado') or '').lower()
    if not veredito or veredito == 'pendente':
        veredito = (aposta.get('status') or '').lower()

    if veredito in ('green', 'ganhou'):
        emoji_status = '✅'
        rotulo = 'GREEN'
    elif veredito in ('red', 'perdeu'):
        emoji_status = '❌'
        rotulo = 'RED'
    elif veredito in ('void', 'devolvido', 'devolvida', 'cancelado'):
        emoji_status = '⚪'
        rotulo = 'DEVOLVIDA'
    else:
        emoji_status = 'ℹ️'
        rotulo = veredito.upper() if veredito else '?'

    bot_nome = (aposta.get('bot_nome') or 'Bot').upper()
    jogador_a = aposta.get('jogador_a') or '?'
    jogador_b = aposta.get('jogador_b') or '?'

    placar_a = aposta.get('placar_final_a')
    placar_b = aposta.get('placar_final_b')
    if placar_a is None:
        placar_a = aposta.get('placar_a_entrada')
    if placar_b is None:
        placar_b = aposta.get('placar_b_entrada')
    placar_final = f'{placar_a}-{placar_b}' if placar_a is not None else '?'

    selecao = aposta.get('selecao') or aposta.get('lado') or '?'
    linha = aposta.get('linha')
    linha_txt = f' {linha}' if linha is not None else ''
    odd = _format_decimal(aposta.get('odd'))

    pnl_raw = aposta.get('pnl')
    if pnl_raw is None:
        pnl_raw = aposta.get('lucro_unidades')

    if pnl_raw is not None:
        pnl_dec = Decimal(str(pnl_raw))
        sinal = '+' if pnl_dec >= 0 else ''
        pnl_txt = f'{sinal}R${_format_decimal(pnl_dec)}'
    else:
        pnl_txt = '-'

    motivo = (aposta.get('motivo') or '').strip()
    motivo_linha = f'\n🧠 {motivo}' if motivo else ''

    msg = (
        f'{emoji_status} <b>{rotulo}</b> — {bot_nome}\n'
        f'━━━━━━━━━━━━━━\n'
        f'⚔️ {jogador_a} vs {jogador_b}: <b>{placar_final}</b>\n'
        f'📊 {selecao}{linha_txt} @ {odd}\n'
        f'💵 PnL: <b>{pnl_txt}</b>'
        f'{motivo_linha}\n'
        f'🆔 #{aposta.get("id")}'
    )
    return msg


# ============================================================
# TELEGRAM API
# ============================================================
async def enviar_telegram(chat_id: str, text: str,
                          max_retries: int = MAX_RETRIES) -> Optional[int]:
    """
    Envia mensagem. Retorna o message_id retornado pelo Telegram em caso
    de sucesso, ou None em caso de falha.
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN nao configurado — pulando envio")
        return None

    agora = datetime.now()
    ultimo = state.canal_last_send.get(chat_id)
    if ultimo:
        delta = (agora - ultimo).total_seconds()
        if delta < RATE_LIMIT_DELAY_SEC:
            await asyncio.sleep(RATE_LIMIT_DELAY_SEC - delta)

    url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN, method='sendMessage')
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True,
    }

    for tentativa in range(1, max_retries + 1):
        try:
            r = await state.http.post(url, json=payload, timeout=HTTP_TIMEOUT_SEC)
            data = r.json()

            if r.status_code == 200 and data.get('ok'):
                state.canal_last_send[chat_id] = datetime.now()
                msg_id = data.get('result', {}).get('message_id')
                logger.info(f"[OK] msg enviada chat_id={chat_id} message_id={msg_id} (tentativa {tentativa})")
                return msg_id

            if r.status_code == 429:
                retry_after = data.get('parameters', {}).get('retry_after', RETRY_BASE_SEC * tentativa)
                logger.warning(f"[429] chat_id={chat_id} rate-limit, retry_after={retry_after}s")
                await asyncio.sleep(retry_after + 0.5)
                continue

            if r.status_code in (400, 403):
                logger.error(f"[{r.status_code}] chat_id={chat_id} erro permanente: {data.get('description', r.text)[:200]}")
                return None

            espera = RETRY_BASE_SEC * (2 ** (tentativa - 1))
            logger.warning(f"[{r.status_code}] chat_id={chat_id} erro temporario, retry em {espera}s: {data}")
            await asyncio.sleep(espera)

        except (httpx.TimeoutException, httpx.NetworkError) as e:
            espera = RETRY_BASE_SEC * (2 ** (tentativa - 1))
            logger.warning(f"[NET] chat_id={chat_id} {type(e).__name__}, retry em {espera}s")
            await asyncio.sleep(espera)

        except Exception as e:
            logger.exception(f"[ERR] chat_id={chat_id} erro inesperado: {e}")
            return None

    logger.error(f"[FAIL] chat_id={chat_id} desistiu apos {max_retries} tentativas")
    return None


async def editar_telegram(chat_id: str, message_id: int, text: str,
                          max_retries: int = MAX_RETRIES) -> bool:
    """
    Edita uma mensagem existente. Retorna True em caso de sucesso (incluindo
    o caso 'message is not modified'), False em caso de falha permanente.
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN nao configurado — pulando edicao")
        return False

    agora = datetime.now()
    ultimo = state.canal_last_send.get(chat_id)
    if ultimo:
        delta = (agora - ultimo).total_seconds()
        if delta < RATE_LIMIT_DELAY_SEC:
            await asyncio.sleep(RATE_LIMIT_DELAY_SEC - delta)

    url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN, method='editMessageText')
    payload = {
        'chat_id': chat_id,
        'message_id': message_id,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True,
    }

    for tentativa in range(1, max_retries + 1):
        try:
            r = await state.http.post(url, json=payload, timeout=HTTP_TIMEOUT_SEC)
            data = r.json()

            if r.status_code == 200 and data.get('ok'):
                state.canal_last_send[chat_id] = datetime.now()
                logger.info(f"[OK] msg editada chat_id={chat_id} message_id={message_id} (tentativa {tentativa})")
                return True

            if r.status_code == 429:
                retry_after = data.get('parameters', {}).get('retry_after', RETRY_BASE_SEC * tentativa)
                logger.warning(f"[429] edit chat_id={chat_id} rate-limit, retry_after={retry_after}s")
                await asyncio.sleep(retry_after + 0.5)
                continue

            desc = (data.get('description') or '').lower()
            if 'message is not modified' in desc:
                logger.info(f"edit chat_id={chat_id} message_id={message_id}: ja igual, ok")
                return True

            if r.status_code in (400, 403):
                logger.error(f"[{r.status_code}] edit chat_id={chat_id} message_id={message_id} erro permanente: {data.get('description', r.text)[:200]}")
                return False

            espera = RETRY_BASE_SEC * (2 ** (tentativa - 1))
            logger.warning(f"[{r.status_code}] edit chat_id={chat_id} erro temporario, retry em {espera}s: {data}")
            await asyncio.sleep(espera)

        except (httpx.TimeoutException, httpx.NetworkError) as e:
            espera = RETRY_BASE_SEC * (2 ** (tentativa - 1))
            logger.warning(f"[NET] edit chat_id={chat_id} {type(e).__name__}, retry em {espera}s")
            await asyncio.sleep(espera)

        except Exception as e:
            logger.exception(f"[ERR] edit chat_id={chat_id} erro inesperado: {e}")
            return False

    logger.error(f"[FAIL] edit chat_id={chat_id} message_id={message_id} desistiu apos {max_retries} tentativas")
    return False


# ============================================================
# LOOKUP - QUERY COM NOMES REAIS + DADOS PRO LAYOUT v5
# ============================================================
SQL_APOSTA_COMPLETA = """
    SELECT
        a.id, a.bot_id, a.tick_id, a.modo,
        a.bookmaker, a.casa, a.esporte, a.torneio, a.liga, a.event_id,
        a.jogador_a, a.jogador_b,
        a.mercado, a.mercado_tipo, a.linha, a.selecao, a.lado,
        a.odd,
        a.placar_a_entrada, a.placar_b_entrada,
        a.score_home_no_momento, a.score_away_no_momento,
        a.placar_final_a, a.placar_final_b,
        a.minuto_entrada, a.periodo_entrada, a.live_time,
        a.stake, a.pnl, a.lucro_unidades,
        a.status, a.resultado, a.motivo,
        a.stats_h2h,
        a.apostado_em, a.resolvido_em,
        a.telegram_message_id, a.telegram_chat_id, a.telegram_message_text,
        b.nome AS bot_nome,
        b.casa AS bot_casa,
        b.esporte AS bot_esporte,
        b.linha_min, b.linha_max,
        b.odd_min, b.odd_max,
        b.filtros AS filtros_jsonb,
        b.em_treinamento,
        b.telegram_canal_id,
        c.chat_id AS canal_chat_id,
        c.nome AS canal_nome,
        c.ativo AS canal_ativo
    FROM apostas a
    INNER JOIN bots b ON a.bot_id = b.id
    LEFT JOIN telegram_canais c ON b.telegram_canal_id = c.id
    WHERE a.id = $1
"""


async def buscar_aposta(aposta_id: int) -> Optional[dict]:
    async with state.pool.acquire() as conn:
        row = await conn.fetchrow(SQL_APOSTA_COMPLETA, aposta_id)
        if not row:
            return None
        d = dict(row)
        esp_to_sport = {'fifa': 'E-Football', 'nba2k': 'E-Basketball',
                        'ehockey': 'E-Hockey', 'etennis': 'E-Tennis'}
        d['bot_sport_banco'] = esp_to_sport.get(d.get('bot_esporte'), d.get('bot_esporte'))
        return d


async def _persistir_msg_telegram(aposta_id: int, chat_id: str, message_id: int, text: str):
    """Salva o tracking da mensagem na aposta pra permitir edit no resolve."""
    try:
        async with state.pool.acquire() as conn:
            await conn.execute("""
                UPDATE apostas
                   SET telegram_message_id   = $1,
                       telegram_chat_id      = $2,
                       telegram_message_text = $3
                 WHERE id = $4
            """, message_id, chat_id, text, aposta_id)
    except Exception as e:
        logger.exception(f"erro salvando telegram_message_id da aposta #{aposta_id}: {e}")


# ============================================================
# HANDLERS
# ============================================================
async def processar_aposta_nova(aposta_id: int):
    try:
        data = await buscar_aposta(aposta_id)
        if not data:
            logger.warning(f"aposta #{aposta_id} nao encontrada")
            return

        if data.get('em_treinamento'):
            logger.info(f"aposta #{aposta_id} pulada: bot {data.get('bot_nome')} em treinamento")
            return

        if not data.get('telegram_canal_id'):
            logger.info(f"aposta #{aposta_id} pulada: bot {data.get('bot_nome')} sem canal Telegram")
            return

        if not data.get('canal_ativo'):
            logger.info(f"aposta #{aposta_id} pulada: canal {data.get('canal_nome')} inativo")
            return

        chat_id = data.get('canal_chat_id')
        if not chat_id or chat_id.startswith('PLACEHOLDER_'):
            logger.warning(f"aposta #{aposta_id} pulada: canal {data.get('canal_nome')} sem chat_id real")
            return

        msg = await montar_msg_aposta_nova(data)
        message_id = await enviar_telegram(chat_id, msg)

        if message_id is not None:
            await _persistir_msg_telegram(aposta_id, chat_id, message_id, msg)

    except Exception as e:
        logger.exception(f"erro processando aposta_nova #{aposta_id}: {e}")


async def processar_aposta_resolvida(aposta_id: int):
    try:
        data = await buscar_aposta(aposta_id)
        if not data:
            return

        if data.get('em_treinamento'):
            logger.info(f"aposta resolvida #{aposta_id} pulada: bot em treinamento")
            return

        if not data.get('telegram_canal_id') or not data.get('canal_ativo'):
            return

        chat_id_atual = data.get('canal_chat_id')
        if not chat_id_atual or chat_id_atual.startswith('PLACEHOLDER_'):
            return

        tg_msg_id = data.get('telegram_message_id')
        tg_chat_id = data.get('telegram_chat_id') or chat_id_atual
        tg_text = data.get('telegram_message_text')

        # Caminho preferido: edita a mensagem original (anti-flood)
        if tg_msg_id and tg_text:
            novo_texto = tg_text + _footer_resolucao(data)
            ok = await editar_telegram(tg_chat_id, tg_msg_id, novo_texto)
            if ok:
                logger.info(f"aposta resolvida #{aposta_id} editada: resultado={data.get('resultado')}")
                return
            logger.warning(f"aposta #{aposta_id} falhou ao editar mensagem original — caindo pra mensagem nova")

        # Fallback: mensagem nova (apostas legacy sem telegram_message_id)
        msg = montar_msg_aposta_resolvida(data)
        await enviar_telegram(chat_id_atual, msg)
        logger.info(f"aposta resolvida #{aposta_id} enviada (fallback nova msg): resultado={data.get('resultado')}")

    except Exception as e:
        logger.exception(f"erro processando aposta_resolvida #{aposta_id}: {e}")


# ============================================================
# LISTENER
# ============================================================
async def loop_listener():
    while not state.shutdown:
        conn = None
        try:
            conn = await asyncpg.connect(DB_DSN)

            def on_aposta_nova(_c, _pid, _ch, payload):
                try:
                    asyncio.create_task(processar_aposta_nova(int(payload)))
                except Exception as e:
                    logger.exception(f"on_aposta_nova erro: {e}")

            def on_aposta_resolvida(_c, _pid, _ch, payload):
                try:
                    asyncio.create_task(processar_aposta_resolvida(int(payload)))
                except Exception as e:
                    logger.exception(f"on_aposta_resolvida erro: {e}")

            await conn.add_listener('aposta_nova', on_aposta_nova)
            await conn.add_listener('aposta_resolvida', on_aposta_resolvida)
            logger.info("LISTEN ativo em aposta_nova + aposta_resolvida")

            while not state.shutdown:
                await asyncio.sleep(30)
                try:
                    await conn.execute('SELECT 1')
                except Exception:
                    logger.warning("ping falhou, reconectando")
                    break

        except Exception as e:
            logger.exception(f"loop_listener erro: {e}, reconectando em 5s")
            await asyncio.sleep(5)
        finally:
            if conn:
                try:
                    await conn.close()
                except:
                    pass


# ============================================================
# MAIN
# ============================================================
async def main():
    logger.info("=" * 60)
    logger.info("TelegramNotifier iniciando (v8 - edit msg original ao resolver, anti-flood)")
    logger.info(f"TOKEN configurado: {'sim' if TELEGRAM_BOT_TOKEN else 'NAO!'}")
    if TELEGRAM_BOT_TOKEN:
        logger.info(f"TOKEN preview: {TELEGRAM_BOT_TOKEN[:8]}...{TELEGRAM_BOT_TOKEN[-4:]}")
    logger.info("=" * 60)

    state.pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=5)
    state.http = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: setattr(state, 'shutdown', True))
        except NotImplementedError:
            signal.signal(sig, lambda *_: setattr(state, 'shutdown', True))

    try:
        await loop_listener()
    finally:
        logger.info("encerrando...")
        if state.http:
            await state.http.aclose()
        if state.pool:
            await state.pool.close()
        logger.info("encerrado")


if __name__ == '__main__':
    asyncio.run(main())
