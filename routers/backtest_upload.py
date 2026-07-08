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

import io
import json
import logging
import re
from datetime import datetime
from typing import Optional

from fastapi import (APIRouter, BackgroundTasks, File, HTTPException,
                     UploadFile)
from fastapi.responses import StreamingResponse
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
# janela do WR: "all" ou "last_<N>" (qtd) / "last_<N>h|d|m" (tempo)
_WR_JANELA_RE = re.compile(r'^(all|last_\d+[hdm]?)$')
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
    # janela do WR: "all" ou "last_N"/"last_Nh"/"last_Nd" (quantidade OU tempo)
    wr_janela: str = Field(default="all", max_length=20)
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
    # filtros complementares (H2H): media, gap_media, zscore, gap_linha, tendencia
    # cada item: {tipo, janela, min, minAtivo, max, maxAtivo}
    filtros_comp: list = Field(default_factory=list)
    # filtros de historico (WR): VARIOS filtros de win-rate por janela (escadinha)
    # cada item: {janela, prob:[min%,max%], minPartidas}. Formato do bot ao vivo.
    filtros_hist: list = Field(default_factory=list)
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


# tipos de filtro complementar aceitos (mesmos do bot ao vivo / CriarBot)
_COMP_TIPOS_VALIDOS = {"media", "gap_media", "gap", "gap_linha", "tendencia", "zscore", "z"}
_MAX_FILTROS_COMP = 20


def _limpar_filtros_comp(lista) -> list:
    """Sanitiza os filtros complementares vindos da aba avulsa. Mantem so tipos
    conhecidos, valida thresholds ativos (numero), e devolve no formato que o
    worker entende (mesmo de filtrosCompAdicionados). Falha EXPLICITA (400) se um
    threshold ativo nao for numero - melhor erro claro que backtest zerado."""
    if not lista:
        return []
    if not isinstance(lista, (list, tuple)):
        raise HTTPException(status_code=400, detail="filtros_comp deve ser uma lista")
    out = []
    for f in lista:
        if not isinstance(f, dict):
            continue
        tipo = str(f.get("tipo", "")).strip().lower()
        if tipo not in _COMP_TIPOS_VALIDOS:
            continue  # ignora tipo desconhecido (nao quebra)
        min_ativo = bool(f.get("minAtivo"))
        max_ativo = bool(f.get("maxAtivo"))
        if not min_ativo and not max_ativo:
            continue  # sem limite ativo = no-op, nao adiciona
        entry = {"tipo": tipo, "janela": f.get("janela"),
                 "minAtivo": min_ativo, "maxAtivo": max_ativo}
        if min_ativo:
            try:
                entry["min"] = float(f.get("min"))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400,
                                    detail=f"filtro complementar '{tipo}': min invalido ({f.get('min')!r}).")
        if max_ativo:
            try:
                entry["max"] = float(f.get("max"))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400,
                                    detail=f"filtro complementar '{tipo}': max invalido ({f.get('max')!r}).")
        out.append(entry)
        if len(out) >= _MAX_FILTROS_COMP:
            break
    return out


