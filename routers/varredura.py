# -*- coding: utf-8 -*-
r"""
routers/varredura.py — a varredura como job do painel.

A API aqui e' SO DESPACHANTE: valida, grava na fila e responde. Quem roda e' o
servico `workers/varredura_daemon.py`, em processo separado e com prioridade
baixa. Nenhum endpoint daqui segura o event loop — garimpo e' coisa de horas.

Registrar no main.py:
    from routers import varredura
    app.include_router(varredura.router)
"""
import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from database import get_pool
from security import get_current_user, acesso_total

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/varredura", tags=["varredura"])

MODOS = ("grosso", "completo", "total")
# o mesmo teto do worker, repetido aqui so pra avisar o usuario na hora de criar
TETO_AVISO = 400_000_000


# =============================================================== modelos ====
class CriarVarreduraRequest(BaseModel):
    job_backtest_id: int = Field(..., gt=0,
                                 description="backtest JA CONCLUIDO que vira a fonte")
    nome: Optional[str] = Field(default=None, max_length=120)
    modo: str = Field(default="completo")
    # "ult.10,ult.30,todas" — vazio = todas as janelas do arquivo
    janelas: Optional[str] = Field(default=None, max_length=200)
    min_apostas: Optional[int] = Field(default=None, ge=1, le=100000)
    guardar: Optional[int] = Field(default=None, ge=100, le=200000)
    # em TOTAL DE PONTOS quem decide e' o TETO de linha — por isso o nlmax
    nlmax: Optional[int] = Field(default=None, ge=1, le=60)
    nlin: Optional[int] = Field(default=None, ge=1, le=60)
    placebo: Optional[int] = Field(default=None, ge=1, le=50)
    sem_odd: bool = Field(default=False)
    # PRE-COMPROMISSO. Vazio = o worker separa 30% do fim como holdout.
    data_corte: Optional[str] = Field(default=None,
                                      description="AAAA-MM-DD; a busca so ve ate aqui")
    # pula a parada em 'planejado' mesmo se a estimativa for enorme
    confirmado: bool = Field(default=False)


# ============================================================== auxiliares ==
def _pode_ver(usuario, dono_id):
    return acesso_total(usuario) or dono_id == usuario.get("id")


async def _buscar(conn, job_id, usuario):
    row = await conn.fetchrow("SELECT * FROM varredura_jobs WHERE id = $1", job_id)
    if row is None or not _pode_ver(usuario, row["user_id"]):
        raise HTTPException(status_code=404, detail="varredura nao encontrada")
    return row


def _json(v, padrao=None):
    if v is None:
        return padrao
    if isinstance(v, str):
        try:
            return json.loads(v or "null")
        except Exception:
            return padrao
    return v


def _linha(row, completo=False):
    d = {
        "id": row["id"], "nome": row["nome"], "status": row["status"],
        "progresso": row["progresso"], "progresso_msg": row["progresso_msg"],
        "job_backtest_id": row["job_backtest_id"], "erro": row["erro"],
        "criado_em": row["criado_em"], "iniciado_em": row["iniciado_em"],
        "concluido_em": row["concluido_em"],
        "tem_saida": bool(row["arquivo_tudo"]),
        "tem_holdout": bool(row["arquivo_holdout"]),
    }
    if completo:
        d["params"] = _json(row["params"], {})
        d["contrato"] = _json(row["contrato"])
        d["resumo"] = _json(row["resumo"])
        d["data_corte"] = row["data_corte"]
    return d


