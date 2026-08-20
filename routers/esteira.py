# -*- coding: utf-8 -*-
r"""
routers/esteira.py — a esteira como job do painel (passo 4).

A API aqui e' SO DESPACHANTE: valida, grava a rodada 'pendente' e responde.
Quem roda e' o servico `workers/esteira_daemon.py` (fila propria, 2 slots,
piso de RAM e teto global de pesados). Nenhum endpoint segura o event loop.

ATENCAO AO NOME DO ARQUIVO: tem que ser routers/esteira.py — o main.py
importa pelo nome (a licao do varredura_router.py: import quebrado derruba a
API em loop com o nssm mostrando SERVICE_RUNNING e nada escutando na 8000).

Registrar no main.py (2 mudancas):
    1) na linha "from routers import ...", acrescentar ", esteira" no fim
    2) junto dos outros include_router:
       app.include_router(esteira.router, dependencies=PROTEGIDO)

O que o POST /esteira/rodadas aceita (espelha o contrato do worker):
    itens[]        — lista de estrategias no formato da planilha (nome,
                     mercado, linha_min, chip_*, variar, ...). O worker monta
                     snapshot, injeta a SENTINELA e gera as variacoes.
    (ou planilha)  — caminho de um xlsx JA NO SERVIDOR (fluxo avancado/CLI)
    fonte, UMA de tres formas:
      upload_id                      — parquet ja subido pelo backtest avulso
      fonte_arquivo (+ dias)        — arquivo no servidor, recorte com cache
      fonte='banco' + casa + data_inicio + data_fim
    opcionais: casa_padrao, esporte_padrao, sem_sentinela, sentinela_ap_min,
               max_zerados (0 desliga a trava), timeout_min, stake, banca
"""
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from database import get_pool
from security import get_current_user, acesso_total

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/esteira", tags=["esteira"])

RAIZ = Path(__file__).resolve().parent.parent
ORIGENS = ("planilha", "varredura", "manual")
FINAIS = ("concluido", "erro", "cancelado")


# =============================================================== modelos ====
class CriarEsteiraRequest(BaseModel):
    nome: Optional[str] = Field(default=None, max_length=120)
    origem: str = Field(default="manual",
                        description="planilha | varredura | manual")
    origem_ref: Optional[str] = Field(
        default=None, max_length=200,
        description="ex.: id do garimpo (origem=varredura) ou nome do xlsx")
    # as estrategias — pelo painel vem 'itens'; 'planilha' e' o fluxo avancado
    itens: Optional[List[Dict[str, Any]]] = None
    planilha: Optional[str] = Field(default=None, max_length=400)
    # fonte dos ticks (UMA das tres formas)
    upload_id: Optional[str] = Field(default=None, max_length=400)
    fonte_arquivo: Optional[str] = Field(default=None, max_length=400)
    dias: Optional[int] = Field(default=None, ge=1, le=365)
    fonte: Optional[str] = Field(default=None,
                                 description="'banco' liga a fonte banco")
    casa: Optional[str] = Field(default=None, max_length=60)
    data_inicio: Optional[str] = Field(default=None,
                                       description="AAAA-MM-DD (fonte banco)")
    data_fim: Optional[str] = Field(default=None,
                                    description="AAAA-MM-DD (fonte banco)")
    # defaults dos snapshots + opcoes do worker
    casa_padrao: Optional[str] = Field(default=None, max_length=60)
    esporte_padrao: Optional[str] = Field(default=None, max_length=60)
    sem_sentinela: bool = Field(default=False)
    sentinela_ap_min: Optional[int] = Field(default=None, ge=1)
    max_zerados: Optional[int] = Field(
        default=None, ge=0, description="0 desliga a trava de zerados")
    timeout_min: Optional[float] = Field(default=None, ge=1, le=600)
    stake: Optional[float] = Field(default=None, gt=0)
    banca: Optional[float] = Field(default=None, gt=0)


# ============================================================== auxiliares ==
def _pode_ver(usuario, dono_id):
    return acesso_total(usuario) or dono_id == usuario.get("id")


