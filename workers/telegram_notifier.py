"""
telegram_notifier.py - Worker de envio de tips pro Telegram
v4 - adiciona linha "Motivo" na mensagem (lida da coluna apostas.motivo)
v3 - fix: usa coluna 'resultado' (green/red/void) em vez de 'status' (resolvida)

O resolver de apostas seta:
  - status = 'resolvida' (generico)
  - resultado = 'green'/'red'/'void' (veredito real)

O bot_executor preenche apostas.motivo com resumo curto dos filtros
que bateram pra apitar a tip (ex: "WR10=100% · gap=8 · Q2").
"""
import asyncio
import json
import logging
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
# FORMATACAO
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


def montar_msg_aposta_nova(aposta: dict) -> str:
    """Mensagem HTML quando aposta é criada"""
    emoji = _emoji_esporte(aposta.get('bot_esporte') or aposta.get('esporte', ''))
    bot_nome = (aposta.get('bot_nome') or 'Bot').upper()
    casa = (aposta.get('casa') or aposta.get('bot_casa') or '?').upper()
    liga = aposta.get('liga') or aposta.get('torneio') or '?'

    jogador_a = aposta.get('jogador_a') or '?'
    jogador_b = aposta.get('jogador_b') or '?'

    selecao = aposta.get('selecao') or aposta.get('lado') or '?'
    linha = aposta.get('linha')
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
    placar = f'{placar_a}x{placar_b}' if placar_a is not None else '-'

    stake = _format_decimal(aposta.get('stake'))
    motivo = (aposta.get('motivo') or '').strip()
    motivo_linha = f'\n🧠 {motivo}' if motivo else ''

    msg = (
        f'🟢 <b>NOVA TIP</b> — {bot_nome}\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'{emoji} <b>{casa}</b> · {liga}\n'
        f'⚔️ {jogador_a} vs {jogador_b}\n'
        f'📊 <b>{selecao}{linha_txt}</b> @ <b>{odd}</b>\n'
        f'⏱️ Live: {tempo_txt} · {placar}\n'
        f'💰 Stake: R${stake}'
        f'{motivo_linha}\n'
        f'🆔 #{aposta.get("id")}'
    )
    return msg


def montar_msg_aposta_resolvida(aposta: dict) -> str:
    """Mensagem HTML quando aposta resolve.

    IMPORTANTE: usa coluna 'resultado' (que tem green/red/void),
    NAO 'status' (que tem 'resolvida' generico).
    """
    # Prioriza 'resultado' sobre 'status' (este eh sempre 'resolvida')
    veredito = (aposta.get('resultado') or '').lower()
    if not veredito or veredito == 'pendente':
        # fallback se algum dia o resolver mudar de coluna
        veredito = (aposta.get('status') or '').lower()

    if veredito in ('green', 'ganhou'):
        emoji_status = '✅'
        rotulo = 'GREEN'
    elif veredito in ('red', 'perdeu'):
        emoji_status = '❌'
        rotulo = 'RED'
    elif veredito in ('void', 'devolvido', 'cancelado'):
        emoji_status = '⚪'
        rotulo = 'VOID'
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
    placar_final = f'{placar_a}x{placar_b}' if placar_a is not None else '?'

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
async def enviar_telegram(chat_id: str, text: str, max_retries: int = MAX_RETRIES) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN nao configurado — pulando envio")
        return False

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
                logger.info(f"[OK] msg enviada chat_id={chat_id} (tentativa {tentativa})")
                return True

            if r.status_code == 429:
                retry_after = data.get('parameters', {}).get('retry_after', RETRY_BASE_SEC * tentativa)
                logger.warning(f"[429] chat_id={chat_id} rate-limit, retry_after={retry_after}s")
                await asyncio.sleep(retry_after + 0.5)
                continue

            if r.status_code in (400, 403):
                logger.error(f"[{r.status_code}] chat_id={chat_id} erro permanente: {data.get('description', r.text)[:200]}")
                return False

            espera = RETRY_BASE_SEC * (2 ** (tentativa - 1))
            logger.warning(f"[{r.status_code}] chat_id={chat_id} erro temporario, retry em {espera}s: {data}")
            await asyncio.sleep(espera)

        except (httpx.TimeoutException, httpx.NetworkError) as e:
            espera = RETRY_BASE_SEC * (2 ** (tentativa - 1))
            logger.warning(f"[NET] chat_id={chat_id} {type(e).__name__}, retry em {espera}s")
            await asyncio.sleep(espera)

        except Exception as e:
            logger.exception(f"[ERR] chat_id={chat_id} erro inesperado: {e}")
            return False

    logger.error(f"[FAIL] chat_id={chat_id} desistiu apos {max_retries} tentativas")
    return False


# ============================================================
# LOOKUP - QUERY COM NOMES REAIS
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
        a.apostado_em, a.resolvido_em,
        b.nome AS bot_nome,
        b.casa AS bot_casa,
        b.esporte AS bot_esporte,
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
        return dict(row)


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

        msg = montar_msg_aposta_nova(data)
        await enviar_telegram(chat_id, msg)

    except Exception as e:
        logger.exception(f"erro processando aposta_nova #{aposta_id}: {e}")


async def processar_aposta_resolvida(aposta_id: int):
    try:
        data = await buscar_aposta(aposta_id)
        if not data:
            return

        if data.get('em_treinamento'):
            logger.info(f"aposta resolvida #{aposta_id} pulada: bot {data.get('bot_nome')} em treinamento")
            return

        if not data.get('telegram_canal_id') or not data.get('canal_ativo'):
            return

        chat_id = data.get('canal_chat_id')
        if not chat_id or chat_id.startswith('PLACEHOLDER_'):
            return

        msg = montar_msg_aposta_resolvida(data)
        await enviar_telegram(chat_id, msg)
        logger.info(f"aposta resolvida #{aposta_id} enviada: resultado={data.get('resultado')}")

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
    logger.info("TelegramNotifier iniciando (v4 — mostra motivo da tip)")
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
