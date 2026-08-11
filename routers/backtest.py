# -*- coding: utf-8 -*-
r"""
routers/mikedb.py — a aba "MikeDB" do backtest avulso.

O QUE RESOLVE
    Hoje, pra testar uma ideia, o Santos precisa: entrar na VPS -> copiar os
    .gz -> consolidar -> rodar backtest_csv.py na mao -> baixar o parquet ->
    subir no painel. Este router faz TUDO isso no servidor, com o painel
    pedindo por HTTP, e desemboca no MESMO upload_id que o "Escolher arquivo"
    produz — ou seja, dali pra frente (Analisar H2H, filtros, rodar) nada
    muda no fluxo que ja existe.

DUAS VIAS (a casa escolhida decide, sem o usuario pensar nisso)
    1. COLETORES (superbet/estrelabet/betano/...):
       backtest_csv.py le o historico PARQUET particionado
       (bookmaker=/liga=/data=) e gera o recorte pedido. Rapido: le so as
       particoes que importam.
    2. BET365 (nao temos coletor proprio):
       coletor_betsapi.py raspa a BetsAPI -> converter_betsapi.py traduz o
       CSV largo pro parquet LONGO que o motor entende (dialeto bet365:
       mercado_tipo numerico, selecao de HC com nick, ML Casa/Fora).

ENDPOINTS
    GET  /mikedb/status                 -> diagnostico: pastas, scripts, CDP
    GET  /mikedb/catalogo?casa=         -> casas, ligas e periodo disponiveis
    POST /mikedb/gerar                  -> inicia a geracao (devolve job_id)
    GET  /mikedb/gerar/{job_id}         -> progresso + resultado
    GET  /mikedb/download/{upload_id}   -> baixa o parquet gerado

BLINDAGENS (as licoes desta semana, aplicadas de nascenca)
    - INSTANCIA UNICA: 1 geracao por vez (lock). O episodio do catalogo
      empilhando 5 copias e derrubando o banco nao se repete aqui.
    - SUBPROCESSO SEM SHELL: argumentos vao em lista (nunca shell=True), com
      validacao de data/nome — sem chance de injecao pelo formulario.
    - TIMEOUT por etapa + cancelamento: processo pendurado morre sozinho.
    - CDP CHECADO ANTES: a via bet365 exige o Chrome com --remote-debugging-
      port=9222 aberto e logado na BetsAPI (Cloudflare). Se a porta estiver
      fechada, o job falha na hora com instrucao clara, em vez de travar.
    - CAMINHO: C:\Users\Administrator\PyCharmMiscProject\MikeBacktest
      (a pasta unica — o E: nao existe mais). Override por env
      MIKEBACKTEST_DIR / MIKEBACKTEST_HIST se um dia mudar.
    - Erro sempre com etapa + ultimas linhas do log do script.

INSTALACAO
    1. Salvar como routers/mikedb.py
    2. main.py:  from routers import mikedb
                 app.include_router(mikedb.router)
    3. (opcional) env MIKEBACKTEST_DIR / MIKEBACKTEST_HIST se as pastas
       mudarem de lugar.
"""

import asyncio
import glob
import logging
import os
import re
import shutil
import socket
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from security import get_current_user
from workers.backtest_upload import UPLOAD_DIR, caminho_do_upload
# fonte UNICA: os mesmos mapas que o motor usa pra casar mercado/esporte.
# Se o runner ganhar um mercado novo, esta aba enxerga junto, sem eu duplicar
# tabela nenhuma aqui (o dialeto muda por casa: superbet fala 'OVER_UNDER',
# betano fala '13'/'157', estrelabet '18', bet365 '1450').
from workers.backtest_runner import (MERCADO_TIPOS_POR_CASA,
                                     ESPORTE_UI_PARA_BANCO)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/mikedb", tags=["mikedb"])


# ---------------------------------------------------------------- caminhos ---
def _primeira_pasta_existente(candidatas) -> Path:
    for c in candidatas:
        if c and Path(c).exists():
            return Path(c)
    return Path(candidatas[0])


BASE_DIR = _primeira_pasta_existente([
    os.environ.get("MIKEBACKTEST_DIR"),
    r"C:\Users\Administrator\PyCharmMiscProject\MikeBacktest",
])
HIST_DIR = Path(os.environ.get("MIKEBACKTEST_HIST") or (BASE_DIR / "historico"))
PYTHON = sys.executable  # o mesmo interpretador da API (tem polars/pyarrow)

SCRIPT_CSV = BASE_DIR / "backtest_csv.py"
SCRIPT_BETSAPI = BASE_DIR / "coletor_betsapi.py"
SCRIPT_CONVERTER = BASE_DIR / "converter_betsapi.py"

CDP_HOST, CDP_PORTA = "127.0.0.1", 9222
TIMEOUT_CSV_S = 45 * 60          # recorte do historico: minutos
TIMEOUT_BETSAPI_S = 6 * 60 * 60  # raspagem da BetsAPI: pode levar horas
TIMEOUT_CONVERTER_S = 30 * 60