async def _buscar(conn, job_id, usuario):
    row = await conn.fetchrow("SELECT * FROM esteira_jobs WHERE id = $1",
                              job_id)
    if row is None or not _pode_ver(usuario, row["user_id"]):
        raise HTTPException(status_code=404, detail="rodada nao encontrada")
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


def _xlsx_da_rodada(job_id: int) -> Path:
    return RAIZ / "esteiras" / f"esteira_{job_id}.xlsx"


def _linha(row, completo=False):
    d = {
        "id": row["id"], "nome": row["nome"], "origem": row["origem"],
        "origem_ref": row["origem_ref"], "status": row["status"],
        "suspeita": row["suspeita"], "sentinela_ok": row["sentinela_ok"],
        "total_itens": row["total_itens"],
        "itens_prontos": row["itens_prontos"],
        "progresso_msg": row["progresso_msg"], "erro": row["erro"],
        "criado_em": row["criado_em"], "iniciado_em": row["iniciado_em"],
        "finalizado_em": row["finalizado_em"],
        "tem_planilha": _xlsx_da_rodada(row["id"]).is_file(),
    }
    # contagens REAIS da view (quando a linha veio dela)
    for k in ("itens_total_real", "itens_concluidos_real", "itens_erro",
              "itens_pulados"):
        if k in dict(row):
            d[k] = row[k]
    if completo:
        d["params"] = _json(row["params"], {})
        d["baseline"] = _json(row["baseline"])
        d["alertas"] = _json(row["alertas"])
        d["suspeita_motivo"] = row["suspeita_motivo"]
        d["h2h_ts_inicio"] = row["h2h_ts_inicio"]
        d["h2h_ts_fim"] = row["h2h_ts_fim"]
    return d


# ================================================================ endpoints ==
@router.get("/arquivos")
async def listar_arquivos(usuario: dict = Depends(get_current_user)):
    """Planilhas (.xlsx) e parquets da RAIZ do tipmike_api, pros dropdowns da
    tela — em vez de digitar caminho. So le nomes; nada do cliente vira path."""
    def _lista(padrao, ignora=()):  # nome, tamanho MB, mtime — mais novo 1o
        itens = []
        try:
            for p in RAIZ.glob(padrao):
                if not p.is_file() or p.name.startswith("~$"):
                    continue
                if any(p.name.startswith(x) for x in ignora):
                    continue
                st = p.stat()
                itens.append({"nome": p.name,
                              "mb": round(st.st_size / 1048576, 1),
                              "mtime": st.st_mtime})
        except Exception:
            logger.exception("[esteira] falha listando arquivos da raiz")
        itens.sort(key=lambda x: -x["mtime"])
        return itens[:100]
    return {"planilhas": _lista("*.xlsx", ignora=("placar_", "esteira_")),
            "parquets": _lista("*.parquet")}



# ===================================================== tela de escolha (5b) ==
def _mods_selecao():
    """Import tardio: se os modulos nao estiverem em workers/, o endpoint
    responde com o motivo em portugues — a API nunca cai por import no topo
    (a licao do incidente do JSX salvo por cima do router)."""
    try:
        from workers import esteira_conversor as C, esteira_selecao as S
        return C, S
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail="esteira_conversor.py / esteira_selecao.py precisam estar "
                   f"em workers\\ — mova os arquivos e tente de novo ({e})")


def _baseline_do_contrato(contrato):
    """O contrato guarda o baseline como texto ('-4,88% (2.407 ap)') ou
    numero. Parse defensivo: se nao achar, devolve None e o alerta de
    magnitude simplesmente nao sai."""
    if not contrato:
        return None
    v = contrato.get("baseline") if isinstance(contrato, dict) else None
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if abs(float(v)) <= 100 else None
    import re as _re
    s = str(v)
    # o contrato guarda a LINHA INTEIRA do plano ("-452,09u ... -4,52%"):
    # o numero que interessa e' o ancorado no %, nunca o primeiro que aparece
    m = _re.search(r"(-?\d+(?:[.,]\d+)?)\s*%", s)
    if m:
        return float(m.group(1).replace(",", "."))
    # sem % na string: o primeiro numero PLAUSIVEL pra ROI de mercado
    for m in _re.finditer(r"-?\d+(?:[.,]\d+)?", s):
        x = float(m.group(0).replace(",", "."))
        if abs(x) <= 100:
            return x
    return None


