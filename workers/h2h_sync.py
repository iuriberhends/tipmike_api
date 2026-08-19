# -*- coding: utf-8 -*-
"""
h2h_sync.py — ANALISA e PREENCHE o h2h_historico a partir dos ticks + TipManager.

Motivacao (28-31/jul): o backtest le o H2H do BANCO. Se o banco tem buraco
(par sem historico, ou jogo que o coletor viu e o historico nao tem), o
backtest decide com informacao diferente da que o bot vivo teria — foi essa
a origem do residuo "inserted-after" que sobrou depois do fix v12 do runner.
Este worker fecha o buraco: olha os pares que aparecem nos TICKS do periodo,
mede o que falta no h2h_historico e puxa da TM so o que falta.

Porte fiel dos scripts fase2_basket.py / fase2_fifa.py (que ja rodavam soltos
na VPS), com 3 diferencas deliberadas:
  1. asyncpg PARAMETRIZADO no lugar de psql por subprocess (o original montava
     SQL com f-string; nick com aspas quebrava/injetava);
  2. credenciais por VARIAVEL DE AMBIENTE (o original tinha email, senha,
     chave AES e token em texto puro no arquivo);
  3. roda como JOB com progresso, chamavel pelo painel.

O QUE NAO MUDOU (de proposito, pra nao brigar com o fase2 que ja escreve la):
  - event_id = 'tmh2_' + md5(jogador_a|jogador_b|fixture_date|placar)[:16]
  - dedup por JANELA DE 3 MINUTOS + placar + par (NAO por dia: dois jogos reais
    do mesmo par no mesmo dia sao distintos e ambos devem entrar)
  - ts gravado NAIVE, exatamente como o fixture_date da TM vem (o 'Z' da TM ja
    e horario de Brasilia — converter pra UTC deslocaria 3h e viraria
    vazamento fabricado no cutoff do backtest)

ENV obrigatorias:
    TM_EMAIL, TM_SENHA, TM_AES_KEY, TM_APP_TOKEN,
    TM_SUPABASE_URL, TM_SUPABASE_ANON
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- config ----

# torneios/endpoint por esporte — copiados dos fase2 (basket=2, fifa=1)
CFG_ESPORTE = {
    "E-Basketball": {
        "tm_sport": 2,
        "torneios": [5, 6, 7, 8, 21, 23, 37, 43, 46, 51],
        "url_h2h": "https://h2h.tipmanager.xyz:2087/v2/ebasket_encrypted",
    },
    "E-Football": {
        "tm_sport": 1,
        "torneios": [1, 2, 3, 4, 20, 25, 35, 36, 41, 42, 44, 45, 47, 48, 49, 50],
        "url_h2h": "https://h2h.tipmanager.xyz:2087/v2/esoccer_encrypted",
    },
}
# apelidos vindos da UI
ALIAS_ESPORTE = {
    "nba2k": "E-Basketball", "basket": "E-Basketball",
    "e-basketball": "E-Basketball", "basketball": "E-Basketball",
    "fifa": "E-Football", "efootball": "E-Football",
    "e-football": "E-Football", "soccer": "E-Football",
}

URL_PLAYERS = "https://api.tipmanager.net/v1/players"

THROTTLE_MIN = 1.5
THROTTLE_MAX = 10.0
SLEEP_429 = 8
TETO_TM = 100          # resposta no teto = a TM cortou -> dispara o 24x
LIMITE_PARES_PADRAO = 60
MIN_CONFRONTOS_PADRAO = 20

_throttle = {"atual": THROTTLE_MIN, "ok": 0}

# job store em memoria (some se a API reiniciar — o trabalho ja gravado no
# banco permanece; e so o relatorio que se perde)
JOBS: dict[str, dict] = {}
MAX_JOBS = 40

# SERIALIZA os preenchimentos: NOT EXISTS nao e atomico sem constraint unica,
# entao dois jobs simultaneos (ou dois cliques no botao) enxergam "nao existe"
# ao mesmo tempo e inserem o mesmo jogo duas vezes. Um por vez.
_LOCK_PREENCHER = asyncio.Lock()


def normalizar_esporte(valor: Optional[str]) -> Optional[str]:
    if not valor:
        return None
    v = str(valor).strip()
    if v in CFG_ESPORTE:
        return v
    return ALIAS_ESPORTE.get(v.lower())


def _creds() -> dict:
    faltando = [k for k in ("TM_EMAIL", "TM_SENHA", "TM_AES_KEY", "TM_APP_TOKEN",
                            "TM_SUPABASE_URL", "TM_SUPABASE_ANON")
                if not os.environ.get(k)]
    if faltando:
        raise RuntimeError(
            "credenciais da TipManager ausentes no ambiente: "
            + ", ".join(faltando)
            + ". Defina as variaveis no servico (nssm set tipmikeapi AppEnvironmentExtra)."
        )
    chave = os.environ["TM_AES_KEY"]
    return {
        "email": os.environ["TM_EMAIL"],
        "senha": os.environ["TM_SENHA"],
        "aes": chave.encode() if isinstance(chave, str) else chave,
        "token_app": os.environ["TM_APP_TOKEN"],
        "supabase_url": os.environ["TM_SUPABASE_URL"].rstrip("/"),
        "supabase_anon": os.environ["TM_SUPABASE_ANON"],
    }


# ------------------------------------------------------- cliente TipManager --
# tudo aqui e BLOQUEANTE (requests/curl_cffi) — sempre chamado via to_thread

def _throttle_429():
    _throttle["atual"] = min(_throttle["atual"] * 1.8, THROTTLE_MAX)
    _throttle["ok"] = 0


def _throttle_ok():
    _throttle["ok"] += 1
    if _throttle["ok"] >= 5 and _throttle["atual"] > THROTTLE_MIN:
        _throttle["atual"] = max(_throttle["atual"] - 0.5, THROTTLE_MIN)
        _throttle["ok"] = 0


def _decrypt(conteudo: bytes, aes_key: bytes):
    from Crypto.Cipher import AES
    iv, ct, tag = conteudo[:12], conteudo[12:-16], conteudo[-16:]
    c = AES.new(aes_key, AES.MODE_GCM, nonce=iv, mac_len=16)
    c.update(b"")
    return json.loads(gzip.decompress(c.decrypt_and_verify(ct, tag)))


def _login(cred: dict) -> str:
    import requests
    r = requests.post(
        f"{cred['supabase_url']}/auth/v1/token?grant_type=password",
        headers={"apikey": cred["supabase_anon"], "Content-Type": "application/json"},
        json={"email": cred["email"], "password": cred["senha"]}, timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]


def _carregar_players(cred: dict, cfg: dict) -> dict:
    """nick -> LISTA de ids (a TM da nomes/ids diferentes pro mesmo jogador
    em torneios diferentes; guardar so o 1o id subestima o H2H)."""
    import requests
    mapa: dict[str, list] = {}
    for t in cfg["torneios"]:
        try:
            r = requests.get(
                f"{URL_PLAYERS}?id_sport={cfg['tm_sport']}&id_tournament={t}&place=9",
                headers={"Authorization": f"Bearer {cred['token_app']}"}, timeout=15)
            if r.status_code != 200:
                continue
            for p in r.json():
                nick = str(p.get("description") or "").strip().lower()
                pid = p.get("id")
                if not nick or pid is None:
                    continue
                for chave in (nick, nick.replace(" ", "")):
                    lst = mapa.setdefault(chave, [])
                    if pid not in lst:
                        lst.append(pid)
        except Exception as e:
            logger.warning(f"[h2h_sync] players torneio {t}: {str(e)[:80]}")
    return mapa


def _nick_base(chave: str) -> str:
    """'nick (ecf volta)' -> 'nick'. NAO mexe em 'nick2' (jogador diferente)."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", chave or "").strip()