CASA_BETSAPI = "bet365"  # unica casa sem coletor proprio

# ligas da BetsAPI (catalogo do coletor) — o front mostra estas quando bet365
LIGAS_BETSAPI = [
    {"valor": "H2H", "rotulo": "H2H GG League (e-basket)", "sport": "basquete"},
    {"valor": "BATTLE", "rotulo": "Battle (e-basket)", "sport": "basquete"},
    {"valor": "FUT_H2H", "rotulo": "H2H GG League (e-soccer)", "sport": "futebol"},
    {"valor": "FUT_GT", "rotulo": "GT League (e-soccer)", "sport": "futebol"},
    {"valor": "FUT_BATTLE", "rotulo": "Battle (e-soccer)", "sport": "futebol"},
    {"valor": "FUT_ADRIATIC", "rotulo": "Adriatic League (e-soccer)", "sport": "futebol"},
]

# Mercados LOGICOS que o painel oferece (chave = a mesma do motor).
# O usuario escolhe "Handicap FT"; o servidor traduz pro dialeto da casa.
MERCADOS_LOGICOS = [
    ("over_under_ft", "Over/Under FT"),
    ("ah_ft", "Handicap FT"),
    ("over_under_ht", "Over/Under HT"),
    ("ah_ht", "Handicap HT"),
    ("ml_ft", "Money Line FT"),
    ("ml_ht", "Money Line HT"),
    ("btts_ft", "Ambas marcam"),
    ("asian_over_under_ft", "Over/Under asiatico"),
]

ESPORTES = [{"valor": banco, "ui": ui, "rotulo": rot} for ui, banco, rot in [
    ("nba2k", "E-Basketball", "e-Basket (NBA2K)"),
    ("fifa", "E-Football", "e-Soccer (FIFA)"),
    ("ehockey", "E-Hockey", "e-Hockey"),
    ("etennis", "E-Tennis", "e-Tennis"),
] if ESPORTE_UI_PARA_BANCO.get(ui) == banco]


def _tipos_do_mercado(logico: str, casa: Optional[str]) -> str:
    """Traduz o mercado LOGICO pros mercado_tipo da casa (lista separada por
    virgula, que e o que o backtest_csv aceita). Sem casa escolhida, junta o
    dialeto de TODAS — o recorte sai igual e nao perde linha de casa nenhuma."""
    if not logico:
        return ""
    casas = ([casa.lower()] if casa else list(MERCADO_TIPOS_POR_CASA.keys()))
    tipos = []
    for c in casas:
        for t in (MERCADO_TIPOS_POR_CASA.get(c, {}).get(logico) or []):
            if t not in tipos:
                tipos.append(t)
    return ",".join(tipos)


_RE_DATA = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_SEGURO = re.compile(r"^[\w\s\-.,()/]{1,60}$", re.UNICODE)


# --------------------------------------------------- mercados de verdade ---
# O mapa do motor diz o que a casa DEVERIA ter. A amostragem do historico
# (03/ago) mostrou que a realidade e' mais rica e mais traicoeira:
#   - os codigos mudam por ESPORTE dentro da mesma casa (betano: '13' Total de
#     Gols no futebol, '1902'/'1907' total de pontos no basquete; estrelabet:
#     '18' vs '227'/'236'/'303');
#   - o historico da bet365 esta em OUTRO DIALETO (texto: 'GAME TOTAL',
#     'GAME SPREAD'), nao nos IDs numericos que o motor casa ('1450','1446') —
#     recorte dali sairia com 0 apostas no backtest.
# Entao o painel para de adivinhar: le os mercado_tipo que EXISTEM na casa (e
# no esporte) escolhidos, agrupa pelos nomes logicos quando reconhece, e
# mostra o resto com o nome que a casa usa. Cache de 1h — a varredura amostra
# 1 parquet por liga, entao custa segundos.
_CACHE_MERCADOS: dict = {}
_TTL_MERCADOS = 3600


