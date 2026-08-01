# -*- coding: utf-8 -*-
"""
routers/h2h_sync.py — endpoints do botao "Analisar H2H" do backtest.

Fluxo em DUAS FASES (de proposito: nada e inserido sem o relatorio antes):
    POST /h2h-sync/analisar          -> job_id  (le ticks x h2h_historico)
    GET  /h2h-sync/{job_id}          -> status/progresso/relatorio
    POST /h2h-sync/{job_id}/preencher-> job_id novo (puxa da TM e insere)

Instalar:
    1. salvar como routers/h2h_sync.py
    2. salvar o worker como workers/h2h_sync.py
    3. em main.py:  from routers import h2h_sync
                    app.include_router(h2h_sync.router)
    4. credenciais da TM no ambiente do servico (NUNCA no arquivo):
       nssm set tipmikeapi AppEnvironmentExtra TM_EMAIL=... TM_SENHA=...
         TM_AES_KEY=... TM_APP_TOKEN=... TM_SUPABASE_URL=... TM_SUPABASE_ANON=...
    5. nssm restart tipmikeapi
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from database import get_pool
from security import get_current_user
from workers import h2h_sync

router = APIRouter(prefix="/h2h-sync", tags=["h2h-sync"])


class AnaliseRequest(BaseModel):
    upload_id: Optional[str] = Field(None, description="parquet do avulso: analisa os pares DO ARQUIVO (ignora a tabela ticks)")
    casa: Optional[str] = Field(None, description="bookmaker dos ticks (obrigatoria SEM upload_id)")
    esporte: str = Field("nba2k", description="nba2k/E-Basketball ou fifa/E-Football")
    liga: Optional[str] = Field(None, description="opcional: restringe a uma liga")
    dias: int = Field(15, ge=1, le=120)
    data_inicio: Optional[datetime] = None
    data_fim: Optional[datetime] = None
    min_confrontos: int = Field(h2h_sync.MIN_CONFRONTOS_PADRAO, ge=0, le=500)


class PreencherRequest(BaseModel):
    limite_pares: int = Field(h2h_sync.LIMITE_PARES_PADRAO, ge=1, le=500)
    dry_run: bool = Field(False, description="consulta a TM e diz quantos jogos "
                                             "ENTRARIAM, sem gravar nada")


def _job_ou_404(job_id: str) -> dict:
    job = h2h_sync.JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "job nao encontrado (a API pode ter reiniciado)")
    return job


@router.post("/analisar")
async def analisar(req: AnaliseRequest, background: BackgroundTasks,
                   usuario: dict = Depends(get_current_user)):
    if not req.upload_id and not req.casa:
        raise HTTPException(400, "informe casa ou upload_id")
    if not req.upload_id and h2h_sync.normalizar_esporte(req.esporte) is None:
        raise HTTPException(400, f"esporte nao suportado: {req.esporte}")
    params = req.model_dump()
    job_id = h2h_sync._novo_job("analise", params)
    background.add_task(h2h_sync.rodar_analise, get_pool(), params, job_id)
    return {"job_id": job_id, "status": "rodando"}


@router.get("/{job_id}")
async def status(job_id: str, usuario: dict = Depends(get_current_user)):
    job = _job_ou_404(job_id)
    # o relatorio de analise pode ter centenas de pares — devolve resumo +
    # so os que precisam (o painel nao precisa listar os que ja estao ok)
    rel = job.get("relatorio")
    if job["tipo"] == "analise" and isinstance(rel, dict) and "pares" in rel:
        rel = dict(rel)
        rel["pares"] = [p for p in rel["pares"] if p.get("precisa")][:300]
    return {**{k: v for k, v in job.items() if k != "relatorio"}, "relatorio": rel}


@router.post("/{job_id}/preencher")
async def preencher(job_id: str, req: PreencherRequest,
                    background: BackgroundTasks,
                    usuario: dict = Depends(get_current_user)):
    job = _job_ou_404(job_id)
    if job["tipo"] != "analise":
        raise HTTPException(400, "este job nao e uma analise")
    if job["status"] != "concluido" or not job.get("relatorio"):
        raise HTTPException(409, "a analise ainda nao terminou")
    if not job["relatorio"].get("pares_precisam"):
        raise HTTPException(409, "nada a preencher: o historico ja cobre os pares")
    try:
        h2h_sync._creds()
    except RuntimeError as e:
        raise HTTPException(503, str(e))

    novo = h2h_sync._novo_job("preenchimento", {"origem": job_id,
                                                "limite_pares": req.limite_pares})
    background.add_task(h2h_sync.rodar_preenchimento, get_pool(),
                        job["relatorio"], novo, req.limite_pares, req.dry_run)
    return {"job_id": novo, "status": "rodando", "dry_run": req.dry_run,
            "pares_na_fila": min(job["relatorio"]["pares_precisam"], req.limite_pares)}