def _ids_do_nick(pmap: dict, nome: str) -> list:
    n = (nome or "").lower().strip()
    n_sem = n.replace(" ", "")
    alvo = _nick_base(n)
    ids = []
    for chave, lst in pmap.items():
        if chave == n or chave.replace(" ", "") == n_sem or _nick_base(chave) == alvo:
            for pid in lst:
                if pid not in ids:
                    ids.append(pid)
    return ids


def _h2h(cred: dict, cfg: dict, token: str, ida, idb, hour_range=(0, 24)):
    from curl_cffi import requests as cffi
    body = {"id_sport": cfg["tm_sport"], "id_player_a": ida, "id_player_b": idb,
            "timezone": "America/Sao_Paulo", "hour_range": list(hour_range)}
    for tent in range(6):
        try:
            r = cffi.post(
                cfg["url_h2h"], json=body,
                headers={"Authorization": f"Bearer {cred['token_app']}",
                         "x-api-key": token, "Content-Type": "application/json",
                         "Origin": "https://tipmanager.net",
                         "Referer": "https://tipmanager.net/"},
                impersonate="chrome110", timeout=25, verify=False)
            if r.status_code == 200:
                _throttle_ok()
                return _decrypt(r.content, cred["aes"]), token
            if r.status_code == 401:
                token = _login(cred)
                continue
            if r.status_code == 429:
                ra = r.headers.get("Retry-After") or r.headers.get("retry-after")
                espera = (int(ra) + 2) if (ra and str(ra).strip().isdigit()
                                           ) else min(SLEEP_429 * (2 ** tent), 300)
                _throttle_429()
                time.sleep(espera)
                continue
            if r.status_code in (502, 503, 504):
                time.sleep(5 * (tent + 1))
                continue
            if r.status_code in (403, 419):
                _throttle_429()
                time.sleep(min(30 * (tent + 1), 180))
                token = _login(cred)
                continue
            time.sleep(5 * (tent + 1))
            token = _login(cred)
        except Exception as e:
            logger.warning(f"[h2h_sync] h2h {ida}x{idb}: {str(e)[:80]}")
            time.sleep(2 * (tent + 1))
    return None, token