def _escanear_mercados(casa: Optional[str], sport: Optional[str]) -> list:
    import time as _t
    chave = (casa or "", sport or "")
    cache = _CACHE_MERCADOS.get(chave)
    if cache and (_t.time() - cache[0]) < _TTL_MERCADOS:
        return cache[1]

    import polars as pl
    alvo = f"bookmaker={casa}" if casa else "bookmaker=*"
    amostra = []
    for pasta_liga in sorted(glob.glob(str(HIST_DIR / alvo / "liga=*")))[:60]:
        arqs = sorted(glob.glob(os.path.join(pasta_liga, "data=*", "*.parquet")))
        if arqs:
            amostra.append(arqs[-1])   # o dia mais recente daquela liga
    if not amostra:
        return []

    cols = ["mercado_tipo", "mercado", "sport"]
    quadros = []
    for f in amostra:
        try:
            quadros.append(pl.read_parquet(f, columns=cols))
        except Exception:
            continue
    if not quadros:
        return []
    df = pl.concat(quadros, how="vertical_relaxed")
    if sport:
        df = df.filter(pl.col("sport") == sport)
    if df.is_empty():
        return []

    contagem = (df.drop_nulls("mercado_tipo")
                  .group_by("mercado_tipo")
                  .agg(pl.col("mercado").drop_nulls().first().alias("exemplo"),
                       pl.len().alias("linhas"))
                  .sort("linhas", descending=True))

    # codigo -> nome logico do motor (quando a casa e' conhecida).
    # ATENCAO (bug pego em producao): um MESMO codigo pode servir a dois
    # mercados logicos — na superbet, 'OVER_UNDER' e over_under_ft E TAMBEM
    # asian_over_under_ft. Sem prioridade, o segundo sobrescrevia o primeiro e
    # o painel mostrava "Over/Under asiatico" no lugar do "Over/Under FT".
    # Agora vale a ORDEM de MERCADOS_LOGICOS (o principal vem antes) e o
    # setdefault impede a sobrescrita.
    ordem = [k for k, _ in MERCADOS_LOGICOS]
    logico_de = {}
    for c, mapa in MERCADO_TIPOS_POR_CASA.items():
        if casa and c != casa.lower():
            continue
        chaves = ([k for k in ordem if k in mapa] +
                  [k for k in mapa if k not in ordem])
        for chave_log in chaves:
            for cod in mapa[chave_log]:
                logico_de.setdefault(str(cod).upper(), chave_log)
    rotulo_de = dict(MERCADOS_LOGICOS)
    rotulo_de.update({
        "odd_even": "Ímpar/Par", "correct_score": "Placar exato",
        "double_chance_ft": "Dupla chance", "half_full": "Intervalo/Final",
        "over_under_ft_player": "Total do jogador", "player_total": "Total do jogador",
        "ah_ht": "Handicap HT", "ml_ht": "Money Line HT",
    })

    grupos: dict = {}
    soltos = []
    for linha in contagem.iter_rows(named=True):
        cod = str(linha["mercado_tipo"])
        if cod.upper().endswith("UPDATE"):
            continue  # linhas de placar: o backtest_csv ja mantem sempre
        log = logico_de.get(cod.upper())
        if log:
            g = grupos.setdefault(log, {"codigos": [], "linhas": 0})
            g["codigos"].append(cod)
            g["linhas"] += int(linha["linhas"])
        else:
            # codigo de TEXTO ja e' auto-explicativo ('PLAYER_TOTAL'); codigo
            # NUMERICO ('1902') nao diz nada, entao usa o nome do mercado —
            # cortado no primeiro parenteses pra nao virar nome de um jogo
            # especifico ("Boston Celtics (Losmi) - Total de Pontos (").
            exemplo = (linha["exemplo"] or "").split("(")[0].strip()
            rot = cod if not cod.isdigit() else (exemplo or cod)
            soltos.append({"valor": cod, "rotulo": rot[:34] or cod,
                           "detalhe": (linha["exemplo"] or "")[:60],
                           "linhas": int(linha["linhas"]), "conhecido": False})

    saida = [{"valor": ",".join(g["codigos"]),
              "rotulo": rotulo_de.get(k, k), "linhas": g["linhas"],
              "conhecido": True}
             for k, g in grupos.items()]
    saida.sort(key=lambda x: -x["linhas"])
    soltos.sort(key=lambda x: -x["linhas"])
    resultado = saida + soltos[:12]   # os 12 mercados extras mais gordos
    _CACHE_MERCADOS[chave] = (_t.time(), resultado)
    return resultado


@router.get("/mercados")
async def mercados(casa: Optional[str] = None, sport: Optional[str] = None,
                   usuario: dict = Depends(get_current_user)):
    """Mercados que EXISTEM no historico pra essa casa/esporte (amostrado)."""
    _valida_texto(casa, "casa")
    _valida_texto(sport, "sport")
    try:
        return {"mercados": await asyncio.to_thread(_escanear_mercados, casa, sport)}
    except Exception as e:
        logger.exception("[mikedb] falha escaneando mercados")
        return {"mercados": [], "aviso": str(e)}


# -------------------------------------------------------------------- jobs ---
class _CanceladoPeloUsuario(Exception):
    """Cancelamento pedido no painel — nao e' falha, entao nao vira 'erro'
    (o Santos precisa distinguir 'eu parei' de 'quebrou')."""


JOBS: dict = {}
_LOCK_GERACAO = asyncio.Lock()