# ================================================================ endpoints ==
@router.get("/origens")
async def listar_origens(limite: int = Query(50, ge=1, le=200),
                         usuario: dict = Depends(get_current_user)):
    """Backtests que servem de fonte pro garimpo.

    `escancarado` = job SEM filtro (sem chip, sem linha, sem teto). E' a fonte
    certa: se o job de origem ja veio filtrado, a busca so procura DENTRO da
    estrategia dele e nunca fora dela. O front mostra isso como aviso.
    """
    # A coluna de data da backtest_jobs varia entre instalacoes (criado_em /
    # iniciado_em / nenhuma das duas). Em vez de chutar — o que derruba o
    # endpoint com 500, e no browser aparece como "sem conexao" porque a
    # resposta de erro sai sem os headers de CORS — descubro no catalogo e
    # monto o SELECT com o que existe de fato.
    pool = get_pool()
    async with pool.acquire() as conn:
        col_data = await conn.fetchval(
            """SELECT column_name FROM information_schema.columns
                WHERE table_name = 'backtest_jobs'
                  AND column_name IN ('criado_em', 'iniciado_em', 'created_at')
                ORDER BY CASE column_name
                           WHEN 'criado_em'   THEN 1
                           WHEN 'iniciado_em' THEN 2
                           ELSE 3 END
                LIMIT 1""")
        campo_data = f"{col_data} AS criado_em" if col_data else "NULL AS criado_em"
        base_sql = f"""SELECT id, {campo_data}, total_apostas, bot_snapshot, user_id
                         FROM backtest_jobs
                        WHERE status = 'concluido' AND total_apostas >= 500"""
        if acesso_total(usuario):
            rows = await conn.fetch(
                base_sql + " ORDER BY id DESC LIMIT $1", limite)
        else:
            rows = await conn.fetch(
                base_sql + " AND user_id = $2 ORDER BY id DESC LIMIT $1",
                limite, usuario.get("id"))
    saida = []
    for r in rows:
        # linha torta nao derruba a lista inteira
        snap = _json(r["bot_snapshot"], {}) or {}
        f = snap.get("filtros") or {}
        filtrado = bool(f.get("filtrosHistAdicionados") or f.get("folgaAtivo")
                        or snap.get("linha_min") or snap.get("linha_max")
                        or snap.get("max_apostas_partida"))
        try:
            saida.append({
                "job_id": r["id"], "criado_em": r["criado_em"],
                "apostas": r["total_apostas"],
                "mercado": snap.get("mercado"), "casa": snap.get("casa"),
                "esporte": snap.get("esporte"),
                "escancarado": not filtrado,
            })
        except Exception:
            logger.exception(f"[varredura] origem {r['id']} ilegivel — pulando")
    return saida


@router.post("/jobs")
async def criar_varredura(req: CriarVarreduraRequest,
                          usuario: dict = Depends(get_current_user)):
    """Cria o job e devolve na hora. Quem roda e' o daemon."""
    if req.modo not in MODOS:
        raise HTTPException(status_code=400,
                            detail=f"modo invalido; use um de {MODOS}")
    # coluna DATE: o asyncpg exige datetime.date, nao string
    corte = None
    if req.data_corte:
        try:
            from datetime import date as _date
            corte = _date.fromisoformat(req.data_corte)
        except Exception:
            raise HTTPException(status_code=400,
                                detail="data_corte deve ser AAAA-MM-DD")

    pool = get_pool()
    async with pool.acquire() as conn:
        org = await conn.fetchrow(
            """SELECT id, status, total_apostas, user_id
                 FROM backtest_jobs WHERE id = $1""", req.job_backtest_id)
        if org is None or not _pode_ver(usuario, org["user_id"]):
            raise HTTPException(status_code=404, detail="backtest nao encontrado")
        if org["status"] != "concluido":
            raise HTTPException(
                status_code=400,
                detail=f"o backtest {org['id']} esta '{org['status']}' — "
                       "so da' pra garimpar em cima de job concluido")
        if (org["total_apostas"] or 0) < 500:
            raise HTTPException(
                status_code=400,
                detail=f"o backtest {org['id']} tem so {org['total_apostas']} "
                       "apostas; pouco pra garimpar")

        # nao deixa empilhar a mesma coisa duas vezes por engano
        dup = await conn.fetchval(
            """SELECT id FROM varredura_jobs
                WHERE job_backtest_id = $1
                  AND status IN ('pendente', 'planejando', 'planejado', 'rodando')
                LIMIT 1""", req.job_backtest_id)
        if dup:
            raise HTTPException(
                status_code=409,
                detail=f"ja existe a varredura {dup} na fila para este backtest")

        params = {k: v for k, v in req.model_dump().items()
                  if k not in ("job_backtest_id", "nome", "data_corte")
                  and v is not None and v is not False}
        job_id = await conn.fetchval(
            """INSERT INTO varredura_jobs
                   (user_id, job_backtest_id, nome, params, data_corte, status)
               VALUES ($1, $2, $3, $4::jsonb, $5, 'pendente')
            RETURNING id""",
            usuario.get("id"), req.job_backtest_id,
            req.nome or f"garimpo do job {req.job_backtest_id}",
            json.dumps(params),
            corte)

        na_frente = await conn.fetchval(
            "SELECT COUNT(*) FROM varredura_jobs WHERE status='pendente' AND id < $1",
            job_id)

    logger.info(f"[varredura] job {job_id} na fila (origem {req.job_backtest_id})")
    return {"id": job_id, "status": "pendente", "na_frente": na_frente,
            "aviso": ("modo 'total' com muitas janelas pode levar horas — o job "
                      "para e pede confirmacao se a estimativa for muito alta")
            if req.modo == "total" and not req.confirmado else None}