def _jogos_de(data) -> list:
    if not isinstance(data, dict):
        return []
    return (data.get("info") or {}).get("last_50") or []


def _h2h_completo(cred, cfg, token, ida, idb):
    """1 chamada barata; se vier no teto (TM cortou), 24 chamadas horarias."""
    data, token = _h2h(cred, cfg, token, ida, idb, (0, 24))
    jogos = _jogos_de(data)
    if len(jogos) < TETO_TM:
        return data, token
    bucket = {}
    for m in jogos:
        fd = m.get("fixture_date") or m.get("date")
        if fd:
            bucket[(m.get("id_player_a"), m.get("id_player_b"), fd)] = m
    falhas = []
    for h in range(24):
        d, token = _h2h(cred, cfg, token, ida, idb, (h, h + 1))
        if d is None:
            falhas.append(h)
        else:
            for m in _jogos_de(d):
                fd = m.get("fixture_date") or m.get("date")
                if fd:
                    bucket[(m.get("id_player_a"), m.get("id_player_b"), fd)] = m
        time.sleep(_throttle["atual"])
    for h in falhas:
        d, token = _h2h(cred, cfg, token, ida, idb, (h, h + 1))
        if d is not None:
            for m in _jogos_de(d):
                fd = m.get("fixture_date") or m.get("date")
                if fd:
                    bucket[(m.get("id_player_a"), m.get("id_player_b"), fd)] = m
        time.sleep(_throttle["atual"])
    return {"info": {"last_50": list(bucket.values())}}, token


def _h2h_multi(cred, cfg, token, ids_a: list, ids_b: list):
    """Acumula TODAS as combinacoes de ids do par (nomes variam por torneio)."""
    bucket, tentadas = {}, 0
    for ida in ids_a:
        for idb in ids_b:
            tentadas += 1
            if tentadas > 1:
                time.sleep(0.4)
            data, token = _h2h_completo(cred, cfg, token, ida, idb)
            for m in _jogos_de(data):
                fd = m.get("fixture_date") or m.get("date")
                if fd:
                    bucket[(m.get("id_player_a"), m.get("id_player_b"), fd)] = m
    return ({"info": {"last_50": list(bucket.values())}} if bucket else None), token


# ------------------------------------------------------------- utilitarios --

def _ts_naive(valor) -> Optional[datetime]:
    """fixture_date da TM -> datetime NAIVE, sem conversao de fuso.
    O 'Z' da TM e mentiroso: o horario ja vem em America/Sao_Paulo (regra
    registrada e reconfirmada no diag de 28/jul). Converter pra UTC aqui
    deslocaria todo registro 3h e quebraria o cutoff do backtest."""
    if not valor:
        return None
    s = str(valor).strip().replace("T", " ")
    if s.endswith("Z"):
        s = s[:-1]
    if "+" in s[10:]:
        s = s[:10] + s[10:].split("+")[0]
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _event_id(ja: str, jb: str, date_raw, fa, fb) -> str:
    chave = f"{ja}|{jb}|{date_raw}|{fa}|{fb}"
    return "tmh2_" + hashlib.md5(chave.encode()).hexdigest()[:16]


def _novo_job(tipo: str, params: dict) -> str:
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"id": jid, "tipo": tipo, "status": "rodando", "progresso": 0,
                 "etapa": "iniciando", "params": params, "erro": None,
                 "criado_em": datetime.now(timezone.utc).isoformat(),
                 "relatorio": None}
    if len(JOBS) > MAX_JOBS:
        for velho in sorted(JOBS, key=lambda k: JOBS[k]["criado_em"])[:len(JOBS) - MAX_JOBS]:
            JOBS.pop(velho, None)
    return jid


# v3 (19/ago) - DIAGNOSTICO EM PORTUGUES, ADITIVO.
# O analisar continua devolvendo TUDO que devolvia (a lista de pares que
# alimenta o /preencher nao muda uma virgula). O que entra e' o contrato do
# h2h_diagnostico: as quatro perguntas, o veredito e o "o que fazer".
# Import tolerante de proposito: sem o modulo, o analisar roda igual a hoje.
try:
    from workers import h2h_diagnostico as _diag
except Exception:            # pragma: no cover
    try:
        import h2h_diagnostico as _diag
    except Exception:
        _diag = None


# --------------------------------------------------------------- ANALISAR ---