def _novo_job() -> str:
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {
        "id": jid, "status": "rodando", "progresso": 0,
        "etapa": "na fila", "log": [], "erro": None, "resultado": None,
        "criado_em": datetime.now().isoformat(timespec="seconds"),
        # v9 (11/ago): cancelamento. _proc = processo do script rodando agora
        # (coletor/converter/backtest_csv); _cancelado = pedido do usuario.
        # Sem isto, geracao travada so saia com restart da API — e a trava de
        # "uma por vez" ficava presa junto, bloqueando tudo.
        "_proc": None, "_cancelado": False,
    }
    # nao deixa a memoria crescer pra sempre (mantem os 20 mais novos)
    if len(JOBS) > 20:
        for k in sorted(JOBS, key=lambda x: JOBS[x]["criado_em"])[:-20]:
            JOBS.pop(k, None)
    return jid


def _log_job(jid: str, linha: str, etapa: Optional[str] = None,
             progresso: Optional[int] = None):
    j = JOBS.get(jid)
    if not j:
        return
    linha = (linha or "").rstrip()
    if linha:
        j["log"].append(linha)
        del j["log"][:-200]  # so as ultimas 200 linhas
    if etapa:
        j["etapa"] = etapa
    if progresso is not None:
        j["progresso"] = max(0, min(100, int(progresso)))


# ------------------------------------------------------------- utilitarios ---
def _valida_data(v: Optional[str], nome: str):
    if v and not _RE_DATA.match(v):
        raise HTTPException(400, f"{nome} invalida: use AAAA-MM-DD")


def _valida_texto(v: Optional[str], nome: str):
    if v and not _RE_SEGURO.match(v):
        raise HTTPException(400, f"{nome} tem caracteres nao permitidos")


def _cdp_aberto() -> bool:
    """A via bet365 depende do Chrome com CDP na 9222 (Cloudflare exige sessao
    real). Checar ANTES evita job que trava 6h esperando browser que nao existe."""
    try:
        with socket.create_connection((CDP_HOST, CDP_PORTA), timeout=2):
            return True
    except Exception:
        return False


