"""
routers/backtest_upload.py - Endpoints do backtest por UPLOAD de arquivo parquet.

Complementa o routers/backtest.py existente (que roda backtest lendo ticks do
BANCO por periodo). Aqui os ticks vem de um ARQUIVO parquet upado do HD.
O h2h continua vindo do banco com cutoff no ts (decidido).

Endpoints:
    POST /backtest/upload-ticks      sobe o parquet -> upload_id + resumo
    POST /backtest/jobs-upload       cria job usando o upload_id -> dispara worker

Aplicar no VPS:
    1. Salvar em routers/backtest_upload.py
    2. main.py: from routers import backtest_upload
                app.include_router(backtest_upload.router)
    3. nssm restart TipMikeAPI

Depende de:
    - workers/backtest_upload.py  (parse_ticks_parquet, salvar_upload)
    - tabela backtest_jobs com coluna nova: upload_id TEXT (ver migration abaixo)
    - executar_backtest aceitar fonte=arquivo (peca 3 - ainda a fazer)

MIGRATION necessaria (rodar uma vez):
    ALTER TABLE backtest_jobs ADD COLUMN IF NOT EXISTS upload_id TEXT;
"""

import json
import logging
from typing import Optional

from fastapi import (APIRouter, BackgroundTasks, File, HTTPException,
                     UploadFile)
from pydantic import BaseModel, Field

from database import get_pool
from workers.backtest_runner import executar_backtest
from workers.backtest_upload import (salvar_upload, parse_ticks_parquet,
                                     BacktestUploadError)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/backtest", tags=["backtest"])

# Limite defensivo de leitura do upload (alinha com MAX_PARQUET_BYTES do worker).
# 500MB. Evita estourar memoria do processo da API.
_MAX_UPLOAD_BYTES = 500 * 1024 * 1024


# ============================================================
# POST /backtest/upload-ticks
# ============================================================

@router.post("/upload-ticks")
async def upload_ticks(arquivo: UploadFile = File(...)):
    """
    Recebe um .parquet de ticks, salva, valida e devolve um resumo + upload_id.
    A UI mostra o resumo (linhas, periodo, ligas) antes de o usuario rodar o job.

    BLINDADO: valida extensao, tamanho, conteudo e parsing. Qualquer falha
    previsivel vira HTTP 400 com mensagem clara (nao 500 generico).
    """
    nome = (arquivo.filename or "").strip() or "ticks.parquet"
    if not nome.lower().endswith(".parquet"):
        raise HTTPException(status_code=400, detail="Envie um arquivo .parquet")

    # leitura defensiva: limita tamanho pra nao estourar memoria
    try:
        conteudo = await arquivo.read()
    except Exception as e:
        logger.exception("[backtest_upload] falha ao ler upload")
        raise HTTPException(status_code=400, detail=f"Falha ao receber arquivo: {e}")
    finally:
        try:
            await arquivo.close()
        except Exception:
            pass

    if not conteudo:
        raise HTTPException(status_code=400, detail="Arquivo vazio")
    if len(conteudo) > _MAX_UPLOAD_BYTES:
        mb = len(conteudo) / 1024 / 1024
        raise HTTPException(
            status_code=413,
            detail=f"Arquivo muito grande ({mb:.0f}MB). Limite: 500MB",
        )

    # salva no storage temporario -> upload_id
    try:
        upload_id = salvar_upload(conteudo, nome)
    except BacktestUploadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("[backtest_upload] erro inesperado ao salvar")
        raise HTTPException(status_code=500, detail=f"Erro ao salvar: {e}")

    # le pra montar o resumo (sem filtro de bot - so panorama)
    try:
        ticks = parse_ticks_parquet(upload_id, bot=None)
    except BacktestUploadError as e:
        # parquet invalido: avisa claro (400), arquivo ja foi salvo mas e inutil
        raise HTTPException(status_code=400, detail=f"Parquet invalido: {e}")
    except Exception as e:
        logger.exception("[backtest_upload] erro inesperado ao ler parquet")
        raise HTTPException(status_code=500, detail=f"Erro ao ler parquet: {e}")

    if not ticks:
        return {
            "upload_id": upload_id,
            "arquivo": nome,
            "linhas": 0,
            "aviso": "arquivo sem ticks apos leitura",
        }

    # resumo: range de datas, casas, esportes, ligas distintas
    try:
        ts_vals = [t["ts"] for t in ticks if t.get("ts") is not None]
        casas = sorted({t.get("bookmaker") for t in ticks if t.get("bookmaker")})
        esportes = sorted({t.get("sport") for t in ticks if t.get("sport")})
        ligas = sorted({t.get("liga") for t in ticks if t.get("liga")})
    except Exception as e:
        logger.exception("[backtest_upload] erro ao montar resumo")
        raise HTTPException(status_code=500, detail=f"Erro ao resumir: {e}")

    return {
        "upload_id": upload_id,
        "arquivo": nome,
        "linhas": len(ticks),
        "ts_min": str(min(ts_vals)) if ts_vals else None,
        "ts_max": str(max(ts_vals)) if ts_vals else None,
        "casas": casas,
        "esportes": esportes,
        "ligas": ligas[:20],
    }


