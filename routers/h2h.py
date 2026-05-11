"""
routers/h2h.py
GET /h2h, GET /h2h/{ja}/{jb}, GET /h2h/{ja}/{jb}/jogos
"""

from fastapi import APIRouter, Query, HTTPException
from typing import Optional
from database import db

router = APIRouter(prefix="/h2h", tags=["H2H"])


@router.get("")
async def buscar_h2h(
    ja: str = Query(..., description="Jogador A"),
    jb: str = Query(..., description="Jogador B"),
    bookmaker: Optional[str] = None,
    limit: int = Query(default=50, le=200),
):
    """Stats H2H entre dois jogadores."""
    return await _get_h2h_stats(ja, jb, bookmaker, limit)


@router.get("/{ja}/{jb}")
async def h2h_stats(
    ja: str,
    jb: str,
    bookmaker: Optional[str] = None,
    limit: int = Query(default=50, le=200),
):
    """Stats H2H agregados entre dois jogadores."""
    return await _get_h2h_stats(ja, jb, bookmaker, limit)


@router.get("/{ja}/{jb}/jogos")
async def h2h_jogos(
    ja: str,
    jb: str,
    bookmaker: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
):
    """Lista todos os jogos do par H2H."""
    conditions = [
        "(jogador_a ILIKE $1 OR jogador_b ILIKE $1)",
        "(jogador_a ILIKE $2 OR jogador_b ILIKE $2)",
        "mercado_tipo = 'SCORE_UPDATE'",
    ]
    params = [f"%{ja}%", f"%{jb}%"]
    i = 3

    if bookmaker:
        conditions.append(f"bookmaker = ${i}"); params.append(bookmaker); i += 1

    where = " AND ".join(conditions)
    params += [limit, offset]

    sql = f"""
        SELECT DISTINCT ON (event_id)
            ts, bookmaker, liga, event_id,
            jogador_a, jogador_b,
            score_home, score_away
        FROM ticks
        WHERE {where}
        ORDER BY event_id, ts DESC
        LIMIT ${i} OFFSET ${i+1}
    """

    async with db() as conn:
        rows = await conn.fetch(sql, *params)

    return [dict(r) for r in rows]


async def _get_h2h_stats(ja: str, jb: str, bookmaker: Optional[str], limit: int):
    conditions = [
        "(jogador_a ILIKE $1 OR jogador_b ILIKE $1)",
        "(jogador_a ILIKE $2 OR jogador_b ILIKE $2)",
        "mercado_tipo = 'SCORE_UPDATE'",
    ]
    params = [f"%{ja}%", f"%{jb}%"]
    i = 3

    if bookmaker:
        conditions.append(f"bookmaker = ${i}"); params.append(bookmaker); i += 1

    where = " AND ".join(conditions)
    params.append(limit)

    sql = f"""
        SELECT DISTINCT ON (event_id)
            ts, bookmaker, liga, event_id,
            jogador_a, jogador_b,
            score_home, score_away
        FROM ticks
        WHERE {where}
        ORDER BY event_id, ts DESC
        LIMIT ${i}
    """

    async with db() as conn:
        rows = await conn.fetch(sql, *params)

    if not rows:
        return {
            "jogador_a": ja,
            "jogador_b": jb,
            "total_jogos": 0,
            "jogos": [],
        }

    jogos = [dict(r) for r in rows]
    total = len(jogos)

    # Calcula médias de gols
    gols_ft = [
        (j["score_home"] or 0) + (j["score_away"] or 0)
        for j in jogos
        if j["score_home"] is not None and j["score_away"] is not None
    ]
    media_gols = round(sum(gols_ft) / len(gols_ft), 2) if gols_ft else None

    return {
        "jogador_a": ja,
        "jogador_b": jb,
        "total_jogos": total,
        "media_gols_ft": media_gols,
        "ultimo_jogo": jogos[0]["ts"] if jogos else None,
        "jogos": jogos,
    }