def _limpar_filtros_hist(lista) -> list:
    """Sanitiza os filtros de historico (WR) da aba avulsa. Cada item vira um
    filtrosHistAdicionados no formato do worker (base=match, tipo=all, versao=all).
    Valida janela (mesmo formato do WR) e prob [min%,max%]. Falha 400 se invalido -
    erro claro em vez de backtest zerado."""
    if not lista:
        return []
    if not isinstance(lista, (list, tuple)):
        raise HTTPException(status_code=400, detail="filtros_hist deve ser uma lista")
    out = []
    for f in lista:
        if not isinstance(f, dict):
            continue
        janela = str(f.get("janela", "all")).strip().lower()
        if not _WR_JANELA_RE.match(janela):
            raise HTTPException(status_code=400,
                                detail=f"janela do filtro de WR invalida: '{janela}'. Use 'all' ou 'last_N' / 'last_Nh' / 'last_Nd'.")
        prob = f.get("prob") or []
        try:
            pmin = float(prob[0])
            pmax = float(prob[1]) if (len(prob) > 1 and prob[1] is not None) else 100.0
        except (TypeError, ValueError, IndexError):
            raise HTTPException(status_code=400,
                                detail=f"WR do filtro invalido: {f.get('prob')!r}. Informe a % minima.")
        if not (0 <= pmin <= 100 and 0 <= pmax <= 100) or pmin > pmax:
            raise HTTPException(status_code=400,
                                detail=f"WR fora de 0-100 (ou min>max): {[pmin, pmax]}.")
        try:
            minp = max(0, int(f.get("minPartidas", 10)))
        except (TypeError, ValueError):
            minp = 10
        out.append({
            "base": "match", "tipo": "all", "versao": "all",
            "janela": janela, "prob": [pmin, pmax], "minPartidas": minp,
        })
        if len(out) >= 20:
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
    # janela do WR: "all" ou "last_N"/"last_Nh"/"last_Nd" (so valida se WR ativo)
    if req.wr_min is not None:
        wj = (req.wr_janela or "all").strip().lower()
        if not _WR_JANELA_RE.match(wj):
            raise HTTPException(status_code=400,
                                detail=f"janela do WR invalida: '{wj}'. Use 'all' ou 'last_N' / 'last_Nh' / 'last_Nd'.")
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
    # filtros complementares (H2H)
    filtros_comp = _limpar_filtros_comp(req.filtros_comp)
    # filtros de historico (WR): varios por janela
    filtros_hist = _limpar_filtros_hist(req.filtros_hist)

    return {
        "lado": lado, "mercado": mercado, "cenario": cenario,
        "quartos": quartos, "blacklist": blacklist, "whitelist": whitelist,
        "filtros_comp": filtros_comp, "filtros_hist": filtros_hist,
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
    # Prioridade: array filtros_hist (varios filtros, escadinha). Se vazio, cai no
    # filtro unico antigo (wr_min) por compatibilidade.
    if norm.get("filtros_hist"):
        filtros["filtrosHistAdicionados"] = norm["filtros_hist"]
    elif req.wr_min is not None:
        # janela ja vem no formato do worker ("all"/"last_N"/"last_1d"/"last_7d").
        # validada em _validar_e_normalizar; aqui so passa (default "all").
        janela_str = (req.wr_janela or "all").strip().lower()
        filtros["filtrosHistAdicionados"] = [{
            "base": "match",
            "prob": [float(req.wr_min), 100],
            "tipo": "all",
            "janela": janela_str,
            "versao": "all",
            "minPartidas": int(req.wr_min_partidas),
        }]

    # filtros complementares (H2H) -> filtrosCompAdicionados (o worker le direto)
    if norm.get("filtros_comp"):
        filtros["filtrosCompAdicionados"] = norm["filtros_comp"]

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


@router.get("/jobs/{job_id}/planilha")
async def baixar_planilha_apostas(job_id: int):
    """Gera um .xlsx com as apostas do backtest no MESMO formato do export do bot
    ao vivo (aba 'Tips Enviadas'). Permite comparar backtest vs vivo lado a lado.
    Le apostas_detalhe (ja salvo no job) e monta a planilha on-demand."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, apostas_detalhe FROM backtest_jobs WHERE id=$1", job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="job nao encontrado")
    detalhe = row["apostas_detalhe"]
    if isinstance(detalhe, str):
        detalhe = json.loads(detalhe or "[]")
    detalhe = detalhe or []
    if not detalhe:
        raise HTTPException(status_code=400,
                            detail="job sem apostas para exportar (0 apostas ou ainda rodando).")

    def _fmt_dt(ts_iso):
        try:
            d = datetime.fromisoformat(str(ts_iso).replace("Z", ""))
            return d.strftime("%d/%m/%Y"), d.strftime("%H:%M:%S")
        except Exception:
            return str(ts_iso), ""

    _res_map = {"green": "Green", "red": "Red", "void": "Void"}
    linhas = []
    for a in detalhe:
        data, hora = _fmt_dt(a.get("ts"))
        linhas.append({
            "Torneio": a.get("torneio", ""),
            "Campeonato": a.get("liga", ""),
            "Confronto": f"{a.get('jogador_a', '')} x {a.get('jogador_b', '')}",
            "Jogador A": a.get("jogador_a", ""),
            "Time A": a.get("time_a", ""),
            "Jogador B": a.get("jogador_b", ""),
            "Time B": a.get("time_b", ""),
            "Data": data,
            "Hora": hora,
            "Mercado": a.get("mercado", ""),
            "Tip": a.get("tip", ""),
            "Linha": a.get("linha"),
            "Janela 1": a.get("janela_1", ""),
            "Winrate 1": a.get("winrate_1"),
            "Janela 2": a.get("janela_2", ""),
            "Winrate 2": a.get("winrate_2"),
            "Odd": a.get("odd"),
            "Placar Envio": a.get("placar_envio", ""),
            "Placar Final": a.get("score_final", ""),
            "Resultado": _res_map.get(a.get("resultado"), a.get("resultado", "")),
            "Lucro/Prej.": a.get("lucro_unidades"),
        })

    import pandas as pd
    df = pd.DataFrame(linhas)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xls:
        df.to_excel(xls, index=False, sheet_name="Tips Enviadas")
    buf.seek(0)
    fn = f"backtest_job_{job_id}_apostas.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )
