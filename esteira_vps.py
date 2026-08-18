# -*- coding: utf-8 -*-
"""
===============================================================================
 ESTEIRA VPS v2 — 20/50 estrategias direto no MOTOR do backtest, sem clique
===============================================================================
 Roda NA VPS, DENTRO da pasta do tipmike_api. Nada de HTTP/token/upload:
   - importa o executar_backtest (o motor REAL, o mesmo do painel)
   - insere o job direto em backtest_jobs (mesmo INSERT do router avulso)
   - roda o job INLINE (um por vez — o cache v3 do parse serve todos)
   - le o resultado de apostas_detalhe no proprio banco (sem baixar xlsx)
   - calcula ap, G-R, WR, u, ROI, DD, m1/m2, 3d/7d (fim do DADO), vivo,
     queda_ponta — e as VARIACOES por eixo (chip/folga/linha/teto)
   - salva placar_esteira.xlsx (PLACAR / VARIACOES / LOG) + estado p/ retomada

 COMO USAR (na VPS):
   1. salve este arquivo na RAIZ do tipmike_api (junto do main.py)
   2. deixe na mesma pasta: estrategias.xlsx e o parquet de ticks
   3. (recomendado) aplique o workers/backtest_upload.py v3 e ligue o cache:
        BACKTEST_CACHE_N2=1  (o 1o job paga o parse; os outros ~0s)
   4. python esteira_vps.py
   5. abra placar_esteira.xlsx

 Retomada: Ctrl+C salva o que tem; rodar de novo pula o que ja concluiu.
===============================================================================
"""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import timedelta
from pathlib import Path

import pandas as pd
import numpy as np

# ============================== CONFIG =======================================
# v2.9: FONTE dos ticks — aceita .parquet OU .csv (o acervo da BetsAPI).
# Caminho completo funciona; se for so o nome, procura nesta pasta.
PARQUET = r"C:\Users\Administrator\PyCharmMiscProject\MikeBacktest\acervo_betsapi_H2H.csv"

# v2.9: RECORTE DE DIAS. Pega os ULTIMOS N dias do arquivo (contados a partir
# do tick mais recente que existe nele, nao da data de hoje). Use:
#   3, 7, 15, 30 ...  -> ultimos N dias
#   None  ou  0       -> TUDO, sem recorte
# Recortar acelera muito: o motor le so a fatia, nao o acervo inteiro.
DIAS = 30
PLANILHA_ESTRATEGIAS = "estrategias.xlsx"
# v2.8: aborta a rodada apos N jobs SEGUIDOS com 0 apostas. Sem isso, um
# codigo de mercado errado na planilha queima ~50s por job em silencio —
# aconteceu de verdade: 30 jobs perdidos por 'ou_ft' em vez de 'over_under_ft'.
# 0 desliga a trava.
MAX_ZERADOS_SEGUIDOS = 3
SAIDA = "placar_esteira.xlsx"
STATE = "esteira_state.json"

STAKE = 1.0
BANCA = 1000.0
REC_JANELAS = (3, 7)
RODAR_VARIACOES = True
TIMEOUT_JOB_S = 45 * 60      # watchdog: job travado nao trava a fila
HILL_CLIMB = True            # variacao que MELHOROU continua andando
HILL_MAX_PASSOS = 3          # ate N passos extras na mesma direcao
# v2.2: a regra antiga exigia unidades >= as da mae. Na rodada 1 isso
# BARROU o melhor achado do arquivo (linha+1: ROI +7/+8 pontos e DD MENOR,
# custando 4-6 unidades de 100). Agora tolera uma perda pequena de lucro
# total quando o ROI sobe — que e a regua sniper (ROI alto, DD baixo).
HILL_TOL_U = 0.92            # aceita ate 8% menos unidades que a mae
TOP_CARTEIRA = 8             # quantas maes entram na matriz de correlacao
USER_ID = None                            # id do teu usuario (ou None)
# Chip de WR usa o H2H do BANCO, e a query filtra sport = $2 (e a de ticks
# tambem por bookmaker). Se a planilha nao trouxer casa/esporte, o snapshot
# vai com None -> "WHERE sport = NULL" nao casa nada -> TODA config com chip
# devolve 0 apostas (mecanica pura passa liso porque nem consulta H2H).
# Estes defaults valem quando a coluna da planilha estiver vazia.
CASA_PADRAO = "bet365"                    # como esta gravado no banco
ESPORTE_PADRAO = "nba2k"                  # UI -> banco: E-Basketball

VARIACOES = {
    "chip_wr_min": [-5.0, +5.0],          # pontos de %
    "chip_wr_max": [-5.0, +5.0],          # p/ chip em BANDA (mercados de total)
    "folga_min":   [-1.0, +1.0],
    "linha_min":   [-1.0, +1.0],
    "linha_max":   [-1.0, +1.0],          # o corte que decide em total de pontos
    "teto":        [-2, +2],
}
# A linha do handicap anda de 0,5 em 0,5 (1 a 35); a de total de pontos anda na
# casa das dezenas (66 a 161). Passo de 1 ali nao mexe em nada — entao o passo
# dos eixos de linha ESCALA com o valor.
def _passo_linha(base):
    return 5.0 if (base is not None and abs(base) >= 40) else 1.0
# =============================================================================

# --- imports do proprio tipmike_api (por isso o script mora na raiz do repo) --
try:
    from database import init_pool, close_pool, get_pool
    from workers.backtest_runner import executar_backtest
    from workers.backtest_upload import UPLOAD_DIR
except ImportError as e:
    print("ERRO: rode este arquivo DENTRO da pasta do tipmike_api "
          f"(import falhou: {e})")
    sys.exit(1)


# ------------------------------ helpers --------------------------------------
def _num(v):
    try:
        f = float(str(v).replace(",", "."))
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _pct(v):
    f = _num(v)
    if f is None:
        return None
    return f * 100.0 if f <= 1.0 else f


def _txt(v, padrao=""):
    s = str(v).strip()
    return padrao if s.lower() in ("", "nan", "none", "-") else s


def _janela_api(v) -> str:
    s = str(v or "all").strip().lower()
    if s in ("", "all", "todas", "-", "nan", "none"):
        return "all"
    dig = "".join(ch for ch in s if ch.isdigit())
    return f"last_{dig}" if dig else "all"


def montar_snapshot(e: dict) -> dict:
    """Mesmo formato do _montar_snapshot_avulso do router — o worker entende."""
    filtros: dict = {"evitarLinhasSeq": bool(int(_num(e.get("evitar_linhas_seq")) or 0))}

    hist = []
    for pref in ("chip_", "chip2_"):
        wr_min = _pct(e.get(pref + "wr_min"))
        wr_max = _pct(e.get(pref + "wr_max"))
        if wr_min is None and wr_max is None:
            continue
        _h = {
            "base": "match", "tipo": "all", "versao": "all",
            "janela": _janela_api(e.get(pref + "janela")),
            "prob": [wr_min if wr_min is not None else 0.0,
                     wr_max if wr_max is not None else 100.0],
            "minPartidas": int(_num(e.get(pref + "conf")) or 0),
        }
        # v2.5: TETO de confrontos (runner v15). Colunas `chip_conf_max` e
        # `chip2_conf_max` na planilha. Vazio = sem teto.
        _cmax = _num(e.get(pref + "conf_max"))
        if _cmax is not None and _cmax > 0:
            _h["maxPartidas"] = int(_cmax)
        hist.append(_h)
    if hist:
        filtros["filtrosHistAdicionados"] = hist

    fmin, fmax = _num(e.get("folga_min")), _num(e.get("folga_max"))
    if fmin is not None or fmax is not None:
        filtros["folgaAtivo"] = True
        if fmin is not None:
            filtros["folgaMin"] = fmin
        if fmax is not None:
            filtros["folgaMax"] = fmax

    # v3.1: ATROPELO (runner v16) — colunas atropelo_min / atropelo_max /
    # atropelo_margem (default 15) / atropelo_min_jogos (default 6).
    # Vazias = filtro desligado, snapshot identico ao de antes.
    amin, amax = _num(e.get("atropelo_min")), _num(e.get("atropelo_max"))
    if amin is not None or amax is not None:
        filtros["atropeloAtivo"] = True
        if amin is not None:
            filtros["atropeloMin"] = amin
        if amax is not None:
            filtros["atropeloMax"] = amax
        _amg = _num(e.get("atropelo_margem"))
        if _amg:
            filtros["atropeloMargem"] = _amg
        _amj = _num(e.get("atropelo_min_jogos"))
        if _amj:
            filtros["atropeloMinJogos"] = int(_amj)

    # v3.2: TOT_ENV (runner v17) — soma do placar no envio. Colunas
    # `tot_env_min` / `tot_env_max`. NAO confundir com momento_min/max, que no
    # motor e' o ESTAGIO do jogo (1Q/1T/3Q). Vazias = desligado.
    tmin, tmax = _num(e.get("tot_env_min")), _num(e.get("tot_env_max"))
    if tmin is not None or tmax is not None:
        filtros["totEnvAtivo"] = True
        if tmin is not None:
            filtros["totEnvMin"] = tmin
        if tmax is not None:
            filtros["totEnvMax"] = tmax

    mmin, mmax = _num(e.get("momento_min")), _num(e.get("momento_max"))
    if mmin is not None or mmax is not None:
        filtros["momentoAtivo"] = True
        if mmin is not None:
            filtros["momentoMin"] = mmin
        if mmax is not None:
            filtros["momentoMax"] = mmax

    lado = _txt(e.get("lado"), "ambos").lower()
    if lado in ("over", "under"):
        filtros["lados"] = [lado]
        filtros["inner"] = [lado.capitalize()]

    teto = _num(e.get("teto"))
    return {
        "nome": f"Esteira: {e.get('nome')}",
        "casa": _txt(e.get("casa")) or (CASA_PADRAO or None),
        "esporte": _txt(e.get("esporte")) or (ESPORTE_PADRAO or None),
        "mercado": _txt(e.get("mercado"), "ah_ft").lower(),
        "linha_min": _num(e.get("linha_min")),
        "linha_max": _num(e.get("linha_max")),
        "odd_min": _num(e.get("odd_min")),
        "odd_max": _num(e.get("odd_max")),
        "torneios": [], "torneios_excluir": [],
        "whitelist_pares": [], "blacklist_pares": [], "whitelist_cenarios": [],
        "max_apostas_partida": int(teto) if teto else None,
        "filtros": filtros,
    }


def _fmt(v, suf=''):
    if v is None:
        return ''
    f = float(v)
    return (f'{int(f)}{suf}' if f == int(f) else f'{f:g}{suf}')


def resumo_config(snap: dict) -> dict:
    """v2.3: devolve a config RESOLVIDA (a que o motor recebeu) em colunas.
    Antes o placar so trazia o nome — e numa variacao ('[folga_min+1]') o
    valor real ficava implicito. Agora cada linha se explica sozinha."""
    fl = snap.get('filtros', {}) or {}
    hist = fl.get('filtrosHistAdicionados', []) or []

    def chip(h):
        if not h:
            return '', ''
        jan = str(h.get('janela', 'all'))
        rot = 'Todas' if jan == 'all' else 'Últ.' + jan.split('_')[-1]
        lo, hi = (h.get('prob') or [0, 100])[:2]
        if lo and hi and hi < 100:
            txt = f'{rot} {_fmt(lo)}~{_fmt(hi)}%'
        elif hi and hi < 100:
            txt = f'{rot}≤{_fmt(hi)}%'
        else:
            txt = f'{rot}≥{_fmt(lo)}%'
        return txt, (h.get('minPartidas') or '')

    c1, cf1 = chip(hist[0] if len(hist) > 0 else None)
    c2, cf2 = chip(hist[1] if len(hist) > 1 else None)
    cx1 = (hist[0].get('maxPartidas') if len(hist) > 0 else None) or ''
    cx2 = (hist[1].get('maxPartidas') if len(hist) > 1 else None) or ''
    teto = snap.get('max_apostas_partida')
    fmin = fl.get('folgaMin') if fl.get('folgaAtivo') else None
    fmax = fl.get('folgaMax') if fl.get('folgaAtivo') else None
    partes = [p for p in (
        c1, (f'conf≥{_fmt(cf1)}' if cf1 else ''),
        (f'conf≤{_fmt(cx1)}' if cx1 else ''), c2,
        (f'conf2≤{_fmt(cx2)}' if cx2 else ''),
        (f'L≥{_fmt(snap.get("linha_min"))}' if snap.get('linha_min') else ''),
        (f'L≤{_fmt(snap.get("linha_max"))}' if snap.get('linha_max') else ''),
        (f'odd≥{_fmt(snap.get("odd_min"))}' if snap.get('odd_min') else ''),
        (f'folga≥{_fmt(fmin)}' if fmin is not None else ''),
        (f'folga≤{_fmt(fmax)}' if fmax is not None else ''),
        (f'tot_env≥{_fmt(fl.get("totEnvMin"))}'
         if fl.get('totEnvAtivo') and fl.get('totEnvMin') is not None else ''),
        (f'tot_env≤{_fmt(fl.get("totEnvMax"))}'
         if fl.get('totEnvAtivo') and fl.get('totEnvMax') is not None else ''),
        (f'atropelo≥{_fmt(fl.get("atropeloMin"))}%'
         if fl.get('atropeloAtivo') and fl.get('atropeloMin') is not None else ''),
        (f'atropelo≤{_fmt(fl.get("atropeloMax"))}%'
         if fl.get('atropeloAtivo') and fl.get('atropeloMax') is not None else ''),
        (f'teto {teto}' if teto else 'sem teto')) if p]
    return {
        'config': ' · '.join(partes),
        'chip1': c1, 'chip1_conf': cf1, 'chip1_conf_max': cx1,
        'chip2': c2, 'chip2_conf': cf2, 'chip2_conf_max': cx2,
        'linha_min': snap.get('linha_min'), 'linha_max': snap.get('linha_max'),
        'odd_min': snap.get('odd_min'), 'odd_max': snap.get('odd_max'),
        'folga_min': fmin, 'folga_max': fmax, 'teto': teto,
        'lado': (fl.get('lados') or ['ambos'])[0],
    }


def _hash_motor() -> str:
    """v2.7: impressao digital do MOTOR (workers/backtest_runner.py).

    Por que existe: o state guardava resultado por config + parquet. Trocar o
    runner nao mudava a assinatura, entao a esteira devolvia numero do motor
    ANTIGO como se fosse do novo — silenciosamente. Isso chegou a pular um
    teste de aceite. Agora o proprio arquivo do runner entra no hash: mexeu no
    motor, tudo roda de novo, sem ninguem precisar lembrar de apagar o state.
    """
    for cam in (Path("workers/backtest_runner.py"),
                Path(__file__).resolve().parent / "workers" / "backtest_runner.py"):
        try:
            if cam.is_file():
                return hashlib.sha1(cam.read_bytes()).hexdigest()[:10]
        except Exception:
            pass
    return "motor-desconhecido"


_MOTOR_HASH = None


def assinatura(snap: dict, base: str = '') -> str:
    # v2.3: o NOME fica FORA do hash. Antes ele entrava, e duas linhas com a
    # mesma config sob nomes diferentes (ex.: a mae 'c_todas75_L105_f35' e a
    # variacao 'c_todas70_L105_f35 [chip_wr_min+5]', que sao a MESMA coisa)
    # geravam assinaturas distintas — o state nao reconhecia e o motor rodava
    # o job de novo. Na rodada de 04/ago isso queimou 8 jobs (~5 min) repetindo
    # configs ja medidas.
    # v2.4: a BASE (hash do parquet) entra no hash. Sem isso, trocar de
    # parquet fazia o state devolver o numero da base ANTIGA como se fosse
    # da nova — silenciosamente. Agora base nova = tudo roda de novo.
    global _MOTOR_HASH
    if _MOTOR_HASH is None:
        _MOTOR_HASH = _hash_motor()
    limpo = {k: v for k, v in snap.items() if k != 'nome'}
    limpo['__base__'] = base
    limpo['__motor__'] = _MOTOR_HASH
    return hashlib.sha1(json.dumps(limpo, sort_keys=True, default=str)
                        .encode()).hexdigest()[:14]


def gerar_variacoes(e: dict) -> list:
    out = []
    for campo, deltas in VARIACOES.items():
        base = _num(e.get(campo))
        if base is None:
            continue
        for dlt in deltas:
            v = dict(e)
            if campo in ("chip_wr_min", "chip_wr_max"):
                b = _pct(base)
                novo = min(100.0, max(0.0, b + dlt))
                if novo == b:
                    continue
            else:
                _d = dlt * _passo_linha(base) if campo.startswith("linha") else dlt
                novo = base + _d
                if campo == "teto":
                    novo = int(novo)
                    if novo < 1:
                        continue
            v[campo] = novo
            v["nome"] = f"{e.get('nome')} [{campo}{'+' if dlt > 0 else ''}{dlt:g}]"
            v["_mae"] = e.get("nome")
            v["_eixo"] = f"{campo}{'+' if dlt > 0 else ''}{dlt:g}"
            out.append(v)
    return out


def _df_do_detalhe(detalhe):
    if isinstance(detalhe, str):
        detalhe = json.loads(detalhe or "[]")
    d = pd.DataFrame(detalhe or [])
    if not len(d):
        return d
    d["u"] = pd.to_numeric(d.get("lucro_unidades"), errors="coerce")
    res = d.get("resultado").astype(str).str.lower()
    d = d[res.isin(["green", "red"]) & d["u"].notna()].copy()
    d["green"] = d["resultado"].astype(str).str.lower().eq("green")
    d["ts"] = pd.to_datetime(d["ts"].astype(str).str.replace("Z", ""),
                             errors="coerce", format="mixed")
    d = d[d["ts"].notna()].sort_values("ts")
    par = (d.get("jogador_a").astype(str).str.upper() + "|"
           + d.get("jogador_b").astype(str).str.upper())
    gap = d.groupby(par)["ts"].diff().dt.total_seconds().div(60).fillna(999)
    d["jogo"] = ((gap > 45) | (gap == 999)).groupby(par).cumsum().astype(str) + par
    return d


def lucro_por_jogo(detalhe):
    """Serie jogo -> lucro (pra correlacao de CARTEIRA entre estrategias)."""
    d = _df_do_detalhe(detalhe)
    if not len(d):
        return pd.Series(dtype=float)
    return d.groupby("jogo")["u"].sum()


def _proximo_passo(v: dict):
    """Um passo a mais na MESMA direcao do eixo que melhorou (hill-climb)."""
    eixo = str(v.get("_eixo", ""))
    for campo, deltas in VARIACOES.items():
        for dlt in deltas:
            if eixo == f"{campo}{'+' if dlt > 0 else ''}{dlt:g}":
                n = dict(v)
                base = _num(v.get(campo))
                if base is None:
                    return None
                if campo in ("chip_wr_min", "chip_wr_max"):
                    b = _pct(base)
                    novo = min(100.0, max(0.0, b + dlt))
                    if novo == b:
                        return None
                else:
                    _d = dlt * _passo_linha(base) if campo.startswith("linha") else dlt
                    novo = base + _d
                    if campo == "teto":
                        novo = int(novo)
                        if novo < 1:
                            return None
                n[campo] = novo
                n["_passo"] = int(v.get("_passo", 1)) + 1
                n["nome"] = (f"{v.get('_mae')} [{campo}"
                             f"{'+' if dlt > 0 else ''}{dlt * n['_passo']:g}]")
                return n
    return None


def metricas_do_detalhe(detalhe: list) -> dict:
    d = _df_do_detalhe(detalhe)
    if not len(d):
        return {"apostas": 0}
    n, G = len(d), int(d["green"].sum())
    if not n:
        return {"apostas": 0}
    cum = d["u"].cumsum()
    dd = float((cum.cummax() - cum).max())
    fim = d["ts"].max().normalize() + timedelta(days=1)
    meio = d["ts"].min() + (d["ts"].max() - d["ts"].min()) / 2
    out = {
        "apostas": n, "G-R": f"{G}-{n - G}",
        "WR": round(G / n * 100, 1),
        "unidades": round(float(d["u"].sum()), 2),
        "ROI": round(float(d["u"].sum()) / n * 100, 2),
        "DD": round(dd, 1),
        "lucro_dd": (round(float(d["u"].sum()) / dd, 2) if dd > 0 else None),
        "roi_m1": round(float(d[d.ts < meio]["u"].sum())
                        / max(len(d[d.ts < meio]), 1) * 100, 2),
        "roi_m2": round(float(d[d.ts >= meio]["u"].sum())
                        / max(len(d[d.ts >= meio]), 1) * 100, 2),
        "de": str(d["ts"].min())[:16], "ate": str(d["ts"].max())[:16],
    }
    for w in REC_JANELAS:
        f = d[d.ts >= fim - timedelta(days=w)]
        Gw = int(f["green"].sum())
        out[f"ap_{w}d"] = len(f)
        out[f"GR_{w}d"] = f"{Gw}-{len(f) - Gw}"
        out[f"u_{w}d"] = round(float(f["u"].sum()), 2)
        out[f"roi_{w}d"] = (round(float(f["u"].sum()) / len(f) * 100, 2)
                            if len(f) else None)
    out["vivo"] = (1 if all(
        (out.get(f"ap_{w}d", 0) < 10) or (out.get(f"u_{w}d", 0) > 0)
        for w in REC_JANELAS) and out.get(f"ap_{REC_JANELAS[-1]}d", 0) >= 10
        else 0)
    w0 = REC_JANELAS[0]
    out["queda_ponta"] = (round(out[f"roi_{w0}d"] - out["roi_m2"], 2)
                          if out.get(f"roi_{w0}d") is not None else None)
    # z por JOGO (nao por aposta): lucro medio/desvio na unidade certa —
    # a mesma regua do varredor. z>=2 = dificil ser sorte.
    pj = d.groupby("jogo")["u"].sum()
    if len(pj) >= 5 and float(pj.std(ddof=1) or 0) > 0:
        out["jogos"] = int(len(pj))
        out["z_jogo"] = round(float(pj.mean() / (pj.std(ddof=1)
                              / np.sqrt(len(pj)))), 2)
    else:
        out["jogos"] = int(len(pj))
        out["z_jogo"] = None
    # CEGO: ultimos ~30%% das apostas por tempo (holdout de sequencia)
    corte = int(n * 0.7)
    tr, cg = d.iloc[:corte], d.iloc[corte:]
    if len(cg) >= 10:
        out["roi_treino"] = round(float(tr["u"].sum()) / max(len(tr), 1) * 100, 2)
        out["roi_cego"] = round(float(cg["u"].sum()) / len(cg) * 100, 2)
        out["ap_cego"] = int(len(cg))
        out["desvio_cego"] = round(out["roi_cego"] - out["roi_treino"], 2)
    else:
        out["roi_cego"] = None
    return out


def carregar_estado() -> dict:
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"parquet": {}, "jobs": {}}


def salvar_estado(st: dict):
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"[esteira] AVISO: state nao salvo: {e}")


def _coluna_ts(df) -> str:
    """Acha a coluna de tempo do arquivo (o acervo da BetsAPI e o parquet dos
    coletores nao usam o mesmo nome). BLINDADO: se nao achar, devolve ''."""
    for c in ("ts", "timestamp", "data_hora", "datahora", "time", "hora_ts",
              "created_at", "dt"):
        if c in df.columns:
            return c
    for c in df.columns:
        if "ts" == str(c).lower() or "time" in str(c).lower():
            return c
    return ""


def preparar_upload_local(st: dict) -> str:
    """v2.9: le a FONTE (.parquet ou .csv), aplica o recorte de DIAS e grava
    um parquet em UPLOAD_DIR — o caminho e o upload_id que o worker entende.

    A assinatura do cache inclui o recorte, entao trocar DIAS de 30 pra 7
    gera outro arquivo e NAO reaproveita o anterior por engano.
    """
    origem = Path(PARQUET)
    if not origem.is_file():
        alt = Path(__file__).resolve().parent / origem.name
        if alt.is_file():
            origem = alt
    bruto = hashlib.sha1(origem.read_bytes()).hexdigest()[:16]
    dias = int(DIAS) if DIAS else 0
    h = f"{bruto}_d{dias or 'tudo'}"
    if st["parquet"].get("hash") == h and os.path.exists(st["parquet"].get("path", "")):
        print(f"[esteira] base ja preparada ({dias or 'tudo'} dias) — reusando")
        return st["parquet"]["path"]

    Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    destino = str(Path(UPLOAD_DIR) / f"esteira_{h}.parquet")

    eh_csv = origem.suffix.lower() in (".csv", ".txt")
    if not eh_csv and not dias:
        # parquet inteiro: caminho antigo, so copia (rapido)
        shutil.copyfile(origem, destino)
        st["parquet"] = {"hash": h, "path": destino}
        salvar_estado(st)
        print(f"[esteira] base preparada em {destino} (parquet inteiro)")
        return destino

    print(f"[esteira] lendo {origem.name} ...")
    try:
        df = (pd.read_csv(origem, low_memory=False) if eh_csv
              else pd.read_parquet(origem))
    except UnicodeDecodeError:
        df = pd.read_csv(origem, low_memory=False, encoding="latin-1")
    n0 = len(df)

    if dias:
        col = _coluna_ts(df)
        if not col:
            print(f"[esteira] AVISO: nao achei coluna de tempo em "
                  f"{list(df.columns)[:8]}... — seguindo SEM recorte de dias")
        else:
            ts = pd.to_datetime(df[col], errors="coerce")
            fim = ts.max()
            if pd.isna(fim):
                print("[esteira] AVISO: coluna de tempo sem data valida — "
                      "seguindo SEM recorte")
            else:
                ini = fim - pd.Timedelta(days=dias)
                df = df[ts >= ini].copy()
                # imprime o que o arquivo TEM, nao o que foi pedido: pedir 30
                # dias num arquivo de 15 mostrava "07/07 a 06/08" e dava a
                # impressao de cobertura que nao existe.
                _tr = ts[ts >= ini]
                _real = (_tr.max() - _tr.min()).days + 1
                print(f"[esteira] recorte pedido: {dias} dias | REAL no arquivo: "
                      f"{_real} dias ({_tr.min():%d/%m} a {_tr.max():%d/%m}) "
                      f"-> {len(df):,} de {n0:,} linhas")
                if _real < dias:
                    print(f"[esteira] AVISO: o arquivo so tem {_real} dias — "
                          f"pedir {dias} nao cortou nada")
                if df.empty:
                    raise ValueError(
                        f"o recorte de {dias} dias nao deixou nenhuma linha — "
                        f"confira DIAS ou a coluna de tempo ({col})")
    if not dias:
        _c = _coluna_ts(df)
        if _c:
            _t = pd.to_datetime(df[_c], errors="coerce")
            print(f"[esteira] sem recorte: {n0:,} linhas | "
                  f"{(_t.max()-_t.min()).days+1} dias "
                  f"({_t.min():%d/%m} a {_t.max():%d/%m})")
        else:
            print(f"[esteira] sem recorte: {n0:,} linhas (arquivo inteiro)")

    df.to_parquet(destino, index=False)
    st["parquet"] = {"hash": h, "path": destino}
    salvar_estado(st)
    print(f"[esteira] base preparada em {destino}")
    return destino


# ------------------------------ ciclo ----------------------------------------
async def rodar_um(conn_pool, snap: dict, upload_id: str):
    async with conn_pool.acquire() as conn:
        job_id = await conn.fetchval(
            """
            INSERT INTO backtest_jobs
                (bot_id, data_inicio, data_fim, stake_modo, stake_valor,
                 banca_inicial, bot_snapshot, status, progresso, upload_id, user_id)
            VALUES (NULL, NULL, NULL, 'fixo', $1, $2, $3::jsonb,
                    'pendente', 0, $4, $5)
            RETURNING id
            """,
            STAKE, BANCA, json.dumps(snap, default=str), upload_id, USER_ID,
        )
    try:
        await asyncio.wait_for(executar_backtest(job_id),
                               timeout=TIMEOUT_JOB_S)   # o MOTOR REAL, inline
    except asyncio.TimeoutError:
        raise RuntimeError(f"job {job_id}: passou de {TIMEOUT_JOB_S//60}min "
                           f"(watchdog) — fila segue no proximo")
    async with conn_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, erro, apostas_detalhe FROM backtest_jobs WHERE id=$1",
            job_id)
    if row is None:
        raise RuntimeError(f"job {job_id} sumiu do banco")
    if str(row["status"]).lower() == "erro":
        raise RuntimeError(f"job {job_id} ERRO no motor: {row['erro']}")
    det = row["apostas_detalhe"]
    return job_id, metricas_do_detalhe(det), lucro_por_jogo(det)


async def main():
    print("=" * 78)
    print(" ESTEIRA VPS v2 — motor real importado, jobs em sequencia")
    print("=" * 78)
    for arq in (PARQUET, PLANILHA_ESTRATEGIAS):
        if not os.path.exists(arq) and not os.path.exists(
                str(Path(__file__).resolve().parent / Path(arq).name)):
            print(f"ERRO: nao achei {arq}")
            return
    print(f"[esteira] fonte: {Path(PARQUET).name} | recorte: "
          f"{(str(DIAS) + ' dias') if DIAS else 'TUDO'}")

    zerados_seguidos = [0]
    est = pd.read_excel(PLANILHA_ESTRATEGIAS)
    est.columns = [str(c).strip().lower() for c in est.columns]
    if "nome" not in est.columns:
        est["nome"] = [f"estrategia_{i+1}" for i in range(len(est))]
    fila = [dict(r) for _, r in est.iterrows()]
    if RODAR_VARIACOES:
        extra = []
        for e in fila:
            if int(_num(e.get("variar")) or 0) == 1:
                extra += gerar_variacoes(e)
        fila += extra
        print(f"[esteira] {len(est)} estrategias + {len(extra)} variacoes")

    st = carregar_estado()
    await init_pool()
    pool = get_pool()
    upload_id = preparar_upload_local(st)

    placar, log = [], []
    por_jogo: dict = {}
    t_ini = time.time()
    try:
        i = 0
        while fila:
            e = fila.pop(0)
            i += 1
            nome = str(e.get("nome"))
            try:
                snap = montar_snapshot(e)
            except Exception as ex:
                log.append({"estrategia": nome, "evento": f"snapshot: {ex}"})
                continue
            ass = assinatura(snap, st['parquet'].get('hash', ''))
            reg = st["jobs"].get(ass, {})
            try:
                if reg.get("metricas"):
                    m = reg["metricas"]
                    igual = (f" (identica a '{reg['nome']}')"
                             if reg.get("nome") and reg["nome"] != nome else "")
                    print(f"[{i}/{len(fila)}] {nome}: ja rodada "
                          f"(job {reg.get('job_id')}){igual} — reaproveitando")
                else:
                    t0 = time.time()
                    print(f"[{i}/{len(fila)}] {nome}: rodando no motor...")
                    job_id, m, pj = await rodar_um(pool, snap, upload_id)
                    por_jogo[nome] = pj
                    st["jobs"][ass] = {"job_id": job_id, "nome": nome,
                                       "metricas": m}
                    salvar_estado(st)
                    if not m.get("apostas"):
                        zerados_seguidos[0] += 1
                        tem_chip = bool(snap["filtros"].get("filtrosHistAdicionados"))
                        print(f"    job {job_id} em {time.time()-t0:.0f}s -> "
                              f"0 apostas" + (
                                  "  <-- config COM CHIP e H2H vazio: confira "
                                  "casa/esporte (CASA_PADRAO/ESPORTE_PADRAO) "
                                  "contra o que o banco tem" if tem_chip else
                                  "  (filtro cortou tudo)"))
                        if (MAX_ZERADOS_SEGUIDOS
                                and zerados_seguidos[0] >= MAX_ZERADOS_SEGUIDOS):
                            print("\n" + "=" * 78)
                            print(f" ABORTANDO: {zerados_seguidos[0]} jobs "
                                  "SEGUIDOS com 0 apostas.")
                            print(" Quase sempre e um destes tres, nesta ordem:")
                            print(f"   1. coluna `mercado` da planilha "
                                  f"(este job usou: {snap.get('mercado')!r})")
                            print(f"   2. PARQUET nao tem ticks desse mercado "
                                  f"(atual: {PARQUET!r})")
                            print(f"   3. casa/esporte "
                                  f"({snap.get('casa')!r}/{snap.get('esporte')!r})")
                            print(" Confira, corrija e rode de novo — o que ja "
                                  "rodou fica no state.")
                            print("=" * 78)
                            break
                    else:
                        zerados_seguidos[0] = 0
                        print(f"    job {job_id} em {time.time()-t0:.0f}s -> "
                              f"{m.get('apostas')} ap | {m.get('G-R')} | "
                              f"WR {m.get('WR')} | ROI {m.get('ROI')} | "
                              f"3d {m.get('roi_3d')} | 7d {m.get('roi_7d')}")
                placar.append({"estrategia": nome, **resumo_config(snap),
                               "mae": e.get("_mae", ""),
                               "eixo": e.get("_eixo", ""),
                               "job": st["jobs"][ass].get("job_id"), **m})
                # HILL-CLIMB: variacao que MELHOROU a mae anda mais um passo
                if (HILL_CLIMB and e.get("_mae") and m.get("apostas", 0) > 0
                        and int(e.get("_passo", 1)) < HILL_MAX_PASSOS):
                    mae_m = next((p for p in placar
                                  if p["estrategia"] == e.get("_mae")
                                  and not p.get("mae")), None)
                    u_mae = mae_m.get("unidades") if mae_m else None
                    piso_u = (u_mae * HILL_TOL_U if isinstance(u_mae, (int, float))
                              and u_mae > 0 else -9)
                    if (mae_m and m.get("ROI") is not None
                            and m["ROI"] > (mae_m.get("ROI") or -9)
                            and m.get("unidades", -9) >= piso_u):
                        prox = _proximo_passo(e)
                        if prox is not None:
                            print(f"    hill-climb: {prox['nome']} entra na fila")
                            fila.insert(0, prox)
            except KeyboardInterrupt:
                raise
            except Exception as ex:
                log.append({"estrategia": nome, "evento": str(ex)})
                print(f"    ERRO em {nome}: {ex}")
    except KeyboardInterrupt:
        print("\n[esteira] Ctrl+C — salvando o parcial...")
    finally:
        try:
            await close_pool()
        except Exception:
            pass

    if not placar and not log:
        print("[esteira] nada a salvar")
        return
    P = pd.DataFrame(placar)
    variacoes = pd.DataFrame()
    if len(P):
        maes = P[P["mae"].astype(str) == ""].copy()
        w0 = REC_JANELAS[0]
        maes = maes.sort_values([f"roi_{w0}d", "ROI"],
                                ascending=False, na_position="last")
        variacoes = P[P["mae"].astype(str) != ""].copy()
        if len(variacoes):
            ref = maes.set_index("estrategia")
            def _delta(r, col):
                try:
                    return round(r[col] - ref.loc[r["mae"], col], 2)
                except Exception:
                    return None
            for col, novo in (("ROI", "dROI"), ("apostas", "dAp"),
                              ("unidades", "dU")):
                variacoes[novo] = variacoes.apply(
                    lambda r, c=col: _delta(r, c), axis=1)
            # retencao de lucro: quanto do lucro da mae a variacao manteve
            u_mae_col = variacoes.apply(
                lambda r: (ref.loc[r["mae"], "unidades"]
                           if r["mae"] in ref.index else np.nan), axis=1)
            variacoes["ret_u"] = (variacoes["unidades"] / u_mae_col).round(3)
            variacoes["veredito"] = np.where(
                (variacoes["dROI"].fillna(-9) > 0)
                & (variacoes["ret_u"].fillna(0) >= HILL_TOL_U),
                "MELHOROU (roi+ mantendo lucro)",
                np.where(variacoes["dROI"].fillna(-9) > 0,
                         "roi+ mas custa lucro", "-"))
        # EVOLUCAO: compara com a ultima rodada registrada no state
        hist_ant = {h["estrategia"]: h for h in st.get("historico", [])}
        evolucao = []
        for _, r in maes.iterrows():
            ant = hist_ant.get(r["estrategia"])
            if ant:
                evolucao.append({
                    "estrategia": r["estrategia"],
                    "roi_3d_antes": ant.get("roi_3d"), "roi_3d_agora": r.get("roi_3d"),
                    "d_roi_3d": (round(r["roi_3d"] - ant["roi_3d"], 2)
                                 if pd.notna(r.get("roi_3d")) and ant.get("roi_3d") is not None else None),
                    "roi_antes": ant.get("ROI"), "roi_agora": r.get("ROI"),
                    "tendencia": ("AQUECENDO" if (ant.get("roi_3d") is not None
                                  and pd.notna(r.get("roi_3d"))
                                  and r["roi_3d"] > ant["roi_3d"]) else "esfriando"),
                })
        st["historico"] = [{"estrategia": r["estrategia"], "ROI": r.get("ROI"),
                            "roi_3d": (None if pd.isna(r.get("roi_3d")) else r.get("roi_3d")),
                            "roi_7d": (None if pd.isna(r.get("roi_7d")) else r.get("roi_7d"))}
                           for _, r in maes.iterrows()]
        salvar_estado(st)
        # CARTEIRA: correlacao de lucro POR JOGO entre as top maes desta rodada
        cart = None
        tops = [n for n in maes["estrategia"].head(TOP_CARTEIRA) if n in por_jogo]
        if len(tops) >= 2:
            M = pd.DataFrame({n: por_jogo[n] for n in tops}).fillna(0.0)
            cart = M.corr().round(2)
        with pd.ExcelWriter(SAIDA) as w:
            maes.to_excel(w, sheet_name="PLACAR", index=False)
            if len(variacoes):
                variacoes.sort_values("dROI", ascending=False).to_excel(
                    w, sheet_name="VARIACOES", index=False)
            if evolucao:
                pd.DataFrame(evolucao).to_excel(w, sheet_name="EVOLUCAO", index=False)
            if cart is not None:
                cart.to_excel(w, sheet_name="CARTEIRA")
            if log:
                pd.DataFrame(log).to_excel(w, sheet_name="LOG", index=False)
        print(f"\n[esteira] {len(maes)} estrategias + {len(variacoes)} "
              f"variacoes em {SAIDA} ({(time.time()-t_ini)/60:.0f} min)")
    elif log:
        pd.DataFrame(log).to_excel(SAIDA.replace(".xlsx", "_log.xlsx"),
                                   index=False)


def _ler_argumentos():
    """v3.0: linha de comando manda mais que o CONFIG do topo.

        python esteira_vps.py --30            (atalho: ultimos 30 dias)
        python esteira_vps.py --7
        python esteira_vps.py --tudo          (sem recorte)
        python esteira_vps.py --dias 15
        python esteira_vps.py --dias tudo --fonte outro_acervo.csv
        python esteira_vps.py --planilha estrategias_over.xlsx --saida over.xlsx

    Sem argumento nenhum, vale o que esta escrito no CONFIG (DIAS/PARQUET/...).
    """
    global DIAS, PARQUET, PLANILHA_ESTRATEGIAS, SAIDA, STATE, MAX_ZERADOS_SEGUIDOS

    argv = sys.argv[1:]
    # atalhos numericos: --30 --15 --7 --3 --tudo (viram --dias X)
    normal = []
    for a in argv:
        s = a.lstrip('-')
        if a.startswith('--') and (s.isdigit() or s.lower() in ('tudo', 'all')):
            normal += ['--dias', s]
        else:
            normal.append(a)

    ap = argparse.ArgumentParser(
        prog='esteira_vps.py',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='Roda a planilha de estrategias no motor real do tipmike.',
        epilog='atalhos: --30  --15  --7  --3  --tudo')
    ap.add_argument('--dias', '-d', default=None,
                    help='ultimos N dias da fonte, ou "tudo" pra nao recortar. '
                         'Conta do tick MAIS RECENTE do arquivo, nao de hoje')
    ap.add_argument('--fonte', '-f', default=None,
                    help='csv ou parquet com os ticks (default: o do CONFIG)')
    ap.add_argument('--planilha', '-p', default=None, help='xlsx de estrategias')
    ap.add_argument('--saida', '-o', default=None, help='xlsx do placar')
    ap.add_argument('--state', default=None, help='json de estado (cache de jobs)')
    ap.add_argument('--sem-trava', action='store_true',
                    help='nao aborta apos jobs seguidos com 0 apostas')
    a = ap.parse_args(normal)

    if a.dias is not None:
        d = str(a.dias).strip().lower()
        if d in ('tudo', 'all', '0'):
            DIAS = None
        else:
            try:
                DIAS = int(d)
                if DIAS <= 0:
                    DIAS = None
            except ValueError:
                print(f"ERRO: --dias {a.dias!r} nao e numero nem 'tudo'")
                sys.exit(2)
    if a.fonte:
        PARQUET = a.fonte
    if a.planilha:
        PLANILHA_ESTRATEGIAS = a.planilha
    if a.saida:
        SAIDA = a.saida
    if a.state:
        STATE = a.state
    if a.sem_trava:
        MAX_ZERADOS_SEGUIDOS = 0


if __name__ == "__main__":
    _ler_argumentos()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