SQL_ANALISE = """
-- v2 (17/ago): a lista de jogos vem da h2h_matches, nao mais de um
-- GROUP BY na `ticks`.
-- MEDIDO: o caminho antigo dava Parallel Seq Scan lendo 12,2 MILHOES de
-- linhas da ticks pra destilar 1.564 eventos = 52,8s em 3 DIAS (e cresce
-- linear: 15 dias viravam minutos, dai o "tempo esgotado"). A h2h_matches
-- ja e' exatamente isso — 1 linha por evento, com par, liga e horario,
-- mantida a cada 60s pelo atualizar_h2h.
-- DIFERENCA DE SEMANTICA (assumida de proposito): `ini` passa a ser o
-- ts_fim (ultimo tick do jogo) em vez do MIN(ts) (primeiro). Num jogo de
-- ~20min a janela de +-45min vira [inicio-25, inicio+65] — o proprio jogo
-- continua dentro com folga, e o jogo ANTERIOR do mesmo par (2h antes)
-- segue de fora. Validar comparando os totais antes/depois no mesmo
-- periodo: pares, cobertos e so-tick tem que bater.
WITH jogos AS (
    SELECT m.event_id,
           m.ts_fim AS ini,
           UPPER(m.jogador_a) AS ja,
           UPPER(m.jogador_b) AS jb
    FROM h2h_matches m
    WHERE m.bookmaker = $1
      AND m.sport = $2
      AND m.ts_fim >= $3 AND m.ts_fim < $4
      AND m.jogador_a IS NOT NULL AND m.jogador_b IS NOT NULL
      AND m.score_a IS NOT NULL AND m.score_b IS NOT NULL
      AND ($5::text IS NULL OR m.liga = $5)
),
faltas AS (
    SELECT LEAST(j.ja, j.jb) AS p1,
           GREATEST(j.ja, j.jb) AS p2,
           COUNT(*) AS jogos_ticks,
           COUNT(*) FILTER (WHERE NOT ex.ok) AS jogos_faltando
    FROM jogos j
    CROSS JOIN LATERAL (
        SELECT EXISTS (
            SELECT 1 FROM h2h_historico h
            WHERE h.sport = $2
              AND ((UPPER(h.jogador_a) = j.ja AND UPPER(h.jogador_b) = j.jb)
                OR (UPPER(h.jogador_a) = j.jb AND UPPER(h.jogador_b) = j.ja))
              AND (h.ts AT TIME ZONE 'America/Sao_Paulo')
                  BETWEEN j.ini - INTERVAL '45 minutes'
                      AND j.ini + INTERVAL '45 minutes'
        ) AS ok
    ) ex
    GROUP BY 1, 2
)
SELECT f.p1, f.p2, f.jogos_ticks, f.jogos_faltando,
       (SELECT COUNT(*) FROM h2h_historico h
         WHERE h.sport = $2
           AND ((UPPER(h.jogador_a) = f.p1 AND UPPER(h.jogador_b) = f.p2)
             OR (UPPER(h.jogador_a) = f.p2 AND UPPER(h.jogador_b) = f.p1))
       ) AS jogos_hist
FROM faltas f
ORDER BY f.jogos_faltando DESC, jogos_hist ASC
"""