def _montar_selecao(caminho_tudo, caminho_holdout, baseline, criterio, top):
    """Roda em thread (pandas em 17k linhas travaria o event loop).
    Replica o caminho do testar_selecao: rename -> coercao -> merge do
    holdout pela chave normalizada -> conversor -> pacote colunar."""
    import pandas as pd
    C, S = _mods_selecao()

    m = pd.read_csv(caminho_tudo, low_memory=False)
    ren = {"apostas": "ap", "unidades": "u", "lucro_dd": "ldd",
           "max_reds": "seq_neg", "roi_m1": "m1", "roi_m2": "m2",
           "conc_alvo": "conc3", "n_par": "n_alvos"}
    m = m.rename(columns={k: v for k, v in ren.items() if k in m.columns})
    for c in ("ROI", "ap", "WR", "u", "ldd", "conc3", "DD", "u_dia", "ap_dia",
              "m1", "m2", "pior_dia"):
        if c in m.columns:
            m[c] = pd.to_numeric(m[c], errors="coerce")

    cruzadas = 0
    if caminho_holdout and os.path.isfile(caminho_holdout):
        h = pd.read_csv(caminho_holdout, low_memory=False)
        K = ["janela", "wr_min", "wr_max", "janela2", "op2", "wr2",
             "conf_min", "conf_max", "linha_min", "linha_max",
             "odd_min", "odd_max", "extra", "teto"]

        def nrm(v):
            s = str(v).strip()
            if s in ("-", "nan", "None", ""):
                return "-"
            try:
                return f"{float(s):.4f}"
            except ValueError:
                return s

        def key(d):
            return d[K].astype(str).apply(lambda c: c.map(nrm)).agg("|".join, axis=1)

        m["_k"] = key(m)
        h["_k"] = key(h)
        h = h.rename(columns={"ROI": "ROI_ho", "apostas": "ap_ho"})
        m = m.drop_duplicates("_k").merge(
            h[["_k", "ROI_ho", "ap_ho"]].drop_duplicates("_k"),
            on="_k", how="left")
        m = m.drop(columns=["_k"])
        cruzadas = int(m["ROI_ho"].notna().sum())

    # o drop_duplicates do merge fura o indice — sem isto, o indice do df
    # deixa de bater com a POSICAO na lista e a marcacao da tela erraria
    m = m.reset_index(drop=True)

    # o top inicial se decide com o df ainda NUMERICO (nlargest exige)
    crit = criterio if criterio in m.columns else "ROI"
    idx_top = [int(x) for x in m.nlargest(top, crit).index] if crit in m.columns else []

    # NaN das celulas vazias nao e' JSON — vira None aqui, uma vez so
    m = m.astype(object).where(m.notna(), None)
    registros = m.to_dict("records")

    # quais linhas o MOTOR nao reproduz — com o motivo, nunca silencio
    itens, recusadas = C.converter_lote(registros)
    irrep = {r["i"]: r["motivo"] for r in recusadas}

    # o pacote da tela (colunar, formato do prototipo) + os itens da planilha
    # colunares e ALINHADOS POR INDICE com o pack (recusada = linha nula)
    pack = S.empacotar(registros, baseline_treino=baseline)
    COLS_ITEM = ["nome", "grupo", "mercado", "casa", "esporte", "chip_janela",
                 "chip_wr_min", "chip_wr_max", "chip_conf", "chip_conf_max",
                 "chip2_janela", "chip2_wr_min", "chip2_wr_max",
                 "linha_min", "linha_max", "odd_min", "odd_max",
                 "folga_min", "folga_max", "tot_env_min", "tot_env_max",
                 "atropelo_min", "atropelo_max", "teto",
                 "evitar_linhas_seq", "variar"]
    it_rows, pos = [], 0
    for i in range(len(registros)):
        if i in irrep:
            it_rows.append(None)
        else:
            d = itens[pos]
            pos += 1
            it_rows.append([d.get(c) for c in COLS_ITEM])

    # a fotografia inicial: os alertas do top-N pelo criterio pedido
    escolhidas = [registros[i] for i in idx_top]
    checks, veredito, resumo = S.alertas(
        escolhidas, todas=registros, baseline_treino=baseline)

    return {
        "total": len(registros),
        "holdout_cruzado": cruzadas,
        "baseline": baseline,
        "pack": pack,
        "itens_pack": {"cols": COLS_ITEM, "rows": it_rows},
        "irreproduziveis": {str(k): v for k, v in irrep.items()},
        "alertas": {"criterio": crit, "top": top, "indices": idx_top,
                    "checks": checks, "veredito": veredito, "resumo": resumo},
    }


@router.get("/varreduras/{vid}/selecao")
async def selecao_da_varredura(
        vid: int,
        criterio: str = Query("ldd", max_length=30),
        top: int = Query(30, ge=1, le=300),
        baseline: Optional[float] = Query(None,
            description="ROI do mercado no treino; vazio = do contrato"),
        usuario: dict = Depends(get_current_user)):
    """A tela de escolha: converte o garimpo, marca o que o motor nao
    reproduz e devolve o pacote colunar + alertas do top inicial. Recebe o
    ID da varredura — nunca caminho de arquivo."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM varredura_jobs WHERE id = $1", vid)
        if row is None or not _pode_ver(usuario, row["user_id"]):
            raise HTTPException(status_code=404,
                                detail="varredura nao encontrada")
        if row["status"] != "concluido":
            raise HTTPException(
                status_code=400,
                detail=f"a varredura {vid} esta '{row['status']}' — so da "
                       "pra escolher em cima de garimpo concluido")
    caminho = row["arquivo_tudo"]
    if not caminho or not os.path.isfile(caminho):
        raise HTTPException(
            status_code=404,
            detail=f"a varredura {vid} nao tem o arquivo completo no disco "
                   "(arquivo_tudo) — rode o garimpo de novo")
    if baseline is None:
        baseline = _baseline_do_contrato(_json(row["contrato"], {}))

    import asyncio
    try:
        corpo = await asyncio.to_thread(
            _montar_selecao, caminho, row["arquivo_holdout"], baseline,
            criterio, top)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[esteira] selecao da varredura {vid} falhou")
        raise HTTPException(status_code=500,
                            detail=f"falha lendo o garimpo: {e}")
    corpo["varredura"] = {"id": row["id"], "nome": row["nome"]}
    logger.info(f"[esteira] selecao da varredura {vid}: {corpo['total']} "
                f"configs, {len(corpo['irreproduziveis'])} irreproduziveis")
    return corpo


class AlertasSelecaoRequest(BaseModel):
    indices: List[int] = Field(..., min_length=1, max_length=300,
                               description="indices das linhas marcadas")
    baseline: Optional[float] = None


@router.post("/varreduras/{vid}/selecao/alertas")
async def alertas_da_selecao(vid: int, req: AlertasSelecaoRequest,
                             usuario: dict = Depends(get_current_user)):
    """Recalcula os 4 alertas para a marcacao ATUAL da tela — mesma conta do
    GET, so muda quem sao as escolhidas."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM varredura_jobs WHERE id = $1", vid)
        if row is None or not _pode_ver(usuario, row["user_id"]):
            raise HTTPException(status_code=404,
                                detail="varredura nao encontrada")
    caminho = row["arquivo_tudo"]
    if not caminho or not os.path.isfile(caminho):
        raise HTTPException(status_code=404,
                            detail="arquivo do garimpo sumiu do disco")
    baseline = req.baseline
    if baseline is None:
        baseline = _baseline_do_contrato(_json(row["contrato"], {}))

    def _rodar():
        import pandas as pd
        C, S = _mods_selecao()
        m = pd.read_csv(caminho, low_memory=False)
        ren = {"apostas": "ap", "unidades": "u", "lucro_dd": "ldd",
               "max_reds": "seq_neg", "roi_m1": "m1", "roi_m2": "m2",
               "conc_alvo": "conc3", "n_par": "n_alvos"}
        m = m.rename(columns={k: v for k, v in ren.items() if k in m.columns})
        for c in ("ROI", "conc3"):
            if c in m.columns:
                m[c] = pd.to_numeric(m[c], errors="coerce")
        if row["arquivo_holdout"] and os.path.isfile(row["arquivo_holdout"]):
            h = pd.read_csv(row["arquivo_holdout"], low_memory=False)
            K = ["janela", "wr_min", "wr_max", "janela2", "op2", "wr2",
                 "conf_min", "conf_max", "linha_min", "linha_max",
                 "odd_min", "odd_max", "extra", "teto"]

            def nrm(v):
                s = str(v).strip()
                if s in ("-", "nan", "None", ""):
                    return "-"
                try:
                    return f"{float(s):.4f}"
                except ValueError:
                    return s

            def key(d):
                return d[K].astype(str).apply(
                    lambda c: c.map(nrm)).agg("|".join, axis=1)

            m["_k"] = key(m)
            h["_k"] = key(h)
            h = h.rename(columns={"ROI": "ROI_ho", "apostas": "ap_ho"})
            m = m.drop_duplicates("_k").merge(
                h[["_k", "ROI_ho", "ap_ho"]].drop_duplicates("_k"),
                on="_k", how="left")
        m = m.reset_index(drop=True)
        m = m.astype(object).where(m.notna(), None)
        regs = m.to_dict("records")
        escolhidas = [regs[i] for i in req.indices if 0 <= i < len(regs)]
        return S.alertas(escolhidas, todas=regs, baseline_treino=baseline)

    import asyncio
    try:
        checks, veredito, resumo = await asyncio.to_thread(_rodar)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[esteira] alertas da selecao {vid} falharam")
        raise HTTPException(status_code=500, detail=f"falha nos alertas: {e}")
    return {"checks": checks, "veredito": veredito, "resumo": resumo,
            "n": len(req.indices), "baseline": baseline}


@router.post("/rodadas")
async def criar_rodada(req: CriarEsteiraRequest,
                       usuario: dict = Depends(get_current_user)):
    """Cria a rodada 'pendente' e devolve na hora. Quem roda e' o daemon —
    aqui nao sobe processo nenhum."""
    if req.origem not in ORIGENS:
        raise HTTPException(status_code=400,
                            detail=f"origem invalida; use uma de {ORIGENS}")

    # estrategias: itens[] OU planilha no servidor
    if req.itens is not None:
        if not isinstance(req.itens, list) or len(req.itens) == 0:
            raise HTTPException(status_code=400,
                                detail="itens vazio — mande ao menos 1 "
                                       "estrategia")
        for i, e in enumerate(req.itens):
            if not isinstance(e, dict) or not str(e.get("nome") or "").strip():
                raise HTTPException(
                    status_code=400,
                    detail=f"item {i + 1} sem 'nome' — cada estrategia "
                           "precisa de nome e campos no formato da planilha")
        if len(req.itens) > 300:
            raise HTTPException(status_code=400,
                                detail="mais de 300 itens numa rodada — "
                                       "quebre em rodadas menores")
    elif not req.planilha:
        raise HTTPException(status_code=400,
                            detail="mande 'itens' (lista de estrategias) ou "
                                   "'planilha' (xlsx no servidor)")

    # fonte dos ticks: exatamente o contrato do worker
    tem_banco = (str(req.fonte or "").strip().lower() == "banco")
    formas = sum([bool(req.upload_id), bool(req.fonte_arquivo), tem_banco])
    if formas == 0:
        raise HTTPException(
            status_code=400,
            detail="sem fonte de ticks — mande upload_id, ou fonte_arquivo "
                   "(+dias), ou fonte='banco' com casa/data_inicio/data_fim")
    if formas > 1:
        raise HTTPException(status_code=400,
                            detail="mais de uma fonte de ticks — escolha uma")
    if tem_banco and not (req.casa and req.data_inicio and req.data_fim):
        raise HTTPException(
            status_code=400,
            detail="fonte='banco' exige casa, data_inicio e data_fim "
                   "(AAAA-MM-DD)")

    params = {k: v for k, v in req.model_dump().items()
              if k not in ("nome", "origem", "origem_ref")
              and v is not None and v is not False}

    pool = get_pool()
    async with pool.acquire() as conn:
        job_id = await conn.fetchval(
            """INSERT INTO esteira_jobs
                   (user_id, nome, origem, origem_ref, params, status)
               VALUES ($1, $2, $3, $4, $5::jsonb, 'pendente')
            RETURNING id""",
            usuario.get("id"),
            req.nome or f"rodada {req.origem}",
            req.origem, req.origem_ref, json.dumps(params))
        na_frente = await conn.fetchval(
            """SELECT COUNT(*) FROM esteira_jobs
                WHERE status = 'pendente' AND id < $1""", job_id)

    logger.info(f"[esteira] rodada {job_id} na fila "
                f"({len(req.itens) if req.itens else 'planilha'} itens)")
    return {"id": job_id, "status": "pendente", "na_frente": na_frente}


