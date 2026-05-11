"""
routers/eventos.py
GET /eventos/live, GET /eventos/finished, GET /eventos/{id}
"""

from fastapi import APIRouter, Query, HTTPException
from typing import Optional
from datetime import datetime, timezone, timedelta
from database import db

router = APIRouter(prefix="/eventos", tags=["Eventos"])

# Evento é considerado "live" se teve tick nos últimos N segundos
LIVE_WINDOW_SEG = 60


@router.get("/live")
async def eventos_live(
    bookmaker: Optional[str] = None,
    sport: Optional[str] = None,
    liga: Optional[str] = None,
):
    """Jogos rolando agora (baseado em ticks recentes)."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=LIVE_WINDOW_SEG)

    conditions = ["ts >= $1", "mercado_tipo != 'SCORE_UPDATE'"]
    params = [cutoff]
    i = 2

    if bookmaker:
        conditions.append(f"bookmaker = ${i}"); params.append(bookmaker); i += 1
    if sport:
        conditions.append(f"sport ILIKE ${i}"); params.append(f"%{sport}%"); i += 1
    if liga:
        conditions.append(f"liga ILIKE ${i}"); params.append(f"%{liga}%"); i += 1

    where = " AND ".join(conditions)

    sql = f"""
        SELECT DISTINCT ON (bookmaker, event_id)
            bookmaker, sport, liga, event_id, evento,
            jogador_a, jogador_b,
            score_home, score_away, live_time,
            ts AS ultimo_tick
        FROM ticks
        WHERE {where}
        ORDER BY bookmaker, event_id, ts DESC
    """

    async with db() as conn:
        rows = await conn.fetch(sql, *params)

    return [dict(r) for r in rows]


@router.get("/finished")
async def eventos_finished(
    bookmaker: Optional[str] = None,
    sport: Optional[str] = None,
    liga: Optional[str] = None,
    data_inicio: Optional[str] = None,
    data_fim: Optional[str] = None,
    limit: int = Query(default=100, le=500),
    offset: int = 0,
):
    """Jogos terminados com score final."""
    conditions = ["mercado_tipo = 'SCORE_UPDATE'"]
    params = []
    i = 1

    if bookmaker:
        conditions.append(f"bookmaker = ${i}"); params.append(bookmaker); i += 1
    if sport:
        conditions.append(f"sport ILIKE ${i}"); params.append(f"%{sport}%"); i += 1
    if liga:
        conditions.append(f"liga ILIKE ${i}"); params.append(f"%{liga}%"); i += 1
    if data_inicio:
        conditions.append(f"ts >= ${i}"); params.append(data_inicio); i += 1
    if data_fim:
        conditions.append(f"ts <= ${i}"); params.append(data_fim); i += 1

    where = " AND ".join(conditions)
    params += [limit, offset]

    sql = f"""
        SELECT DISTINCT ON (bookmaker, event_id)
            bookmaker, sport, liga, event_id, evento,
            jogador_a, jogador_b,
            score_home, score_away, live_time,
            ts
        FROM ticks
        WHERE {where}
        ORDER BY bookmaker, event_id, ts DESC
        LIMIT ${i} OFFSET ${i+1}
    """

    async with db() as conn:
        rows = await conn.fetch(sql, *params)

    return [dict(r) for r in rows]


@router.get("/{event_id}")
async def get_evento(event_id: str, bookmaker: Optional[str] = None):
    """Detalhes de um evento específico."""
    conditions = ["event_id = $1"]
    params = [event_id]
    i = 2

    if bookmaker:
        conditions.append(f"bookmaker = ${i}"); params.append(bookmaker); i += 1

    where = " AND ".join(conditions)

    sql = f"""
        SELECT DISTINCT ON (bookmaker, event_id)
            bookmaker, sport, liga, event_id, evento,
            jogador_a, jogador_b,
            score_home, score_away, live_time,
            ts AS ultimo_tick
        FROM ticks
        WHERE {where}
        ORDER BY bookmaker, event_id, ts DESC
    """

    async with db() as conn:
        row = await conn.fetchrow(sql, *params)

    if not row:
        raise HTTPException(status_code=404, detail="Evento não encontrado")

    return dict(row)


@router.get("/{event_id}/odds")
async def get_evento_odds(event_id: str, bookmaker: Optional[str] = None):
    """Odds atuais de um evento."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=120)
    conditions = ["event_id = $1", "ts >= $2", "mercado_tipo != 'SCORE_UPDATE'", "odd_status = 0"]
    params = [event_id, cutoff]
    i = 3

    if bookmaker:
        conditions.append(f"bookmaker = ${i}"); params.append(bookmaker); i += 1

    where = " AND ".join(conditions)

    sql = f"""
        SELECT DISTINCT ON (bookmaker, selecao_id)
            bookmaker, mercado, mercado_tipo, linha,
            selecao, selecao_id, odds, odd_status, ts
        FROM ticks
        WHERE {where}
        ORDER BY bookmaker, selecao_id, ts DESC
    """

    async with db() as conn:
        rows = await conn.fetch(sql, *params)

    return [dict(r) for r in rows]


@router.get("/{event_id}/timeline")
async def get_evento_timeline(event_id: str, bookmaker: Optional[str] = None):
    """Placar ao longo do tempo (SCORE_UPDATEs)."""
    conditions = ["event_id = $1", "mercado_tipo = 'SCORE_UPDATE'"]
    params = [event_id]
    i = 2

    if bookmaker:
        conditions.append(f"bookmaker = ${i}"); params.append(bookmaker); i += 1

    where = " AND ".join(conditions)

    sql = f"""
        SELECT ts, bookmaker, score_home, score_away, live_time
        FROM ticks
        WHERE {where}
        ORDER BY ts ASC
    """

    async with db() as conn:
        rows = await conn.fetch(sql, *params)

    return [dict(r) for r in rows]