async def analisar(pool, params: dict, job_id: Optional[str] = None) -> dict:
    """Diagnostico na REGUA DO RUNNER: o H2H efetivo e ticks(casa) UNION
    h2h_historico com dedup (par + placar + 45min, mantendo o hist). Logo,
    todo jogo listado aqui esta coberto AGORA (veio dos ticks); a pergunta
    certa e QUANTOS sobrevivem ao expurgo de ~3 dias da tabela ticks:
      - coberto_hist  -> tem correspondente no h2h_historico = PERMANENTE
      - so_tick       -> so existe na perna dos ticks = EVAPORA no expurgo
    O casamento com o hist usa par + janela de 45min (a mesma do dedup do
    runner), SEM exigir placar igual — de proposito: se o coletor caiu no
    meio, o tick tem placar parcial e o hist o oficial; exigir placar igual
    marcaria como "descoberto" um jogo que o hist ja cobre melhor."""
    # fonte ARQUIVO (parquet do avulso): os ticks de upload externo (BetsAPI
    # etc.) nunca passaram pela tabela `ticks` — analisar o banco por casa/
    # periodo devolve 0 pares e um "ja cobre" vaziamente verdadeiro (furo
    # pego pelo Santos no 1o teste real, 01/ago). Com upload_id, a analise
    # le OS PARES DO PROPRIO ARQUIVO.
    if params.get("upload_id"):
        return await _analisar_de_arquivo(pool, params, job_id=job_id)

    esporte = normalizar_esporte(params.get("esporte"))
    if esporte is None:
        raise ValueError(f"esporte nao suportado: {params.get('esporte')!r} "
                         f"(use {' ou '.join(CFG_ESPORTE)})")
    casa = params.get("casa")
    if not casa:
        raise ValueError("casa (bookmaker) e obrigatoria")
    liga = params.get("liga") or None
    min_conf = int(params.get("min_confrontos") or MIN_CONFRONTOS_PADRAO)

    fim = params.get("data_fim") or datetime.now(timezone.utc)
    ini = params.get("data_inicio")
    if ini is None:
        ini = fim - timedelta(days=int(params.get("dias") or 15))

    if job_id:
        JOBS[job_id].update(etapa="lendo ticks", progresso=5)

    async with pool.acquire() as conn:
        rows = await conn.fetch(SQL_ANALISE, casa, esporte, ini, fim, liga)

    pares = []
    for r in rows:
        so_tick = int(r["jogos_faltando"] or 0)
        cobertos = int(r["jogos_ticks"] or 0) - so_tick
        precisa = so_tick > 0 or (r["jogos_hist"] or 0) < min_conf
        pares.append({
            "jogador_a": r["p1"], "jogador_b": r["p2"],
            "jogos_ticks": r["jogos_ticks"],
            "cobertos_hist": cobertos,          # permanentes (regua do runner)
            "so_tick": so_tick,                 # so na perna ticks (nao permanente)
            "jogos_hist": r["jogos_hist"],      # profundidade total do par
            "precisa": precisa,
            # v3: o texto antigo dizia "sem copia permanente" porque a perna
            # de tick vinha da tabela `ticks`, expurgada em ~3 dias. Desde a
            # v15 do runner ela vem da h2h_matches, que e' PERMANENTE (o
            # proprio runner: "guarda jogos que o expurgo ja comeu dos
            # ticks"). Entao so_tick NAO evapora - so' nao tem o placar
            # oficial da TM ainda.
            "motivo": (f"{so_tick} jogo(s) que o nosso coletor viu e a "
                       f"TipManager ainda nao confirmou"
                       if so_tick > 0
                       else ("poucos confrontos entre os dois"
                             if precisa else "ok")),
        })

    rel = {
        "casa": casa, "esporte": esporte, "liga": liga,
        "periodo": {"inicio": ini.isoformat() if hasattr(ini, "isoformat") else str(ini),
                    "fim": fim.isoformat() if hasattr(fim, "isoformat") else str(fim)},
        "min_confrontos": min_conf,
        "pares_total": len(pares),
        "pares_ok": sum(1 for p in pares if not p["precisa"]),
        "pares_precisam": sum(1 for p in pares if p["precisa"]),
        "jogos_ticks_total": sum(p["jogos_ticks"] for p in pares),
        "cobertos_hist_total": sum(p["cobertos_hist"] for p in pares),
        "so_tick_total": sum(p["so_tick"] for p in pares),
        "pares": pares,
    }

    # v3: o diagnostico em portugues. Falha aqui NAO derruba a analise - o
    # relatorio antigo (e o /preencher) seguem valendo sem ele.
    if _diag is not None:
        try:
            if job_id:
                JOBS[job_id].update(etapa="conferindo o dado", progresso=70)
            nicks = sorted({p["jogador_a"] for p in pares}
                           | {p["jogador_b"] for p in pares})
            rel["diagnostico"] = await _diag.diagnosticar(
                pool, casa=casa, sport=esporte, nicks=nicks,
                inicio=ini, fim=fim, jogos_periodo=None)
        except Exception as e:
            rel["diagnostico"] = {
                "veredito": "indisponivel",
                "resumo": "Nao consegui conferir o dado desta vez.",
                "erro": f"{type(e).__name__}: {str(e)[:160]}",
                "checagens": [], "fontes": [], "filtros": [], "jogadores": [],
            }

    if job_id:
        JOBS[job_id].update(status="concluido", progresso=100,
                            etapa="analise pronta", relatorio=rel)
    return rel


def _ler_pares_do_parquet(caminho: str) -> tuple:
    """Le o parquet do upload e devolve (esporte, casa, eventos) com
    eventos = [{ja, jb, ini(datetime naive)}]. BLOQUEANTE — chamar via
    to_thread. Fuso: MESMA convencao do backtest_upload (Z e BRT; parse
    naive, sem somar/subtrair 3h)."""
    import pandas as pd
    cols = ["event_id", "ts", "jogador_a", "jogador_b",
            "score_home", "score_away", "sport", "bookmaker"]
    df = pd.read_parquet(caminho, columns=None)
    faltam = [c for c in cols if c not in df.columns]
    if faltam:
        raise ValueError(f"parquet sem colunas obrigatorias: {faltam}")
    df = df[cols].copy()
    ts = df["ts"]
    if pd.api.types.is_datetime64_any_dtype(ts):
        if getattr(ts.dt, "tz", None) is not None:
            ts = ts.dt.tz_localize(None)  # descarta tz, mantem o relogio
    else:
        ts = pd.to_datetime(ts.astype(str).str.replace("Z", "", regex=False),
                            errors="coerce")
    df["ts"] = ts
    df = df[df["jogador_a"].notna() & df["jogador_b"].notna()
            & df["score_home"].notna() & df["score_away"].notna()
            & df["ts"].notna()]
    if df.empty:
        return None, None, []
    esporte = str(df["sport"].mode().iat[0]) if df["sport"].notna().any() else None
    casa = str(df["bookmaker"].mode().iat[0]) if df["bookmaker"].notna().any() else None
    g = (df.groupby("event_id")
           .agg(ja=("jogador_a", "first"), jb=("jogador_b", "first"),
                ini=("ts", "min")).reset_index())
    eventos = [{"ja": str(r.ja).upper().strip(), "jb": str(r.jb).upper().strip(),
                "ini": r.ini.to_pydatetime()} for r in g.itertuples()]
    return esporte, casa, eventos


