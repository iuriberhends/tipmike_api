"""
routers/backtest_upload.py - Endpoints do backtest por UPLOAD de arquivo parquet.

Complementa o routers/backtest.py existente (que roda backtest lendo ticks do
BANCO por periodo). Aqui os ticks vem de um ARQUIVO parquet upado do HD.
O h2h continua vindo do banco com cutoff no ts (decidido).

Endpoints:
    POST /backtest/upload-ticks      sobe o parquet -> upload_id + resumo
    POST /backtest/jobs-upload       cria job usando o upload_id -> dispara worker
    POST /backtest/jobs-avulso       backtest standalone (filtros da aba, sem bot)

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
    -- p/ backtest avulso (sem bot), bot_id precisa aceitar NULL:
    ALTER TABLE backtest_jobs ALTER COLUMN bot_id DROP NOT NULL;
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


# ============================================================
# POST /backtest/jobs-avulso
# Backtest STANDALONE (sem bot): filtros vem da aba, ticks do upload.
# Monta um bot_snapshot "virtual" no formato que o worker ja entende.
# Tudo validado e blindado - entrada da aba nao pode gerar snapshot invalido.
# ============================================================

# Vocabulario valido (espelha o backtest_runner). Se o runner mudar, ajustar aqui.
_LADOS_VALIDOS = {"over", "under", "ambos"}
_CENARIOS_VALIDOS = {
    "", "casa_vencendo", "casa_perdendo", "empate",
    "casa_ou_empate", "visitante_ou_empate", "casa_ou_visitante",
}
_MERCADOS_VALIDOS = {
    "over_under_ft", "over_under_ht", "asian_over_under_ft", "asian_over_under_ht",
    "ml_ft", "ml_ht", "btts_ft", "ah_ft", "ah_ht",
    "correct_score", "double_chance_ft", "odd_even",
}
_QUARTOS_VALIDOS = {"q1", "q2", "q3", "q4"}
# limites defensivos (evita payload absurdo)
_MAX_NICKS = 500
_MAX_NICK_LEN = 80


class BacktestAvulsoRequest(BaseModel):
    upload_id: str = Field(..., min_length=1, max_length=200)
    # mercado escolhido na aba (ex: over_under_ft). lado: over/under/ambos.
    mercado: str = Field(..., min_length=1, max_length=40)
    lado: str = Field(default="ambos", max_length=10)
    casa: Optional[str] = Field(default=None, max_length=40)
    esporte: Optional[str] = Field(default=None, max_length=40)
    # WR do H2H (porcentagem): janela e minimo. janela 0 = todas.
    wr_min: Optional[float] = Field(default=None, ge=0, le=100)
    wr_janela: int = Field(default=0, ge=0, le=500)
    wr_min_partidas: int = Field(default=10, ge=0, le=10000)
    # placar: cenario + diferenca minima de gols/pontos
    cenario: Optional[str] = Field(default=None, max_length=30)
    diferenca_placar: Optional[int] = Field(default=None, ge=0, le=200)
    # tempo: quartos ativos (basket). Lista tipo ["q1","q2"].
    quartos: Optional[list] = None
    # linha (faixa)
    linha_min: Optional[float] = Field(default=None, ge=-1000, le=1000)
    linha_max: Optional[float] = Field(default=None, ge=-1000, le=1000)
    # black/white list de nicks (bloqueia/permite nick em QUALQUER posicao)
    blacklist: list = Field(default_factory=list)
    whitelist: list = Field(default_factory=list)
    # stake
    stake_modo: str = Field(default="fixo", max_length=20)
    stake_valor: float = Field(..., gt=0, le=1_000_000)
    banca_inicial: float = Field(default=1000.00, gt=0, le=1_000_000_000)


def _limpar_nicks(lista, campo: str) -> list:
    """Normaliza uma lista de nicks: aceita so strings nao-vazias, faz strip,
    limita tamanho e quantidade. Ignora itens invalidos em vez de quebrar."""
    if not lista:
        return []
    if not isinstance(lista, (list, tuple)):
        raise HTTPException(status_code=400, detail=f"{campo} deve ser uma lista de nicks")
    out = []
    vistos = set()
    for item in lista:
        if item is None:
            continue
        try:
            s = str(item).strip()
        except Exception:
            continue
        if not s:
            continue
        if len(s) > _MAX_NICK_LEN:
            s = s[:_MAX_NICK_LEN]
        chave = s.lower()
        if chave in vistos:   # dedup case-insensitive
            continue
        vistos.add(chave)
        out.append(s)
        if len(out) >= _MAX_NICKS:
            break
    return out


def _validar_e_normalizar(req: "BacktestAvulsoRequest") -> dict:
    """Valida os filtros da aba e devolve um dict ja normalizado pronto pro
    snapshot. Levanta HTTPException 400 com mensagem clara se algo invalido.
    Centraliza TODA a validacao (o snapshot so recebe valores limpos)."""
    # lado
    lado = (req.lado or "ambos").strip().lower()
    if lado not in _LADOS_VALIDOS:
        raise HTTPException(status_code=400,
                            detail=f"lado invalido: '{lado}'. Use over, under ou ambos.")
    # mercado
    mercado = (req.mercado or "").strip().lower()
    if mercado not in _MERCADOS_VALIDOS:
        raise HTTPException(status_code=400,
                            detail=f"mercado invalido: '{mercado}'.")
    # cenario
    cenario = (req.cenario or "").strip().lower()
    if cenario not in _CENARIOS_VALIDOS:
        raise HTTPException(status_code=400,
                            detail=f"cenario invalido: '{cenario}'.")
    # linha: se ambas vierem, min <= max
    if (req.linha_min is not None and req.linha_max is not None
            and req.linha_min > req.linha_max):
        raise HTTPException(status_code=400,
                            detail="linha_min nao pode ser maior que linha_max.")
    # quartos: aceita so q1..q4, normaliza
    quartos = None
    if req.quartos:
        if not isinstance(req.quartos, (list, tuple)):
            raise HTTPException(status_code=400, detail="quartos deve ser uma lista.")
        q_norm = set()
        for x in req.quartos:
            try:
                qx = str(x).strip().lower()
            except Exception:
                continue
            if qx in _QUARTOS_VALIDOS:
                q_norm.add(qx)
        # se mandou quartos mas nenhum valido, e erro (evita filtro vazio silencioso)
        if not q_norm:
            raise HTTPException(status_code=400,
                                detail="quartos sem nenhum valor valido (use q1..q4).")
        quartos = q_norm
    # nicks
    blacklist = _limpar_nicks(req.blacklist, "blacklist")
    whitelist = _limpar_nicks(req.whitelist, "whitelist")

    return {
        "lado": lado, "mercado": mercado, "cenario": cenario,
        "quartos": quartos, "blacklist": blacklist, "whitelist": whitelist,
    }


def _montar_snapshot_avulso(req: "BacktestAvulsoRequest", norm: dict) -> dict:
    """Converte os filtros (ja validados em `norm`) num bot_snapshot que o
    worker ja entende. Nao valida nada aqui - so monta (a validacao foi antes)."""
    filtros: dict = {}

    # lado (ambos = nao restringe)
    if norm["lado"] in ("over", "under"):
        filtros["lados"] = [norm["lado"]]
        filtros["inner"] = [norm["lado"].capitalize()]

    # WR do H2H -> filtrosHistAdicionados (formato que o worker normaliza)
    if req.wr_min is not None:
        janela_str = "all" if not req.wr_janela else f"last_{int(req.wr_janela)}"
        filtros["filtrosHistAdicionados"] = [{
            "base": "match",
            "prob": [float(req.wr_min), 100],
            "tipo": "all",
            "janela": janela_str,
            "versao": "all",
            "minPartidas": int(req.wr_min_partidas),
        }]

    # placar: cenario + diferenca
    if norm["cenario"]:
        filtros["cenarioPartida"] = norm["cenario"]
        filtros["cenarioPartidaAtivo"] = True
    if req.diferenca_placar is not None and req.diferenca_placar > 0:
        filtros["diferencaPlacar"] = int(req.diferenca_placar)
        filtros["diferencaPlacarAtivo"] = True

    # tempo: quartos (basket)
    if norm["quartos"]:
        filtros["quartosAtivos"] = {
            f"q{i}": (f"q{i}" in norm["quartos"]) for i in range(1, 5)
        }

    # black/white list de nicks -> entries {j1: nick} (worker checa qualquer posicao)
    blacklist_pares = [{"j1": n} for n in norm["blacklist"]]
    whitelist_pares = [{"j1": n} for n in norm["whitelist"]]

    return {
        "nome": "Backtest avulso",
        "casa": (req.casa or None),
        "esporte": (req.esporte or None),
        "mercado": norm["mercado"],
        "linha_min": req.linha_min,
        "linha_max": req.linha_max,
        "odd_min": None,
        "odd_max": None,
        "torneios": [],
        "torneios_excluir": [],
        "whitelist_pares": whitelist_pares,
        "blacklist_pares": blacklist_pares,
        "whitelist_cenarios": [],
        "max_apostas_partida": None,
        "filtros": filtros,
    }


async def _rodar_backtest_seguro(job_id: int):
    """Wrapper do executar_backtest pro BackgroundTask. Se o worker estourar,
    marca o job como 'erro' no banco (em vez de deixar 'pendente' pra sempre =
    job zumbi que a UI fica esperando eternamente)."""
    try:
        await executar_backtest(job_id)
    except Exception as e:
        logger.exception(f"[backtest_upload] job avulso {job_id} estourou no worker")
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE backtest_jobs SET status='erro', erro=$2 WHERE id=$1",
                    job_id, f"Falha no worker: {str(e)[:300]}",
                )
        except Exception:
            logger.exception(f"[backtest_upload] nao consegui marcar job {job_id} como erro")


@router.post("/jobs-avulso")
async def criar_job_avulso(req: BacktestAvulsoRequest, background: BackgroundTasks):
    """
    Backtest STANDALONE: nao precisa de bot. Os filtros (WR H2H, placar, tempo,
    black/white list de nicks, mercado/lado) vem da aba; os ticks vem do arquivo
    (upload_id). Monta um bot_snapshot virtual e roda o MESMO worker.

    BLINDADO ponta a ponta:
      - valida TODOS os filtros (lado/mercado/cenario/quartos/nicks/linha) -> 400 claro
      - valida que o upload existe antes de criar o job (nao cria job fadado a falhar)
      - trata coluna upload_id ausente e bot_id NOT NULL (avisa a migration)
      - se o worker estourar no background, marca o job como 'erro' (sem zumbi)
    """
    # 1) valida e normaliza os filtros (400 claro se algo invalido)
    norm = _validar_e_normalizar(req)

    # 2) upload existe e e legivel? (falha cedo, antes de criar job)
    try:
        from workers.backtest_upload import caminho_do_upload
        caminho_do_upload(req.upload_id)
    except BacktestUploadError as e:
        raise HTTPException(status_code=400, detail=f"upload_id invalido: {e}")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[backtest_upload] erro ao validar upload_id (avulso)")
        raise HTTPException(status_code=400, detail=f"upload_id invalido: {e}")

    # 3) monta o snapshot (so com valores ja validados)
    try:
        snapshot = _montar_snapshot_avulso(req, norm)
        snapshot_json = json.dumps(snapshot, default=str)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[backtest_upload] erro montando snapshot avulso")
        raise HTTPException(status_code=400, detail=f"Filtros invalidos: {e}")

    # 4) cria o job
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            try:
                job_id = await conn.fetchval(
                    """
                    INSERT INTO backtest_jobs
                        (bot_id, data_inicio, data_fim, stake_modo, stake_valor,
                         banca_inicial, bot_snapshot, status, progresso, upload_id)
                    VALUES (NULL, NULL, NULL, $1, $2, $3, $4::jsonb, 'pendente', 0, $5)
                    RETURNING id
                    """,
                    req.stake_modo, req.stake_valor, req.banca_inicial,
                    snapshot_json, req.upload_id,
                )
            except Exception as e:
                msg = str(e).lower()
                if "upload_id" in msg and "column" in msg:
                    raise HTTPException(
                        status_code=500,
                        detail="Coluna upload_id ausente. Rode: "
                               "ALTER TABLE backtest_jobs ADD COLUMN IF NOT EXISTS upload_id TEXT;",
                    )
                if "bot_id" in msg and ("null" in msg or "not-null" in msg):
                    raise HTTPException(
                        status_code=500,
                        detail="Coluna bot_id precisa aceitar NULL p/ backtest avulso. Rode: "
                               "ALTER TABLE backtest_jobs ALTER COLUMN bot_id DROP NOT NULL;",
                    )
                logger.exception("[backtest_upload] erro ao inserir job avulso")
                raise HTTPException(status_code=500, detail=f"Erro ao criar job: {e}")

            if job_id is None:
                raise HTTPException(status_code=500, detail="Job nao foi criado (id nulo).")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[backtest_upload] erro de DB no job avulso")
        raise HTTPException(status_code=500, detail=f"Erro de banco: {e}")

    # 5) dispara o worker (com wrapper que marca erro se estourar)
    logger.info(f"[backtest_upload] Job avulso {job_id} criado (mercado={norm['mercado']}, lado={norm['lado']})")
    background.add_task(_rodar_backtest_seguro, job_id)
    return {"job_id": job_id, "status": "pendente", "fonte": "arquivo", "avulso": True}
