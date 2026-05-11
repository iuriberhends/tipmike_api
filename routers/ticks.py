"""
routers/ticks.py
GET /ticks, GET /ticks/count
"""

from fastapi import APIRouter, Query
from typing import Optional
from datetime import datetime, date
from database import db

router = APIRouter(prefix="/ticks", tags=["Ticks"])


@router.get("/count")
async def count_ticks(
    bookmaker: Optional[str] = None,
    sport: Optional[str] = None,
    liga: Optional[str] = None,
    jogador: Optional[str] = None,
    mercado_tipo: Optional[str] = None,
    data_inicio: Optional[date] = None,
    data_fim: Optional[date] = None,
    score_updates_only: bool = False,
):
    conditions = ["1=1"]
    params = []
    i = 1

    if bookmaker:
        conditions.append(f"bookmaker = ${i}"); params.append(bookmaker); i += 1
    if sport:
        conditions.append(f"sport ILIKE ${i}"); params.append(f"%{sport}%"); i += 1
    if liga:
        conditions.append(f"liga ILIKE ${i}"); params.append(f"%{liga}%"); i += 1
    if jogador:
        conditions.append(f"(jogador_a ILIKE ${i} OR jogador_b ILIKE ${i})")
        params.append(f"%{jogador}%"); i += 1
    if mercado_tipo:
        conditions.append(f"mercado_tipo = ${i}"); params.append(mercado_tipo); i += 1
    if data_inicio:
        conditions.append(f"ts >= ${i}"); params.append(datetime.combine(data_inicio, datetime.min.time())); i += 1
    if data_fim:
        conditions.append(f"ts <= ${i}"); params.append(datetime.combine(data_fim, datetime.max.time())); i += 1
    if score_updates_only:
        conditions.append("mercado_tipo = 'SCORE_UPDATE'")

    where = " AND ".join(conditions)
    async with db() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM ticks WHERE {where}", *params)

    return {"total": total}


@router.get("")
async def get_ticks(
    bookmaker: Optional[str] = None,
    sport: Optional[str] = None,
    liga: Optional[str] = None,
    jogador_a: Optional[str] = None,
    jogador_b: Optional[str] = None,
    jogador: Optional[str] = None,
    mercado_tipo: Optional[str] = None,
    event_id: Optional[str] = None,
    data_inicio: Optional[date] = None,
    data_fim: Optional[date] = None,
    score_updates_only: bool = False,
    limit: int = Query(default=100, le=1000),
    offset: int = 0,
):
    conditions = ["1=1"]
    params = []
    i = 1

    if bookmaker:
        conditions.append(f"bookmaker = ${i}"); params.append(bookmaker); i += 1
    if sport:
        conditions.append(f"sport ILIKE ${i}"); params.append(f"%{sport}%"); i += 1
    if liga:
        conditions.append(f"liga ILIKE ${i}"); params.append(f"%{liga}%"); i += 1
    if jogador_a:
        conditions.append(f"jogador_a ILIKE ${i}"); params.append(f"%{jogador_a}%"); i += 1
    if jogador_b:
        conditions.append(f"jogador_b ILIKE ${i}"); params.append(f"%{jogador_b}%"); i += 1
    if jogador:
        conditions.append(f"(jogador_a ILIKE ${i} OR jogador_b ILIKE ${i})")
        params.append(f"%{jogador}%"); i += 1
    if mercado_tipo:
        conditions.append(f"mercado_tipo = ${i}"); params.append(mercado_tipo); i += 1
    if event_id:
        conditions.append(f"event_id = ${i}"); params.append(event_id); i += 1
    if data_inicio:
        conditions.append(f"ts >= ${i}"); params.append(datetime.combine(data_inicio, datetime.min.time())); i += 1
    if data_fim:
        conditions.append(f"ts <= ${i}"); params.append(datetime.combine(data_fim, datetime.max.time())); i += 1
    if score_updates_only:
        conditions.append("mercado_tipo = 'SCORE_UPDATE'")

    where = " AND ".join(conditions)
    params.append(limit)
    params.append(offset)

    sql = f"""
        SELECT id, ts, bookmaker, sport, liga, event_id, evento,
               jogador_a, jogador_b, score_home, score_away, live_time,
               mercado, mercado_tipo, linha, selecao, odds, odd_status
        FROM ticks
        WHERE {where}
        ORDER BY ts DESC
        LIMIT ${i} OFFSET ${i+1}
    """

    async with db() as conn:
        rows = await conn.fetch(sql, *params)

    return [dict(r) for r in rows]