SQL_EXISTE_LOTE = """
SELECT i.ord, EXISTS (
    SELECT 1 FROM h2h_historico h
    WHERE h.sport = $1
      AND ((UPPER(h.jogador_a) = $2 AND UPPER(h.jogador_b) = $3)
        OR (UPPER(h.jogador_a) = $3 AND UPPER(h.jogador_b) = $2))
      AND h.ts BETWEEN i.ini - INTERVAL '45 minutes'
                   AND i.ini + INTERVAL '45 minutes'
) AS ok
FROM unnest($4::timestamp[]) WITH ORDINALITY AS i(ini, ord)
"""


async def _analisar_de_arquivo(pool, params: dict, job_id: Optional[str] = None) -> dict:
    """Analise com os pares do PARQUET subido (fonte=arquivo). O casamento
    com o hist e naive x naive (os dois relogios ja sao BRT), janela de
    45min — a mesma regua do dedup do runner."""
    upload_id = params["upload_id"]
    try:
        from workers.backtest_upload import caminho_do_upload
        caminho = caminho_do_upload(upload_id)
    except Exception:
        caminho = upload_id  # upload_id ja e o caminho no disco

    if job_id:
        JOBS[job_id].update(etapa="lendo o parquet", progresso=5)
    esporte_arq, casa_arq, eventos = await asyncio.to_thread(
        _ler_pares_do_parquet, caminho)
    esporte = normalizar_esporte(params.get("esporte") or esporte_arq)
    if esporte is None:
        raise ValueError(f"esporte nao suportado no arquivo: {esporte_arq!r}")
    if not eventos:
        raise ValueError("o parquet nao tem eventos com par e placar")
    min_conf = int(params.get("min_confrontos") or MIN_CONFRONTOS_PADRAO)

    if job_id:
        JOBS[job_id].update(etapa=f"cruzando {len(eventos)} jogos com o hist",
                            progresso=25)
    por_par: dict[tuple, dict] = {}
    for e in eventos:
        k = (min(e["ja"], e["jb"]), max(e["ja"], e["jb"]))
        por_par.setdefault(k, []).append(e["ini"])

    pares = []
    async with pool.acquire() as conn:
        for i, ((p1, p2), inis) in enumerate(sorted(por_par.items()), 1):
            if job_id and i % 20 == 0:
                JOBS[job_id].update(progresso=25 + int(60 * i / len(por_par)))
            rows = await conn.fetch(SQL_EXISTE_LOTE, esporte, p1, p2, inis)
            cobertos = sum(1 for r in rows if r["ok"])
            so_tick = len(inis) - cobertos
            hist_total = await conn.fetchval(
                """SELECT COUNT(*) FROM h2h_historico
                   WHERE sport = $1
                     AND ((UPPER(jogador_a) = $2 AND UPPER(jogador_b) = $3)
                       OR (UPPER(jogador_a) = $3 AND UPPER(jogador_b) = $2))""",
                esporte, p1, p2)
            precisa = so_tick > 0 or (hist_total or 0) < min_conf
            pares.append({
                "jogador_a": p1, "jogador_b": p2,
                "jogos_ticks": len(inis), "cobertos_hist": cobertos,
                "so_tick": so_tick, "jogos_hist": hist_total,
                "precisa": precisa,
                "motivo": (f"{so_tick} jogo(s) do arquivo sem copia no hist"
                           if so_tick > 0
                           else ("historico raso" if precisa else "ok")),
            })
    pares.sort(key=lambda p: (-(p["so_tick"] or 0), p["jogos_hist"] or 0))

    rel = {
        "fonte": "arquivo", "upload_id": upload_id,
        "casa": casa_arq or params.get("casa"), "esporte": esporte,
        "liga": None,
        "periodo": {"inicio": min(e["ini"] for e in eventos).isoformat(),
                    "fim": max(e["ini"] for e in eventos).isoformat()},
        "min_confrontos": min_conf,
        "pares_total": len(pares),
        "pares_ok": sum(1 for p in pares if not p["precisa"]),
        "pares_precisam": sum(1 for p in pares if p["precisa"]),
        "jogos_ticks_total": sum(p["jogos_ticks"] for p in pares),
        "cobertos_hist_total": sum(p["cobertos_hist"] for p in pares),
        "so_tick_total": sum(p["so_tick"] for p in pares),
        "pares": pares,
    }
    if job_id:
        JOBS[job_id].update(status="concluido", progresso=100,
                            etapa="analise pronta", relatorio=rel)
    return rel