@router.get("/rodadas")
async def listar(limite: int = Query(30, ge=1, le=200),
                 usuario: dict = Depends(get_current_user)):
    pool = get_pool()
    async with pool.acquire() as conn:
        if acesso_total(usuario):
            rows = await conn.fetch(
                """SELECT v.*, j.user_id
                     FROM v_esteira_jobs v
                     JOIN esteira_jobs j ON j.id = v.id
                    ORDER BY v.id DESC LIMIT $1""", limite)
        else:
            rows = await conn.fetch(
                """SELECT v.*, j.user_id
                     FROM v_esteira_jobs v
                     JOIN esteira_jobs j ON j.id = v.id
                    WHERE j.user_id = $2
                    ORDER BY v.id DESC LIMIT $1""",
                limite, usuario.get("id"))
    return [_linha(r) for r in rows]


@router.get("/rodadas/{job_id}")
async def detalhe(job_id: int, usuario: dict = Depends(get_current_user)):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT v.*, j.user_id, j.params, j.baseline, j.alertas,
                      j.suspeita_motivo
                 FROM v_esteira_jobs v
                 JOIN esteira_jobs j ON j.id = v.id
                WHERE v.id = $1""", job_id)
        if row is None or not _pode_ver(usuario, row["user_id"]):
            raise HTTPException(status_code=404,
                                detail="rodada nao encontrada")
    return _linha(row, completo=True)


@router.get("/rodadas/{job_id}/itens")
async def itens(job_id: int, usuario: dict = Depends(get_current_user)):
    """O placar: cada item com snapshot resumido, metricas e alertas.
    O backtest_job_id e' clicavel no painel (abre o job no backtest)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await _buscar(conn, job_id, usuario)
        rows = await conn.fetch(
            """SELECT id, ordem, nome, papel, pai_item_id, assinatura,
                      status, backtest_job_id, metricas, alertas, erro,
                      iniciado_em, finalizado_em
                 FROM esteira_itens
                WHERE esteira_job_id = $1
                ORDER BY ordem, id""", job_id)
    return [{
        "id": r["id"], "ordem": r["ordem"], "nome": r["nome"],
        "papel": r["papel"], "pai_item_id": r["pai_item_id"],
        "assinatura": r["assinatura"], "status": r["status"],
        "backtest_job_id": r["backtest_job_id"],
        "metricas": _json(r["metricas"]), "alertas": _json(r["alertas"]),
        "erro": r["erro"], "iniciado_em": r["iniciado_em"],
        "finalizado_em": r["finalizado_em"],
    } for r in rows]


