"""
routers/sistema.py
GET /health, GET /version
"""

from fastapi import APIRouter
from database import db

router = APIRouter(tags=["Sistema"])

VERSION = "1.0.0"


@router.get("/health")
async def health():
    try:
        async with db() as conn:
            ticks_total = await conn.fetchval("SELECT COUNT(*) FROM ticks")
        db_status = "ok"
    except Exception as e:
        db_status = f"erro: {e}"
        ticks_total = 0

    return {
        "status": "ok",
        "versao": VERSION,
        "db": db_status,
        "ticks_total": ticks_total,
    }


@router.get("/version")
async def version():
    return {"versao": VERSION, "nome": "TipMike API"}