# -------------------------------------------------------------- PREENCHER ---

SQL_INSERT = """
INSERT INTO h2h_historico
    (event_id, ts, sport, jogador_a, jogador_b,
     score_home, score_away, score_ht_home, score_ht_away,
     id_player_a, id_player_b)
SELECT $1, $2::timestamp, $3, $4, $5, $6, $7, $8, $9, $10, $11
WHERE NOT EXISTS (
    -- (a) cinto: o MESMO event_id ja esta la (re-run do mesmo jogo)
    SELECT 1 FROM h2h_historico WHERE event_id = $1
)
AND NOT EXISTS (
    -- (b) suspensorio: mesmo jogo gravado em qualquer ORIENTACAO.
    -- ATENCAO (bug pego antes de rodar): a versao anterior casava o par nas
    -- duas ordens mas exigia o placar SEMPRE na ordem $6/$7 — registro antigo
    -- gravado espelhado (TAAPZ x KARMA 63-38 vs KARMA x TAAPZ 38-63) escapava
    -- e entrava DUPLICADO. Agora par e placar sao checados JUNTOS, na mesma
    -- orientacao, nos dois sentidos. Janela de 3min preservada do fase2 (nao
    -- por dia: dois jogos reais do mesmo par no mesmo dia sao distintos).
    SELECT 1 FROM h2h_historico
     WHERE sport = $3
       AND ABS(EXTRACT(EPOCH FROM (ts - $2::timestamp))) < 180
       AND (
            (UPPER(jogador_a) = $4 AND UPPER(jogador_b) = $5
             AND score_home = $6 AND score_away = $7)
         OR (UPPER(jogador_a) = $5 AND UPPER(jogador_b) = $4
             AND score_home = $7 AND score_away = $6)
       )
)
"""

# Espelho do NOT EXISTS acima, como SELECT — usado pelo dry_run pra contar o
# que ENTRARIA sem gravar nada. Tem que ficar SEMPRE igual ao SQL_INSERT.
SQL_JA_EXISTE = """
SELECT EXISTS (
    SELECT 1 FROM h2h_historico WHERE event_id = $1
    UNION ALL
    SELECT 1 FROM h2h_historico
     WHERE sport = $3
       AND ABS(EXTRACT(EPOCH FROM (ts - $2::timestamp))) < 180
       AND (
            (UPPER(jogador_a) = $4 AND UPPER(jogador_b) = $5
             AND score_home = $6 AND score_away = $7)
         OR (UPPER(jogador_a) = $5 AND UPPER(jogador_b) = $4
             AND score_home = $7 AND score_away = $6)
       )
)
"""

# Registros legados do MESMO jogo em outro formato: o fase2 apaga os bkp_/inc_
# do par depois de preencher pela TM (senao o mesmo jogo fica contado duas
# vezes no Ult.N). Portado igual, mas SO quando a TM realmente trouxe historico
# do par — nunca apagamos backup sem ter posto algo melhor no lugar.
SQL_LIMPAR_BACKUP = """
DELETE FROM h2h_historico
 WHERE sport = $1
   AND (event_id LIKE 'bkp\\_%' OR event_id LIKE 'inc\\_%')
   AND ((UPPER(jogador_a) = $2 AND UPPER(jogador_b) = $3)
     OR (UPPER(jogador_a) = $3 AND UPPER(jogador_b) = $2))
"""


async def _inserir_jogos(conn, esporte: str, pa: str, pb: str, jogos: list,
                         dry_run: bool = False) -> tuple:
    """Insere o que a TM trouxe e ainda nao existe. Devolve (inseridos, vistos).
    dry_run=True: NAO grava — conta quantos ENTRARIAM (o mesmo NOT EXISTS
    rodado como SELECT), pra conferir o plano antes de mexer na tabela."""
    inseridos, vistos = 0, 0
    agora = datetime.now()
    for m in jogos:
        sf = m.get("scores_ft") or {}
        sh = m.get("scores_ht") or {}
        fa, fb = sf.get("score_ft_a"), sf.get("score_ft_b")
        date_raw = m.get("fixture_date") or m.get("date")
        if fa is None or fb is None or not date_raw:
            continue
        ts = _ts_naive(date_raw)
        if ts is None or ts > agora + timedelta(minutes=5):
            continue  # sem data legivel ou jogo no futuro -> nunca entra
        vistos += 1
        ja = (m.get("player_a") or pa).upper().strip()
        jb = (m.get("player_b") or pb).upper().strip()
        args = (_event_id(ja, jb, date_raw, fa, fb), ts, esporte, ja, jb,
                int(fa), int(fb),
                (int(sh["score_ht_a"]) if sh.get("score_ht_a") is not None else None),
                (int(sh["score_ht_b"]) if sh.get("score_ht_b") is not None else None),
                (str(m["id_player_a"]) if m.get("id_player_a") is not None else None),
                (str(m["id_player_b"]) if m.get("id_player_b") is not None else None))
        try:
            if dry_run:
                existe = await conn.fetchval(SQL_JA_EXISTE, *args[:7])
                if not existe:
                    inseridos += 1
            else:
                res = await conn.execute(SQL_INSERT, *args)
                if res and res.upper().endswith(" 1"):
                    inseridos += 1
        except Exception as e:
            logger.warning(f"[h2h_sync] insert {ja}x{jb} {date_raw}: {str(e)[:120]}")
    return inseridos, vistos