@router.post("/rodadas/{job_id}/cancelar")
async def cancelar(job_id: int, usuario: dict = Depends(get_current_user)):
    """Marca como cancelada; o daemon mata o processo no proximo ciclo (o
    worker tambem checa entre um item e outro)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await _buscar(conn, job_id, usuario)
        if row["status"] in FINAIS:
            raise HTTPException(status_code=400,
                                detail=f"a rodada ja esta '{row['status']}'")
        await conn.execute(
            """UPDATE esteira_jobs
                  SET status = 'cancelado', finalizado_em = NOW(),
                      progresso_msg = 'cancelada'
                WHERE id = $1""", job_id)
    return {"id": job_id, "status": "cancelado"}


@router.post("/rodadas/{job_id}/retomar")
async def retomar(job_id: int, usuario: dict = Depends(get_current_user)):
    """O destravamento que era UPDATE no psql vira botao. Vale pra rodada em
    'erro'/'cancelado' (ou 'concluido' com itens pra re-tentar): itens em
    erro voltam pra fila, a rodada volta pra 'pendente' e o daemon pega. A
    retomada do worker PULA o que ja concluiu — re-subir e' barato."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await _buscar(conn, job_id, usuario)
        if row["status"] not in FINAIS:
            raise HTTPException(
                status_code=400,
                detail=f"a rodada esta '{row['status']}' — ja esta na fila "
                       "ou rodando")
        res = await conn.execute(
            """UPDATE esteira_itens SET status = 'pendente', erro = NULL
                WHERE esteira_job_id = $1 AND status IN ('erro', 'rodando')""",
            job_id)
        try:
            reativados = int(str(res).split()[-1])
        except Exception:
            reativados = 0
        restam = await conn.fetchval(
            """SELECT COUNT(*) FROM esteira_itens
                WHERE esteira_job_id = $1 AND status = 'pendente'""", job_id)
        if row["status"] == "concluido" and restam == 0:
            raise HTTPException(
                status_code=400,
                detail="nada a retomar — todos os itens estao concluidos")
        await conn.execute(
            """UPDATE esteira_jobs
                  SET status = 'pendente', erro = NULL, pid = NULL,
                      progresso_msg = 'retomada pedida; aguardando slot'
                WHERE id = $1""", job_id)
    logger.info(f"[esteira] rodada {job_id} retomada "
                f"({reativados} item(ns) reativados, {restam} na fila)")
    return {"id": job_id, "status": "pendente",
            "itens_reativados": reativados, "itens_na_fila": restam}


@router.get("/rodadas/{job_id}/planilha")
async def baixar_planilha(job_id: int,
                          usuario: dict = Depends(get_current_user)):
    """O esteiras/esteira_<id>.xlsx (PLACAR/VARIACOES/BASELINE/CARTEIRA).
    So existe depois do fecho da rodada."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await _buscar(conn, job_id, usuario)
    caminho = _xlsx_da_rodada(job_id)
    if not caminho.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"a rodada {job_id} ainda nao tem planilha "
                   f"(status: {row['status']})")
    return FileResponse(str(caminho), filename=caminho.name)


@router.delete("/rodadas/{job_id}")
async def excluir(job_id: int, usuario: dict = Depends(get_current_user)):
    """Apaga a rodada e os itens (CASCADE). So em status final — rodada viva
    se cancela antes. Os backtest_jobs ficam (a FK e' SET NULL); o xlsx e o
    log da rodada sao apagados junto, sem drama se nao existirem."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await _buscar(conn, job_id, usuario)
        if row["status"] not in FINAIS:
            raise HTTPException(
                status_code=400,
                detail=f"a rodada esta '{row['status']}' — cancele antes de "
                       "excluir")
        await conn.execute("DELETE FROM esteira_jobs WHERE id = $1", job_id)
    for arq in (_xlsx_da_rodada(job_id),
                RAIZ / "esteiras" / f"esteira_{job_id}.log"):
        try:
            if arq.is_file():
                arq.unlink()
        except Exception:
            logger.warning(f"[esteira] nao apaguei {arq} (segue no disco)")
    logger.info(f"[esteira] rodada {job_id} excluida")
    return {"id": job_id, "excluida": True}
