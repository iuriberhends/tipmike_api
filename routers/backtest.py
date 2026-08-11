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

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from database import get_pool
from security import get_current_user, acesso_total
from workers.backtest_runner import executar_backtest

# ---------------------------------------------------------------------------
# v2 (03/ago) — JOB EM PROCESSO SEPARADO
# Medido: com o job rodando via BackgroundTasks (mesmo processo, mesmo event
# loop da API), /docs levou 10,7s com UM job ativo; com 4 empilhados a pagina
# nao carregava. A maquina tem 24 nucleos e 23 ficavam parados, porque tudo
# disputava um loop so.
# Agora a API so DESPACHA: cada job vira um processo (workers/run_job.py).
#   - API responde sempre (o loop nao faz trabalho de CPU);
#   - jobs rodam em paralelo de verdade, um nucleo cada;
#   - job que estoura nao leva a API junto.
# MAX_JOBS_PARALELOS segura a fila: sem teto, 10 jobs simultaneos brigariam
# por RAM e disco. Reversao: USAR_PROCESSO_SEPARADO = False volta ao antigo.
# ---------------------------------------------------------------------------
import asyncio
import os
import sys as _sys
from pathlib import Path as _Path

USAR_PROCESSO_SEPARADO = True
# Teto de jobs simultaneos. Cada processo carrega o proprio parquet em memoria
# (~2,5 GB num arquivo de 1,5M ticks), entao o limite real e' RAM, nao CPU:
# com 32 GB e o Postgres levando 6, 4 jobs ficam folgados e 6 e' o teto sao.
# Da pra ajustar sem editar codigo: variavel de ambiente BACKTEST_MAX_PARALELO.
MAX_JOBS_PARALELOS = int(os.environ.get("BACKTEST_MAX_PARALELO", "4"))
_SEM_JOBS = asyncio.Semaphore(MAX_JOBS_PARALELOS)

# v3 (11/ago): CANCELAMENTO. _PROCS guarda o processo de cada job em execucao
# (pra matar); _CANCELADOS marca quem foi cancelado — inclusive o que ainda
# esta NA FILA, que ao chegar a vez simplesmente nao roda. Antes disso, job
# errado so terminava sozinho ou com restart da API (que matava os outros
# junto).
_PROCS: dict = {}
_CANCELADOS: set = set()
_RAIZ_API = _Path(__file__).resolve().parent.parent


async def _rodar_job_em_processo(job_id: int):
    """Sobe `python -m workers.run_job <id>` e espera. O semaforo limita
    quantos rodam ao mesmo tempo; os demais ficam na fila (status 'pendente'
    no banco, como sempre)."""
    async with _SEM_JOBS:
        if job_id in _CANCELADOS:
            # cancelado enquanto esperava a vez: nem chega a rodar
            _CANCELADOS.discard(job_id)
            logger.info(f"[backtest] job {job_id} cancelado na fila — nao rodou")
            return
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                   PYTHONUNBUFFERED="1")
        # LOG POR JOB: sem isto a saida do filho ia pro vazio e um job que
        # morresse de forma esquisita nao deixaria rastro pra autopsia.
        dir_logs = _RAIZ_API / "logs_jobs"
        try:
            dir_logs.mkdir(exist_ok=True)
        except Exception:
            pass
        arq_log = dir_logs / f"job_{job_id}.log"
        # PRIORIDADE ABAIXO DO NORMAL (Windows): o job e' importante mas nao
        # urgente; a API, os coletores e os bots continuam com passagem
        # preferencial mesmo com a maquina cheia. Nao deixa o job mais lento
        # quando ha nucleo sobrando — e sobra (24).
        criacao = getattr(__import__("subprocess"), "BELOW_NORMAL_PRIORITY_CLASS", 0)
        try:
            saida = open(arq_log, "ab", buffering=0)
        except Exception:
            saida = asyncio.subprocess.DEVNULL
        try:
            proc = await asyncio.create_subprocess_exec(
                _sys.executable, "-m", "workers.run_job", str(job_id),
                cwd=str(_RAIZ_API), env=env,
                stdout=saida, stderr=asyncio.subprocess.STDOUT,
                creationflags=criacao,
            )
            _PROCS[job_id] = proc
            rc = await proc.wait()
            logger.info(f"[backtest] job {job_id} terminou no processo "
                        f"{proc.pid} (codigo {rc}) — log em {arq_log.name}")
        except Exception:
            logger.exception(f"[backtest] falha ao rodar job {job_id} em "
                             f"processo separado — caindo pro modo antigo")
            await executar_backtest(job_id)
        finally:
            _PROCS.pop(job_id, None)
            _CANCELADOS.discard(job_id)
            try:
                if saida not in (None, asyncio.subprocess.DEVNULL):
                    saida.close()
            except Exception:
                pass