async def preencher(pool, analise: dict, job_id: str, limite: Optional[int] = None,
                    dry_run: bool = False):
    """Puxa da TM e insere o que falta, para os pares marcados na analise.
    dry_run=True: consulta a TM e diz quantos jogos ENTRARIAM, sem gravar."""
    job = JOBS[job_id]
    esporte = normalizar_esporte(analise.get("esporte"))
    cfg = CFG_ESPORTE[esporte]
    cred = _creds()

    alvo = [p for p in analise.get("pares", []) if p.get("precisa")]
    limite = int(limite or LIMITE_PARES_PADRAO)
    alvo = alvo[:limite]
    if not alvo:
        job.update(status="concluido", progresso=100, etapa="nada a preencher",
                   relatorio={"pares_processados": 0, "jogos_inseridos": 0,
                              "detalhe": []})
        return job["relatorio"]

    job.update(etapa="login na TipManager", progresso=2)
    token = await asyncio.to_thread(_login, cred)
    job.update(etapa="carregando players dos torneios", progresso=5)
    pmap = await asyncio.to_thread(_carregar_players, cred, cfg)

    detalhe, total_ins, total_limpos = [], 0, 0
    async with _LOCK_PREENCHER, pool.acquire() as conn:
        for i, par in enumerate(alvo, 1):
            pa, pb = par["jogador_a"], par["jogador_b"]
            job.update(etapa=f"{pa} x {pb} ({i}/{len(alvo)})",
                       progresso=5 + int(90 * i / len(alvo)))
            ids_a, ids_b = _ids_do_nick(pmap, pa), _ids_do_nick(pmap, pb)
            if not ids_a or not ids_b:
                detalhe.append({"par": f"{pa} x {pb}", "inseridos": 0,
                                "obs": "nick nao encontrado na TM"})
                continue
            try:
                data, token = await asyncio.to_thread(
                    _h2h_multi, cred, cfg, token, ids_a, ids_b)
            except Exception as e:
                detalhe.append({"par": f"{pa} x {pb}", "inseridos": 0,
                                "obs": f"erro TM: {str(e)[:80]}"})
                continue
            jogos = _jogos_de(data)
            if not jogos:
                detalhe.append({"par": f"{pa} x {pb}", "inseridos": 0,
                                "obs": "TM nao tem historico deste par"})
                continue
            ins, vistos = await _inserir_jogos(conn, esporte, pa, pb, jogos,
                                               dry_run=dry_run)
            total_ins += ins
            limpos = 0
            if not dry_run and vistos > 0:
                # o mesmo jogo em formato legado (bkp_/inc_) contaria DUAS vezes
                # no Ult.N — o fase2 ja resolvia assim. So depois de a TM ter
                # entregue historico do par.
                res = await conn.execute(SQL_LIMPAR_BACKUP, esporte, pa, pb)
                try:
                    limpos = int(str(res).split()[-1])
                except (ValueError, IndexError):
                    limpos = 0
                total_limpos += limpos
            detalhe.append({"par": f"{pa} x {pb}",
                            "inseridos" if not dry_run else "entrariam": ins,
                            "tm_trouxe": vistos, "backup_removido": limpos,
                            "obs": "ok"})
            await asyncio.sleep(_throttle["atual"])

    rel = {"pares_processados": len(alvo),
           "jogos_inseridos" if not dry_run else "jogos_que_entrariam": total_ins,
           "backups_removidos": total_limpos, "dry_run": dry_run,
           "detalhe": detalhe}
    job.update(status="concluido", progresso=100,
               etapa=(f"{total_ins} jogos inseridos" if not dry_run
                      else f"simulacao: {total_ins} entrariam"), relatorio=rel)
    return rel


# --------------------------------------------------- wrappers de background --

async def rodar_analise(pool, params: dict, job_id: str):
    try:
        await analisar(pool, params, job_id=job_id)
    except Exception as e:
        logger.exception("[h2h_sync] analise falhou")
        JOBS[job_id].update(status="erro", erro=str(e)[:300], etapa="falhou")


async def rodar_preenchimento(pool, analise: dict, job_id: str,
                              limite: Optional[int] = None,
                              dry_run: bool = False):
    try:
        await preencher(pool, analise, job_id, limite=limite, dry_run=dry_run)
    except Exception as e:
        logger.exception("[h2h_sync] preenchimento falhou")
        JOBS[job_id].update(status="erro", erro=str(e)[:300], etapa="falhou")