@router.get("/jobs")
async def listar(limite: int = Query(30, ge=1, le=200),
                 usuario: dict = Depends(get_current_user)):
    pool = get_pool()
    async with pool.acquire() as conn:
        if acesso_total(usuario):
            rows = await conn.fetch(
                "SELECT * FROM varredura_jobs ORDER BY id DESC LIMIT $1", limite)
        else:
            rows = await conn.fetch(
                """SELECT * FROM varredura_jobs WHERE user_id = $2
                    ORDER BY id DESC LIMIT $1""", limite, usuario.get("id"))
    return [_linha(r) for r in rows]


@router.get("/jobs/{job_id}")
async def detalhe(job_id: int, usuario: dict = Depends(get_current_user)):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await _buscar(conn, job_id, usuario)
    return _linha(row, completo=True)


@router.post("/jobs/{job_id}/confirmar")
async def confirmar(job_id: int, usuario: dict = Depends(get_current_user)):
    """Libera um job que parou em 'planejado' (estimativa alta). O contrato ja
    esta gravado, entao a tela mostra o que vai rodar ANTES do OK."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await _buscar(conn, job_id, usuario)
        if row["status"] != "planejado":
            raise HTTPException(
                status_code=400,
                detail=f"a varredura {job_id} esta '{row['status']}', "
                       "nao ha o que confirmar")
        # A confirmacao PRECISA morar nos params, nao no status. O worker
        # deduzia "confirmado" de status=='planejado', mas aqui o status vira
        # 'pendente' -> o daemon marca 'planejando' -> o worker rele, acha que
        # nao foi confirmado, refaz o plano, ve a estimativa alta e para em
        # 'planejado' outra vez. Laco infinito: confirmar nunca rodava.
        await conn.execute(
            """UPDATE varredura_jobs
                  SET status = 'pendente', erro = NULL, progresso = 0,
                      params = jsonb_set(COALESCE(params, '{}'::jsonb),
                                         '{confirmado}', 'true'::jsonb),
                      progresso_msg = 'confirmada, aguardando slot'
                WHERE id = $1""", job_id)
    return {"id": job_id, "status": "pendente"}


@router.post("/jobs/{job_id}/cancelar")
async def cancelar(job_id: int, usuario: dict = Depends(get_current_user)):
    """Marca como cancelado; o daemon mata o processo no proximo ciclo."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await _buscar(conn, job_id, usuario)
        if row["status"] in ("concluido", "erro", "cancelado"):
            raise HTTPException(status_code=400,
                                detail=f"a varredura ja esta '{row['status']}'")
        await conn.execute(
            """UPDATE varredura_jobs
                  SET status = 'cancelado', concluido_em = NOW(),
                      progresso_msg = 'cancelada'
                WHERE id = $1""", job_id)
    return {"id": job_id, "status": "cancelado"}


@router.get("/jobs/{job_id}/download")
async def baixar(job_id: int,
                 tipo: str = Query("xlsx", pattern="^(xlsx|tudo|holdout)$"),
                 usuario: dict = Depends(get_current_user)):
    """xlsx = as abas com os rankings · tudo = o csv completo (fonte da verdade)
    · holdout = as mesmas configs medidas nos dias que a busca NAO viu."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await _buscar(conn, job_id, usuario)
    campo = {"xlsx": "arquivo_saida", "tudo": "arquivo_tudo",
             "holdout": "arquivo_holdout"}[tipo]
    caminho = row[campo]
    if not caminho or not os.path.isfile(caminho):
        raise HTTPException(
            status_code=404,
            detail=(f"a varredura {job_id} nao tem arquivo '{tipo}' "
                    f"(status: {row['status']})"))
    return FileResponse(caminho, filename=os.path.basename(caminho))
