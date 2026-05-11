"""
routers/backtest.py - Endpoints da API pra backtest dos bots.

Endpoints:
    POST /backtest/jobs              cria + dispara worker async
    GET  /backtest/jobs/{job_id}     polling pra UI
    GET  /backtest/bot/{bot_id}      historico do bot
    DELETE /backtest/jobs/{job_id}   cancela/remove job

Aplicar no VPS:
    1. Salvar em routers/backtest.py
    2. Editar main.py: from routers import backtest + app.include_router(backtest.router)
    3. nssm restart TipMikeAPI
"""

from datetime import date, datetime
from typing import Any, Optional
import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from database import get_pool
from workers.backtest_runner import executar_backtest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/backtest", tags=["backtest"])


# ============================================================
# Pydantic models
# ============================================================

class BacktestCreateRequest(BaseModel):
    bot_id: int = Field(..., gt=0)
    data_inicio: date
    data_fim: date
    stake_modo: str = Field(default="fixo")
    stake_valor: float = Field(..., gt=0)
    banca_inicial: float = Field(default=1000.00, gt=0)

    @field_validator("stake_modo")
    @classmethod
    def validar_modo(cls, v: str) -> str:
        if v not in ("fixo", "ratchet"):
            raise ValueError("stake_modo deve ser fixo ou ratchet")
        return v

    @field_validator("data_fim")
    @classmethod
    def validar_periodo(cls, v: date, info) -> date:
        ini = info.data.get("data_inicio")
        if ini and v < ini:
            raise ValueError("data_fim nao pode ser anterior a data_inicio")
        if ini and (v - ini).days > 365:
            raise ValueError("Periodo maximo do backtest: 365 dias")
        return v


# ============================================================
# Helpers
# ============================================================

def _row_to_job_dict(row: Any, incluir_detalhe: bool = False) -> dict:
    if row is None:
        return None

    d = dict(row)

    # Decodifica JSONB se vier como string
    for k in ("bot_snapshot", "equity_curve", "apostas_detalhe", "pnl_por_dia"):
        v = d.get(k)
        if isinstance(v, str):
            try:
                d[k] = json.loads(v)
            except Exception:
                pass

    if not incluir_detalhe:
        d.pop("equity_curve", None)
        d.pop("apostas_detalhe", None)
        d.pop("bot_snapshot", None)

    # Numerics em float
    for k in ("stake_valor", "banca_inicial", "pnl", "roi", "win_rate", "drawdown_max"):
        if d.get(k) is not None:
            d[k] = float(d[k])

    return d


# ============================================================
# Endpoints
# ============================================================

@router.post("/jobs")
async def criar_job(req: BacktestCreateRequest, background: BackgroundTasks):
    pool = get_pool()
    async with pool.acquire() as conn:
        bot_row = await conn.fetchrow(
            "SELECT id, nome, casa, esporte, mercado, torneios, torneios_excluir, "
            "linha_min, linha_max, odd_min, odd_max, whitelist_pares, blacklist_pares, "
            "whitelist_cenarios, max_apostas_partida, filtros "
            "FROM bots WHERE id = $1",
            req.bot_id,
        )
        if not bot_row:
            raise HTTPException(status_code=404, detail=f"Bot {req.bot_id} nao encontrado")

        bot_dict = dict(bot_row)
        for k in ("torneios", "torneios_excluir", "whitelist_pares", "blacklist_pares",
                  "whitelist_cenarios", "filtros"):
            v = bot_dict.get(k)
            if isinstance(v, str):
                try:
                    bot_dict[k] = json.loads(v)
                except Exception:
                    pass

        job_id = await conn.fetchval(
            """
            INSERT INTO backtest_jobs
                (bot_id, data_inicio, data_fim, stake_modo, stake_valor,
                 banca_inicial, bot_snapshot, status, progresso)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, 'pendente', 0)
            RETURNING id
            """,
            req.bot_id, req.data_inicio, req.data_fim,
            req.stake_modo, req.stake_valor, req.banca_inicial,
            json.dumps(bot_dict, default=str),
        )

    logger.info(f"[backtest] Job {job_id} criado para bot {req.bot_id}")

    # Dispara worker async (nao bloqueia resposta)
    background.add_task(executar_backtest, job_id)

    return {"job_id": job_id, "status": "pendente"}


@router.get("/jobs/{job_id}")
async def get_job(job_id: int, incluir_detalhe: bool = Query(default=False)):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM backtest_jobs WHERE id = $1", job_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Job {job_id} nao encontrado")
    return _row_to_job_dict(row, incluir_detalhe=incluir_detalhe)


@router.get("/bot/{bot_id}")
async def listar_jobs_do_bot(bot_id: int, limit: int = Query(default=10, le=50)):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM backtest_jobs WHERE bot_id = $1 "
            "ORDER BY iniciado_em DESC LIMIT $2",
            bot_id, limit,
        )
    return [_row_to_job_dict(r, incluir_detalhe=False) for r in rows]


@router.delete("/jobs/{job_id}")
async def deletar_job(job_id: int):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status FROM backtest_jobs WHERE id = $1", job_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Job {job_id} nao encontrado")

        if row["status"] in ("concluido", "erro", "pendente"):
            await conn.execute("DELETE FROM backtest_jobs WHERE id = $1", job_id)
            return {"deleted": True}
        else:
            # Em rodando: sinaliza cancelamento. Worker checa antes de cada update.
            await conn.execute(
                "UPDATE backtest_jobs SET status='erro', erro='Cancelado pelo usuario', "
                "concluido_em=NOW() WHERE id = $1",
                job_id,
            )
            return {"cancelled": True}