def _registrar_parquet(caminho_origem: Path, nome_amigavel: str) -> str:
    """Move o parquet gerado pra UPLOAD_DIR com a MESMA convencao de nome do
    salvar_upload (uuid8_nome), devolvendo o upload_id.

    Nao uso salvar_upload() de proposito: ele recebe bytes, e um recorte de 15
    dias tem centenas de MB — carregar isso na RAM da API so pra regravar em
    disco e' desperdicio. Aqui e' um move (mesmo volume) ou copy (volumes
    diferentes, ex: E: -> C:)."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\-.]", "_", nome_amigavel)[:80]
    if not safe.lower().endswith(".parquet"):
        safe += ".parquet"
    destino = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{safe}"
    try:
        shutil.move(str(caminho_origem), str(destino))
    except Exception:
        shutil.copy2(str(caminho_origem), str(destino))
        try:
            os.remove(caminho_origem)
        except Exception:
            pass
    return str(destino)


async def _rodar(jid: str, args: list, cwd: Path, timeout: int,
                 etapa: str, prog_ini: int, prog_fim: int) -> int:
    """Roda um script do MikeBacktest como subprocesso, transmitindo a saida
    pro log do job (o painel mostra ao vivo). Sem shell: args em lista."""
    _log_job(jid, f"$ {' '.join(str(a) for a in args)}", etapa=etapa,
             progresso=prog_ini)
    # UTF-8 obrigatorio: rodando como subprocesso, o Python herda a codificacao
    # do console do Windows (cp1252) e MORRE no primeiro emoji que o script
    # imprime (o converter estourou em "\U0001f4d6" antes de converter nada).
    # PYTHONUNBUFFERED e' o que faz o log aparecer AO VIVO: sem ele, o Python
    # do subprocesso usa buffer de BLOCO (4-8KB) porque a saida nao e' um
    # terminal — o script trabalha normal, mas o painel fica mudo por minutos
    # e parece travado (barra em 5%, log so com o comando).
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               PYTHONUNBUFFERED="1")
    try:
        proc = await asyncio.create_subprocess_exec(
            *[str(a) for a in args], cwd=str(cwd), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"script nao encontrado: {e}") from e

    _j = JOBS.get(jid) or {}
    if _j.get("_cancelado"):          # cancelou antes desta etapa comecar
        try:
            proc.kill()
        except Exception:
            pass
        raise _CanceladoPeloUsuario()
    _j["_proc"] = proc

    lidas = 0

    async def _drenar():
        nonlocal lidas
        while True:
            linha = await proc.stdout.readline()
            if not linha:
                break
            lidas += 1
            txt = linha.decode("utf-8", errors="replace").rstrip()
            # progresso heuristico: avanca devagar entre ini e fim conforme sai log
            p = prog_ini + min(prog_fim - prog_ini - 1,
                               int((prog_fim - prog_ini) * (1 - 0.985 ** lidas)))
            _log_job(jid, txt, progresso=p)

    try:
        await asyncio.wait_for(asyncio.gather(_drenar(), proc.wait()),
                               timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError(f"{etapa}: estourou o tempo limite "
                           f"({timeout // 60} min) e foi encerrado")
    _j["_proc"] = None
    if _j.get("_cancelado"):
        raise _CanceladoPeloUsuario()
    _log_job(jid, f"[fim] codigo de saida: {proc.returncode}", progresso=prog_fim)
    return proc.returncode or 0


# ------------------------------------------------------------------ modelos ---
class GerarRequest(BaseModel):
    casa: str = Field(..., description="bookmaker (bet365 usa a via BetsAPI)")
    liga: Optional[str] = Field(None, description="liga/torneio (ou codigo da BetsAPI)")
    sport: Optional[str] = Field(None, description="sport do historico (ex: E-Basketball)")
    mercado: Optional[str] = Field(None, description="mercado_tipo cru (avancado)")
    mercado_logico: Optional[str] = Field(None, description="chave do motor: ah_ft, over_under_ft, over_under_ht...")
    jogador: Optional[str] = None
    de: Optional[str] = Field(None, description="AAAA-MM-DD")
    ate: Optional[str] = Field(None, description="AAAA-MM-DD")
    # 'auto' = bet365 vai pra BetsAPI, resto vai pro historico.
    # Mas o historico JA TEM bet365 (coletor via JBot alimentando a MikeDB),
    # entao o painel pode forcar 'historico' pra bet365: sai em segundos em
    # vez de horas e nao depende do Chrome/Cloudflare.
    via: Optional[str] = Field("auto", description="auto | historico | betsapi")
    # paralelismo da raspagem (abas worker no Chrome). Default do coletor = 7.
    workers: Optional[int] = Field(None, ge=1, le=24, description="abas worker (1-24)")


# ----------------------------------------------------------------- /status ---
@router.get("/status")
async def status(usuario: dict = Depends(get_current_user)):
    """Diagnostico da aba: onde ele esta olhando e o que existe. Serve pro
    front avisar 'historico nao encontrado' em vez de dar erro seco."""
    return {
        "base_dir": str(BASE_DIR), "base_existe": BASE_DIR.exists(),
        "hist_dir": str(HIST_DIR), "hist_existe": HIST_DIR.exists(),
        "scripts": {
            "backtest_csv": SCRIPT_CSV.exists(),
            "coletor_betsapi": SCRIPT_BETSAPI.exists(),
            "converter_betsapi": SCRIPT_CONVERTER.exists(),
        },
        "chrome_cdp_aberto": _cdp_aberto(),
        "casa_betsapi": CASA_BETSAPI,
        "ligas_betsapi": LIGAS_BETSAPI,
        "esportes": ESPORTES,
        # o historico da bet365 veio de outro coletor, com rotulos de TEXTO
        # ('GAME TOTAL', 'GAME SPREAD') em vez dos IDs que o motor casa:
        # recorte dali roda, mas o backtest nao reconhece mercado nenhum.
        "aviso_bet365_historico": ("o histórico local da bet365 está em outro "
                                   "dialeto de mercado — o motor não reconhece; "
                                   "use a via BetsAPI"),
        "mercados": [{"valor": k, "rotulo": r} for k, r in MERCADOS_LOGICOS],
        # quais mercados EXISTEM em cada casa (o painel so oferece esses):
        # ah_ht so a estrelabet tem, bet365 so tem 3 mercados etc. Sem isto,
        # da pra pedir combinacao impossivel e receber arquivo vazio.
        "mercados_por_casa": {c: [k for k, _ in MERCADOS_LOGICOS if k in m]
                              for c, m in MERCADO_TIPOS_POR_CASA.items()},
        "geracao_em_andamento": _LOCK_GERACAO.locked(),
    }


# --------------------------------------------------------------- /catalogo ---
_CACHE_SPORT: dict = {}


def _sport_da_liga(casa: Optional[str], liga: str) -> Optional[str]:
    """Descobre o esporte de uma liga lendo 1 linha do parquet mais recente
    dela. Cache em memoria (o esporte de uma liga nao muda)."""
    chave = (casa or "*", liga)
    if chave in _CACHE_SPORT:
        return _CACHE_SPORT[chave]
    valor = None
    try:
        import pyarrow.parquet as pq
        alvo = f"bookmaker={casa}" if casa else "bookmaker=*"
        arqs = sorted(glob.glob(str(HIST_DIR / alvo / f"liga={liga}" /
                                    "data=*" / "*.parquet")))
        if arqs:
            tabela = pq.ParquetFile(arqs[-1]).read_row_group(0, columns=["sport"])
            col = tabela.column("sport").to_pylist()
            for v in col:
                if v:
                    valor = str(v)
                    break
    except Exception:
        valor = None
    _CACHE_SPORT[chave] = valor
    return valor


def _ler_catalogo(casa: Optional[str]) -> dict:
    """Le SO os nomes das pastas do historico particionado (barato: nao abre
    nenhum parquet). Devolve casas, ligas e o periodo coberto."""
    if not HIST_DIR.exists():
        return {"casas": [], "ligas": [], "periodo": None,
                "aviso": f"historico nao encontrado em {HIST_DIR}"}

    casas = sorted({os.path.basename(p).split("=", 1)[1]
                    for p in glob.glob(str(HIST_DIR / "bookmaker=*"))})
    alvo = f"bookmaker={casa}" if casa else "bookmaker=*"
    ligas = {}
    dias_todos = []
    for pdia in glob.glob(str(HIST_DIR / alvo / "liga=*" / "data=*")):
        partes = Path(pdia).parts
        try:
            liga = [x for x in partes if x.startswith("liga=")][0].split("=", 1)[1]
            dia = [x for x in partes if x.startswith("data=")][0].split("=", 1)[1]
        except IndexError:
            continue
        if not _RE_DATA.match(dia):
            continue
        dias_todos.append(dia)
        it = ligas.setdefault(liga, {"liga": liga, "dias": 0, "de": dia, "ate": dia})
        it["dias"] += 1
        it["de"] = min(it["de"], dia)
        it["ate"] = max(it["ate"], dia)

    # ESPORTE POR LIGA (pego em producao 03/ago): as ligas "H2H" da superbet
    # sao E-Football; pedir e-Basket nelas devolvia arquivo vazio e so o log
    # do script explicava. Agora o painel sabe antes de gerar. Le so a 1a
    # linha de 1 parquet por liga (row group 0, coluna sport) — barato.
    for it in ligas.values():
        it["sport"] = _sport_da_liga(casa, it["liga"])

    return {
        "casas": casas,
        "ligas": sorted(ligas.values(), key=lambda x: (-x["dias"], x["liga"])),
        "periodo": ({"de": min(dias_todos), "ate": max(dias_todos)}
                    if dias_todos else None),
    }


@router.get("/catalogo")
async def catalogo(casa: Optional[str] = None,
                   usuario: dict = Depends(get_current_user)):
    _valida_texto(casa, "casa")
    if casa and casa.lower() == CASA_BETSAPI:
        # bet365 nao vem do historico: as opcoes sao o catalogo do coletor
        return {"casas": [CASA_BETSAPI], "ligas": [], "periodo": None,
                "fonte": "betsapi", "ligas_betsapi": LIGAS_BETSAPI}
    dados = await asyncio.to_thread(_ler_catalogo, casa)
    dados["fonte"] = "historico"
    return dados


# ------------------------------------------------------------------ /gerar ---
async def _gerar_do_historico(jid: str, req: GerarRequest) -> str:
    saida = BASE_DIR / f"_mikedb_{jid}.parquet"
    args = [PYTHON, str(SCRIPT_CSV), "--hist", str(HIST_DIR),
            "--out", str(saida), "--parquet"]
    mercado = req.mercado or _tipos_do_mercado(req.mercado_logico, req.casa)
    for flag, valor in (("--casa", req.casa), ("--liga", req.liga),
                        ("--sport", req.sport), ("--mercado", mercado),
                        ("--jogador", req.jogador), ("--de", req.de),
                        ("--ate", req.ate)):
        if valor:
            args += [flag, valor]

    rc = await _rodar(jid, args, BASE_DIR, TIMEOUT_CSV_S,
                      "lendo o historico e recortando", 5, 85)
    if rc != 0:
        raise RuntimeError(f"backtest_csv.py terminou com erro (codigo {rc})")
    if not saida.exists():
        # o script loga o motivo (liga inexistente, periodo sem dado...)
        raise RuntimeError("nenhum tick bateu os filtros — o recorte ficou vazio "
                           "(confira liga e periodo no log abaixo)")
    return str(saida)


async def _gerar_da_betsapi(jid: str, req: GerarRequest) -> str:
    if not _cdp_aberto():
        raise RuntimeError(
            "a via bet365 precisa do Chrome aberto em modo depuracao e logado "
            "na BetsAPI. Na VPS: chrome.exe --remote-debugging-port=9222 "
            '--remote-allow-origins=* --user-data-dir="C:\\chrome_betsapi" '
            "https://betsapi.com (resolva o Cloudflare uma vez).")
    if not (req.de and req.ate):
        raise RuntimeError("informe o periodo (de/ate) para a coleta na BetsAPI")

    # nome deterministico (liga+periodo, NAO o id do job): o coletor le o CSV
    # existente, pula os event_id ja gravados e continua de onde parou. Com o
    # nome atrelado ao job, cada tentativa recomecaria a raspagem do zero —
    # inaceitavel num processo que leva horas.
    liga_slug = re.sub(r"[^\w]", "", (req.liga or "H2H"))[:20]
    csv_saida = BASE_DIR / f"betsapi_{liga_slug}_{req.de}_a_{req.ate}.csv"

    # ------------------------------------------------------------------
    # CAMINHO RAPIDO: o ACERVO (atualizar_betsapi.bat mantem 1 CSV por liga,
    # incremental e sem gap). Se o periodo pedido ja estiver la dentro, o
    # trabalho e' so RECORTAR — segundos, sem Chrome, sem Cloudflare, sem
    # esperar raspagem. O converter devolve codigo 2 quando o periodo nao
    # esta coberto; nesse caso caimos pra raspagem normal, sem drama.
    # ------------------------------------------------------------------
    acervo = BASE_DIR / f"acervo_betsapi_{liga_slug}.csv"
    if acervo.exists():
        _log_job(jid, f"acervo encontrado ({acervo.name}) — tentando recortar "
                      f"em vez de raspar", etapa="lendo o acervo", progresso=8)
        parquet_acervo = BASE_DIR / f"_mikedb_{jid}.parquet"
        args_ac = [PYTHON, str(SCRIPT_CONVERTER), "--csv", str(acervo),
                   "--out", str(parquet_acervo), "--ht"]
        if req.de:
            args_ac += ["--de", req.de]
        if req.ate:
            args_ac += ["--ate", req.ate]
        if (req.liga or "").upper().startswith("FUT_"):
            args_ac += ["--sport", "E-Football"]
        rc_ac = await _rodar(jid, args_ac, BASE_DIR, TIMEOUT_CONVERTER_S,
                             "recortando do acervo", 8, 90)
        if rc_ac == 0 and parquet_acervo.exists():
            _log_job(jid, "recorte do acervo pronto (nao precisou raspar)",
                     progresso=90)
            return str(parquet_acervo)
        _log_job(jid, "o acervo nao cobre esse periodo — vou raspar",
                 etapa="acervo incompleto", progresso=10)
    args = [PYTHON, str(SCRIPT_BETSAPI), "--liga", req.liga or "H2H",
            "--de", req.de, "--ate", req.ate, "--out", str(csv_saida)]
    if req.workers:
        args += ["--workers", str(int(req.workers))]
    rc = await _rodar(jid, args, BASE_DIR, TIMEOUT_BETSAPI_S,
                      "raspando a BetsAPI (pode demorar)", 5, 70)
    if rc != 0 or not csv_saida.exists():
        raise RuntimeError(f"coletor_betsapi.py falhou (codigo {rc}) — "
                           "veja o log; se pediu Cloudflare, resolva no Chrome "
                           "e rode de novo (ele retoma de onde parou)")

    parquet_saida = BASE_DIR / f"_mikedb_{jid}.parquet"
    # v3 do converter: ele agora RECORTA o acervo (o CSV vive e cresce, o
    # parquet sai do tamanho do teste). --ht traz junto os mercados de 1o
    # tempo, ja com o nome que o motor classifica como 'ht'
    # ("1º Tempo - Handicap"), entao over/under e handicap HT passam a
    # existir no arquivo — era o que faltava pro backtest de 1o tempo.
    args = [PYTHON, str(SCRIPT_CONVERTER), "--csv", str(csv_saida),
            "--out", str(parquet_saida), "--ht"]
    if req.de:
        args += ["--de", req.de]
    if req.ate:
        args += ["--ate", req.ate]
    # o converter nasce com sport=E-Basketball (default). As ligas FUT_* da
    # BetsAPI sao e-soccer: sem trocar aqui, o parser do motor filtraria
    # df[sport == 'E-Football'] e zeraria o arquivo inteiro.
    if (req.liga or "").upper().startswith("FUT_"):
        args += ["--sport", "E-Football"]
    rc = await _rodar(jid, args, BASE_DIR, TIMEOUT_CONVERTER_S,
                      "convertendo pro formato do motor", 70, 90)
    if rc != 0 or not parquet_saida.exists():
        raise RuntimeError(f"converter_betsapi.py falhou (codigo {rc})")
    # o CSV FICA: e' o que permite retomar/completar a raspagem depois sem
    # pagar as horas de novo. Ocupa disco, mas disco e' mais barato que tempo.
    return str(parquet_saida)


async def _executar(jid: str, req: GerarRequest):
    j = JOBS[jid]
    try:
        async with _LOCK_GERACAO:
            _log_job(jid, "iniciando", etapa="preparando", progresso=3)
            via = (req.via or "auto").lower()
            eh_betsapi = (via == "betsapi" or
                          (via == "auto" and (req.casa or "").lower() == CASA_BETSAPI))
            caminho = (await _gerar_da_betsapi(jid, req) if eh_betsapi
                       else await _gerar_do_historico(jid, req))

            _log_job(jid, "registrando o arquivo pro backtest",
                     etapa="registrando", progresso=92)
            partes = [p for p in (req.casa, req.liga, req.de, req.ate) if p]
            nome = "mikedb_" + "_".join(partes)
            upload_id = await asyncio.to_thread(
                _registrar_parquet, Path(caminho), nome)

            # mesmo resumo que o "Escolher arquivo" devolve -> o front ja sabe ler
            from routers.backtest_upload import _resumo_upload_leve
            resumo = await asyncio.to_thread(_resumo_upload_leve, upload_id)

            j["resultado"] = {"upload_id": upload_id,
                              "arquivo": os.path.basename(upload_id),
                              **(resumo or {})}
            j["status"] = "concluido"
            _log_job(jid, "pronto", etapa="concluido", progresso=100)
    except _CanceladoPeloUsuario:
        j["status"] = "cancelado"
        j["erro"] = None
        _log_job(jid, "cancelado pelo usuario", etapa="cancelado")
        for resto in BASE_DIR.glob(f"_mikedb_{jid}.*"):
            try:
                os.remove(resto)   # parquet/csv pela metade nao serve pra nada
            except Exception:
                pass
    except Exception as e:
        logger.exception("[mikedb] job %s falhou", jid)
        j["status"] = "erro"
        j["erro"] = str(e)
        _log_job(jid, f"ERRO: {e}", etapa="erro")
        # limpa restos pra nao entulhar a pasta
        # so os temporarios DESTE job (o CSV da BetsAPI tem nome proprio e
        # sobrevive de proposito, pra retomada)
        for resto in BASE_DIR.glob(f"_mikedb_{jid}.*"):
            try:
                os.remove(resto)
            except Exception:
                pass


@router.post("/gerar")
async def gerar(req: GerarRequest, usuario: dict = Depends(get_current_user)):
    _valida_texto(req.casa, "casa")
    _valida_texto(req.liga, "liga")
    _valida_texto(req.sport, "sport")
    _valida_texto(req.mercado, "mercado")
    if req.mercado_logico and req.mercado_logico not in dict(MERCADOS_LOGICOS):
        raise HTTPException(400, f"mercado desconhecido: {req.mercado_logico}")
    _valida_texto(req.jogador, "jogador")
    _valida_data(req.de, "data inicial")
    _valida_data(req.ate, "data final")
    if req.de and req.ate and req.de > req.ate:
        raise HTTPException(400, "a data inicial e maior que a final")
    if _LOCK_GERACAO.locked():
        raise HTTPException(409, "ja existe uma geracao em andamento — "
                                 "espere ela terminar (1 por vez, de proposito)")
    via = (req.via or "auto").lower()
    if via not in ("auto", "historico", "betsapi"):
        raise HTTPException(400, "via invalida: use auto, historico ou betsapi")
    eh_betsapi = (via == "betsapi" or
                  (via == "auto" and (req.casa or "").lower() == CASA_BETSAPI))
    if not eh_betsapi and not HIST_DIR.exists():
        raise HTTPException(400, f"historico nao encontrado em {HIST_DIR}")
    if not eh_betsapi and not SCRIPT_CSV.exists():
        raise HTTPException(400, f"backtest_csv.py nao encontrado em {BASE_DIR}")

    jid = _novo_job()
    asyncio.create_task(_executar(jid, req))
    return {"job_id": jid, "via": "betsapi" if eh_betsapi else "historico"}


@router.get("/gerar/{job_id}")
async def status_job(job_id: str, usuario: dict = Depends(get_current_user)):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "job nao encontrado (a API pode ter reiniciado)")
    return {**j, "log": j["log"][-40:]}


@router.post("/gerar/{job_id}/cancelar")
async def cancelar_geracao(job_id: str, usuario: dict = Depends(get_current_user)):
    """Mata o script em andamento e libera a trava de 'uma geracao por vez'.

    Seguro por etapa: matar o COLETOR no meio nao corrompe o acervo (o CSV e'
    gravado por evento e o resume re-raspa o ultimo com dedupe); matar o
    CONVERTER deixaria um parquet pela metade — por isso o _executar apaga os
    temporarios `_mikedb_<job>.*` ao cancelar."""
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "job nao encontrado (a API pode ter reiniciado)")
    if j["status"] not in ("rodando",):
        return {"ok": True, "status": j["status"], "aviso": "job ja terminou"}
    j["_cancelado"] = True
    proc = j.get("_proc")
    morto = False
    if proc is not None and proc.returncode is None:
        try:
            proc.kill()
            morto = True
        except Exception:
            logger.exception("[mikedb] falha matando processo do job %s", job_id)
    _log_job(job_id, "cancelamento pedido pelo usuario", etapa="cancelando")
    return {"ok": True, "processo_morto": morto, "status": "cancelando"}


# --------------------------------------------------------------- /download ---
@router.get("/download/{upload_id:path}")
async def download(upload_id: str, usuario: dict = Depends(get_current_user)):
    try:
        caminho = caminho_do_upload(upload_id)  # valida que esta dentro do UPLOAD_DIR
    except Exception as e:
        raise HTTPException(400, f"upload_id invalido: {e}")
    if not os.path.exists(caminho):
        raise HTTPException(404, "arquivo nao encontrado")
    return FileResponse(caminho, media_type="application/octet-stream",
                        filename=os.path.basename(caminho))
