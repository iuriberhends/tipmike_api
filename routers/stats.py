"""
routers/stats.py
GET /stats/dashboard, GET /stats/bots, GET /stats/bots/{id}
"""

from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone, timedelta
from database import db

router = APIRouter(prefix="/stats", tags=["Stats"])


@router.get("/dashboard")
async def dashboard():
    """Resumo geral do sistema."""
    agora = datetime.now(timezone.utc)
    inicio_hoje = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    async with db() as conn:
        # Bots
        bots_ativos = await conn.fetchval("SELECT COUNT(*) FROM bots WHERE status = 'ativo'")
        bots_total = await conn.fetchval("SELECT COUNT(*) FROM bots WHERE status != 'arquivado'")

        # Apostas hoje
        apostas_hoje = await conn.fetchval(
            "SELECT COUNT(*) FROM apostas WHERE apostado_em >= $1", inicio_hoje
        )
        apostas_pendentes = await conn.fetchval(
            "SELECT COUNT(*) FROM apostas WHERE resultado = 'pendente'"
        )

        # Lucro e WR hoje
        lucro_hoje = await conn.fetchval(
            "SELECT COALESCE(SUM(lucro_unidades), 0) FROM apostas WHERE apostado_em >= $1 AND resultado != 'pendente'",
            inicio_hoje
        )
        ganhas_hoje = await conn.fetchval(
            "SELECT COUNT(*) FROM apostas WHERE apostado_em >= $1 AND resultado = 'ganhou'",
            inicio_hoje
        )
        resolvidas_hoje = await conn.fetchval(
            "SELECT COUNT(*) FROM apostas WHERE apostado_em >= $1 AND resultado != 'pendente'",
            inicio_hoje
        )

        # Ticks última hora
        uma_hora_atras = agora - timedelta(hours=1)
        ticks_ultima_hora = await conn.fetchval(
            "SELECT COUNT(*) FROM ticks WHERE ts >= $1", uma_hora_atras
        )

        # Bookmakers ativos (tiveram ticks na última hora)
        bookmakers_rows = await conn.fetch(
            "SELECT DISTINCT bookmaker FROM ticks WHERE ts >= $1 ORDER BY bookmaker",
            uma_hora_atras
        )

    win_rate = round((ganhas_hoje / resolvidas_hoje * 100), 1) if resolvidas_hoje > 0 else None

    return {
        "bots_ativos": bots_ativos,
        "bots_total": bots_total,
        "apostas_hoje": apostas_hoje,
        "lucro_hoje": float(lucro_hoje) if lucro_hoje else 0.0,
        "win_rate_hoje": win_rate,
        "apostas_pendentes": apostas_pendentes,
        "ticks_ultima_hora": ticks_ultima_hora,
        "bookmakers_ativos": [r["bookmaker"] for r in bookmakers_rows],
        "atualizado_em": agora.isoformat(),
    }


@router.get("/bots")
async def stats_todos_bots():
    """Performance agregada de todos os bots."""
    sql = """
        SELECT
            b.id, b.nome, b.casa, b.esporte, b.mercado, b.status,
            COUNT(a.id) AS total_apostas,
            COUNT(a.id) FILTER (WHERE a.resultado = 'ganhou') AS ganhas,
            COUNT(a.id) FILTER (WHERE a.resultado = 'perdeu') AS perdidas,
            COUNT(a.id) FILTER (WHERE a.resultado = 'pendente') AS pendentes,
            COALESCE(SUM(a.lucro_unidades) FILTER (WHERE a.resultado != 'pendente'), 0) AS lucro_total,
            ROUND(
                COUNT(a.id) FILTER (WHERE a.resultado = 'ganhou') * 100.0 /
                NULLIF(COUNT(a.id) FILTER (WHERE a.resultado IN ('ganhou','perdeu')), 0),
                1
            ) AS win_rate
        FROM bots b
        LEFT JOIN apostas a ON a.bot_id = b.id AND a.modo = 'real'
        WHERE b.status != 'arquivado'
        GROUP BY b.id, b.nome, b.casa, b.esporte, b.mercado, b.status
        ORDER BY lucro_total DESC
    """

    async with db() as conn:
        rows = await conn.fetch(sql)

    return [dict(r) for r in rows]


@router.get("/bots/{bot_id}")
async def stats_bot(bot_id: int):
    """Performance detalhada de um bot."""
    async with db() as conn:
        bot = await conn.fetchrow("SELECT * FROM bots WHERE id = $1", bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot não encontrado")

        # Stats gerais
        stats = await conn.fetchrow("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE resultado = 'ganhou') AS ganhas,
                COUNT(*) FILTER (WHERE resultado = 'perdeu') AS perdidas,
                COUNT(*) FILTER (WHERE resultado = 'pendente') AS pendentes,
                COALESCE(SUM(lucro_unidades) FILTER (WHERE resultado != 'pendente'), 0) AS lucro,
                ROUND(
                    COUNT(*) FILTER (WHERE resultado = 'ganhou') * 100.0 /
                    NULLIF(COUNT(*) FILTER (WHERE resultado IN ('ganhou','perdeu')), 0),
                    1
                ) AS win_rate,
                MIN(apostado_em) AS primeira_aposta,
                MAX(apostado_em) AS ultima_aposta
            FROM apostas
            WHERE bot_id = $1 AND modo = 'real'
        """, bot_id)

        # Lucro por dia (últimos 30 dias)
        timeline = await conn.fetch("""
            SELECT
                DATE(apostado_em) AS dia,
                COUNT(*) FILTER (WHERE resultado = 'ganhou') AS ganhas,
                COUNT(*) FILTER (WHERE resultado = 'perdeu') AS perdidas,
                COALESCE(SUM(lucro_unidades) FILTER (WHERE resultado != 'pendente'), 0) AS lucro_dia
            FROM apostas
            WHERE bot_id = $1 AND modo = 'real'
              AND apostado_em >= NOW() - INTERVAL '30 days'
            GROUP BY dia
            ORDER BY dia ASC
        """, bot_id)

    return {
        "bot": dict(bot),
        "stats": dict(stats),
        "timeline": [dict(r) for r in timeline],
    }