# ============================================================
# POST /backtest/jobs-upload
# ============================================================

class BacktestUploadJobRequest(BaseModel):
    bot_id: int = Field(..., gt=0)
    upload_id: str = Field(..., min_length=1)
    stake_modo: str = Field(default="fixo")
    stake_valor: float = Field(..., gt=0)
    banca_inicial: float = Field(default=1000.00, gt=0)


@router.post("/jobs-upload")
async def criar_job_upload(req: BacktestUploadJobRequest, background: BackgroundTasks):
    """
    Cria um job de backtest usando os ticks do ARQUIVO (upload_id), nao do banco.
    Mesmo fluxo do POST /jobs: snapshot do bot, insere job, dispara worker.
    A diferenca e que grava upload_id - o worker, vendo ele preenchido, le do
    arquivo em vez do banco (peca 3).

    BLINDADO: valida que o upload existe ANTES de criar o job (nao cria job
    fadado a falhar), trata bot inexistente e erro de DB (ex: coluna upload_id
    ausente -> avisa pra rodar a migration).
    """
    # 1) o upload existe e e legivel? (falha cedo, antes de criar job)
    try:
        from workers.backtest_upload import caminho_do_upload
        caminho_do_upload(req.upload_id)
    except BacktestUploadError as e:
        raise HTTPException(status_code=400, detail=f"upload_id invalido: {e}")
    except Exception as e:
        logger.exception("[backtest_upload] erro ao validar upload_id")
        raise HTTPException(status_code=400, detail=f"upload_id invalido: {e}")

    pool = get_pool()
    try:
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

            try:
                job_id = await conn.fetchval(
                    """
                    INSERT INTO backtest_jobs
                        (bot_id, data_inicio, data_fim, stake_modo, stake_valor,
                         banca_inicial, bot_snapshot, status, progresso, upload_id)
                    VALUES ($1, NULL, NULL, $2, $3, $4, $5::jsonb, 'pendente', 0, $6)
                    RETURNING id
                    """,
                    req.bot_id, req.stake_modo, req.stake_valor, req.banca_inicial,
                    json.dumps(bot_dict, default=str), req.upload_id,
                )
            except Exception as e:
                # erro comum: coluna upload_id ainda nao existe -> avisa pra migrar
                msg = str(e)
                if "upload_id" in msg and ("column" in msg.lower() or "coluna" in msg.lower()):
                    raise HTTPException(
                        status_code=500,
                        detail="Coluna upload_id ausente. Rode a migration: "
                               "ALTER TABLE backtest_jobs ADD COLUMN IF NOT EXISTS upload_id TEXT;",
                    )
                logger.exception("[backtest_upload] erro ao inserir job")
                raise HTTPException(status_code=500, detail=f"Erro ao criar job: {e}")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[backtest_upload] erro de DB ao criar job")
        raise HTTPException(status_code=500, detail=f"Erro de banco: {e}")

    logger.info(f"[backtest_upload] Job {job_id} (upload) criado p/ bot {req.bot_id}")
    background.add_task(executar_backtest, job_id)
    return {"job_id": job_id, "status": "pendente", "fonte": "arquivo"}


# ============================================================
# POST /backtest/validar-cruzado
# ============================================================

class ValidarCruzadoRequest(BaseModel):
    bot_id: int = Field(..., gt=0)
    upload_id: str = Field(..., min_length=1)


@router.post("/validar-cruzado")
async def validar_cruzado_endpoint(req: ValidarCruzadoRequest):
    """
    Compara o arquivo upado com o banco (amostra) pra detectar divergencia ANTES
    de rodar o backtest. Retorna relatorio: quantos so estao no arquivo, quantos
    placares divergem, e se esta dentro do tolerado. A UI mostra como aviso.

    BLINDADO: bot inexistente, upload invalido, erro de banco - tudo tratado.
    """
    from workers.backtest_upload import validar_cruzado, BacktestUploadError

    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            bot_row = await conn.fetchrow(
                "SELECT casa, esporte, torneios, torneios_excluir FROM bots WHERE id=$1",
                req.bot_id,
            )
            if not bot_row:
                raise HTTPException(status_code=404, detail=f"Bot {req.bot_id} nao encontrado")

            bot_dict = dict(bot_row)
            for k in ("torneios", "torneios_excluir"):
                v = bot_dict.get(k)
                if isinstance(v, str):
                    try:
                        bot_dict[k] = json.loads(v)
                    except Exception:
                        pass

            try:
                rel = await validar_cruzado(conn, req.upload_id, bot_dict)
            except BacktestUploadError as e:
                raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[backtest_upload] erro na validacao cruzada")
        raise HTTPException(status_code=500, detail=f"Erro na validacao: {e}")

    return rel