async def _limpar_orfaos():
    """Job marcado 'rodando' cujo processo morreu (restart da API, queda da
    VPS) ficava assim PRA SEMPRE — hoje a tabela tinha jobs 'rodando' ha 41
    horas, sujando o painel e a leitura de carga. Varre antes de criar job
    novo: barato e mantem a casa limpa."""
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            ids = await conn.fetch(
                """UPDATE backtest_jobs
                      SET status='erro',
                          erro='job orfao: o processo morreu (restart da API?)',
                          concluido_em=NOW()
                    WHERE status='rodando'
                      AND iniciado_em < NOW() - INTERVAL '3 hours'
                RETURNING id""")
        if ids:
            logger.info(f"[backtest] {len(ids)} job(s) orfao(s) fechados: "
                        f"{[r['id'] for r in ids]}")
    except Exception:
        logger.exception("[backtest] falha limpando orfaos (segue o jogo)")

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
async def criar_job(req: BacktestCreateRequest, background: BackgroundTasks, usuario: dict = Depends(get_current_user)):
    pool = get_pool()
    async with pool.acquire() as conn:
        bot_row = await conn.fetchrow(
            "SELECT id, user_id, nome, casa, esporte, mercado, torneios, torneios_excluir, "
            "linha_min, linha_max, odd_min, odd_max, whitelist_pares, blacklist_pares, "
            "whitelist_cenarios, max_apostas_partida, filtros "
            "FROM bots WHERE id = $1",
            req.bot_id,
        )
        if not bot_row:
            raise HTTPException(status_code=404, detail=f"Bot {req.bot_id} nao encontrado")
        if not acesso_total(usuario) and bot_row["user_id"] != usuario.get("id"):
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
                 banca_inicial, bot_snapshot, status, progresso, user_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, 'pendente', 0, $8)
            RETURNING id
            """,
            req.bot_id, req.data_inicio, req.data_fim,
            req.stake_modo, req.stake_valor, req.banca_inicial,
            json.dumps(bot_dict, default=str),
            usuario.get("id"),
        )

    logger.info(f"[backtest] Job {job_id} criado para bot {req.bot_id}")
    await _limpar_orfaos()

    # Dispara o worker. Em processo separado (padrao) a API nao trava;
    # o fallback mantem o comportamento antigo se a chave for desligada.
    if USAR_PROCESSO_SEPARADO:
        background.add_task(_rodar_job_em_processo, job_id)
    else:
        background.add_task(executar_backtest, job_id)

    return {"job_id": job_id, "status": "pendente"}


@router.post("/jobs/{job_id}/cancelar")
async def cancelar_job(job_id: int, usuario: dict = Depends(get_current_user)):
    """Para um backtest: mata o processo (ou tira da fila) e marca o job como
    'cancelado' no banco — status PROPRIO, pra nao virar 'erro' e sujar a
    leitura de quem falhou de verdade."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, user_id FROM backtest_jobs WHERE id=$1", job_id)
    if row is None:
        raise HTTPException(404, "job nao encontrado")
    if not acesso_total(usuario) and row["user_id"] != usuario.get("id"):
        raise HTTPException(404, "job nao encontrado")
    if row["status"] not in ("pendente", "rodando"):
        return {"ok": True, "status": row["status"], "aviso": "job ja terminou"}

    _CANCELADOS.add(job_id)
    proc = _PROCS.get(job_id)
    morto = False
    if proc is not None and proc.returncode is None:
        try:
            proc.kill()
            morto = True
        except Exception:
            logger.exception(f"[backtest] falha matando processo do job {job_id}")

    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE backtest_jobs
                  SET status='cancelado', erro='cancelado pelo usuario',
                      concluido_em=NOW()
                WHERE id=$1 AND status IN ('pendente','rodando')""", job_id)
    logger.info(f"[backtest] job {job_id} cancelado (processo morto={morto})")
    return {"ok": True, "status": "cancelado", "processo_morto": morto}


@router.get("/jobs/{job_id}")
async def get_job(job_id: int, incluir_detalhe: bool = Query(default=False), usuario: dict = Depends(get_current_user)):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM backtest_jobs WHERE id = $1", job_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Job {job_id} nao encontrado")
    if not acesso_total(usuario) and row["user_id"] != usuario.get("id"):
        raise HTTPException(status_code=404, detail=f"Job {job_id} nao encontrado")
    return _row_to_job_dict(row, incluir_detalhe=incluir_detalhe)


@router.get("/bot/{bot_id}")
async def listar_jobs_do_bot(bot_id: int, limit: int = Query(default=10, le=50), usuario: dict = Depends(get_current_user)):
    pool = get_pool()
    async with pool.acquire() as conn:
        dono = await conn.fetchrow("SELECT user_id FROM bots WHERE id = $1", bot_id)
        if not dono or (not acesso_total(usuario) and dono["user_id"] != usuario.get("id")):
            raise HTTPException(status_code=404, detail=f"Bot {bot_id} nao encontrado")
        rows = await conn.fetch(
            "SELECT * FROM backtest_jobs WHERE bot_id = $1 "
            "ORDER BY iniciado_em DESC LIMIT $2",
            bot_id, limit,
        )
    return [_row_to_job_dict(r, incluir_detalhe=False) for r in rows]


@router.delete("/jobs/{job_id}")
async def deletar_job(job_id: int, usuario: dict = Depends(get_current_user)):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status, user_id FROM backtest_jobs WHERE id = $1", job_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Job {job_id} nao encontrado")
        if not acesso_total(usuario) and row["user_id"] != usuario.get("id"):
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
