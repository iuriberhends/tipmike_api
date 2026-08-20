# -*- coding: utf-8 -*-
r"""
workers/esteira_job.py — o CICLO da esteira no sistema (passo 2).

Substitui o esteira_vps.py como executor (a CLI antiga continua existindo,
mas a fonte da verdade agora e' o banco):

  esteira_jobs   = a rodada    (status, carimbos de h2h, baseline, alertas)
  esteira_itens  = 1 linha por estrategia (snapshot EXATO + backtest_job_id)

O que este worker faz, na ordem:
  1. CLAIM       — assume o job (aceita 'pendente' E 'preparando': o daemon
                   marca preparando ao reservar; rodada manual chega pendente.
                   Licao do varredor: as duas pontas NUNCA podem discordar).
                   Retomada: 'rodando' com pid MORTO = continua dos itens
                   pendentes (isso substitui o esteira_state.json).
  2. CARIMBO     — h2h_ts_inicio = MAX(inserted_at) do h2h_historico.
  3. BASE        — prepara o parquet (fonte arquivo com recorte de dias, ou
                   upload_id pronto, ou fonte banco com casa+periodo).
  4. ITENS       — se a rodada veio sem itens, monta da planilha/params
                   (mesmo formato do estrategias.xlsx). Injeta a SENTINELA
                   (universo escancarado) se nao houver.
  5. SENTINELA   — roda PRIMEIRO. 0 apostas = para em ~1min com diagnostico
                   (teria evitado a rodada de 44min contra o parquet errado).
                   O resultado dela E' o baseline do mercado.
  6. ITENS       — cada um: INSERT em backtest_jobs (bot_id NULL) + motor
                   REAL inline (executar_backtest) com watchdog + metricas
                   lidas de apostas_detalhe + alertas ceticos por item.
                   Trava de N zerados seguidos. Hill-climb cria itens
                   papel='variacao' com pai_item_id.
  7. FECHO       — h2h_ts_fim (mudou no meio + havia chip => suspeita=true),
                   alertas da rodada, xlsx em esteiras\esteira_<id>.xlsx.

Snapshot e pre-compromisso: gravado no item ANTES de rodar. A chave
"_planilha" dentro do snapshot guarda a linha crua da planilha (o motor
ignora chaves que nao conhece) — e' dela que variacoes/hill-climb nascem.

Funcoes de calculo portadas da esteira_vps.py v3.2 (mesma matematica que
cravou o job 229: 667 ap 435-232 WR 65,2 ROI 19,19).

SELF-TEST (sem banco, sem motor):  python -m workers.esteira_job --teste
"""

import asyncio
import hashlib
import json
import math
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, date
from pathlib import Path

import numpy as np
import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent

# ------------------------------ knobs (env manda) ----------------------------
STAKE_PADRAO = float(os.environ.get("ESTEIRA_STAKE", "1.0"))
BANCA_PADRAO = float(os.environ.get("ESTEIRA_BANCA", "1000.0"))
TIMEOUT_ITEM_MIN = float(os.environ.get("ESTEIRA_TIMEOUT_ITEM_MIN", "45"))
MAX_ZERADOS_PADRAO = int(os.environ.get("ESTEIRA_MAX_ZERADOS", "3"))
REC_JANELAS = (3, 7)
HILL_CLIMB = os.environ.get("ESTEIRA_HILL_CLIMB", "1") != "0"
HILL_MAX_PASSOS = int(os.environ.get("ESTEIRA_HILL_MAX_PASSOS", "3"))
HILL_TOL_U = float(os.environ.get("ESTEIRA_HILL_TOL_U", "0.92"))
TOP_CARTEIRA = 8
CASA_PADRAO = os.environ.get("ESTEIRA_CASA_PADRAO", "bet365")
ESPORTE_PADRAO = os.environ.get("ESTEIRA_ESPORTE_PADRAO", "nba2k")
# alertas ceticos (a regua historica do projeto: premio real fica entre 9 e 21)
ALERTA_PREMIO_PTS = float(os.environ.get("ESTEIRA_ALERTA_PREMIO", "25"))
ALERTA_TOP3_PCT = float(os.environ.get("ESTEIRA_ALERTA_TOP3", "60"))

VARIACOES = {
    "chip_wr_min": [-5.0, +5.0],
    "chip_wr_max": [-5.0, +5.0],
    "folga_min":   [-1.0, +1.0],
    "linha_min":   [-1.0, +1.0],
    "linha_max":   [-1.0, +1.0],
    "teto":        [-2, +2],
}


class EsteiraErro(Exception):
    """Erro de negocio: mensagem e' pro usuario ler, sem traceback."""


class EsteiraCancelada(Exception):
    """A tela marcou cancelado; sair limpo sem sobrescrever o status."""


# =============================================================================
#  FUNCOES PURAS (portadas da esteira_vps.py v3.2 — mesma matematica)
# =============================================================================
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


def _passo_linha(base):
    return 5.0 if (base is not None and abs(base) >= 40) else 1.0


def montar_snapshot(e: dict, casa_padrao=None, esporte_padrao=None) -> dict:
    """Mesmo formato do _montar_snapshot_avulso do router — o motor entende.
    A linha crua da planilha vai junto em "_planilha" (o runner ignora chaves
    desconhecidas) para variacoes/hill-climb e auditoria."""
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
    planilha_crua = {k: str(v) for k, v in e.items()
                     if v is not None and str(v).strip().lower()
                     not in ("", "nan", "none")}
    return {
        "nome": f"Esteira: {e.get('nome')}",
        "casa": _txt(e.get("casa")) or (casa_padrao or CASA_PADRAO or None),
        "esporte": _txt(e.get("esporte")) or (esporte_padrao or ESPORTE_PADRAO or None),
        "mercado": _txt(e.get("mercado"), "ah_ft").lower(),
        "linha_min": _num(e.get("linha_min")),
        "linha_max": _num(e.get("linha_max")),
        "odd_min": _num(e.get("odd_min")),
        "odd_max": _num(e.get("odd_max")),
        "torneios": [], "torneios_excluir": [],
        "whitelist_pares": [], "blacklist_pares": [], "whitelist_cenarios": [],
        "max_apostas_partida": int(teto) if teto else None,
        "filtros": filtros,
        "_planilha": planilha_crua,
    }


def _fmt(v, suf=''):
    if v is None:
        return ''
    f = float(v)
    return (f'{int(f)}{suf}' if f == int(f) else f'{f:g}{suf}')


def resumo_config(snap: dict) -> dict:
    """Config RESOLVIDA em colunas — cada linha do placar se explica sozinha."""
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
    """Impressao digital do MOTOR: mexeu no runner, assinatura muda e nada
    de rodada antiga e' confundido com o motor novo (licao v2.7)."""
    cam = RAIZ / "workers" / "backtest_runner.py"
    try:
        if cam.is_file():
            return hashlib.sha1(cam.read_bytes()).hexdigest()[:10]
    except Exception:
        pass
    return "motor-desconhecido"


_MOTOR_HASH = None


def assinatura(snap: dict, base: str = '') -> str:
    """NOME e _planilha ficam FORA do hash (duas linhas com a mesma config sob
    nomes diferentes = mesma assinatura). BASE e MOTOR entram (trocou parquet
    ou runner = numero novo, nunca reaproveitado em silencio)."""
    global _MOTOR_HASH
    if _MOTOR_HASH is None:
        _MOTOR_HASH = _hash_motor()
    limpo = {k: v for k, v in snap.items() if k not in ('nome', '_planilha')}
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
            v["_passo"] = 1
            out.append(v)
    return out


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
                n["_passo"] = int(_num(v.get("_passo")) or 1) + 1
                n["nome"] = (f"{v.get('_mae')} [{campo}"
                             f"{'+' if dlt > 0 else ''}{dlt * n['_passo']:g}]")
                return n
    return None


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
    d["par"] = (d.get("jogador_a").astype(str).str.upper() + "|"
                + d.get("jogador_b").astype(str).str.upper())
    gap = d.groupby(d["par"])["ts"].diff().dt.total_seconds().div(60).fillna(999)
    d["jogo"] = ((gap > 45) | (gap == 999)).groupby(d["par"]).cumsum().astype(str) + d["par"]
    return d


def lucro_por_jogo(detalhe):
    d = _df_do_detalhe(detalhe)
    if not len(d):
        return pd.Series(dtype=float)
    return d.groupby("jogo")["u"].sum()


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
        "apostas": n, "greens": G, "reds": n - G, "G-R": f"{G}-{n - G}",
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
    pj = d.groupby("jogo")["u"].sum()
    out["jogos"] = int(len(pj))
    if len(pj) >= 5 and float(pj.std(ddof=1) or 0) > 0:
        out["z_jogo"] = round(float(pj.mean() / (pj.std(ddof=1)
                              / np.sqrt(len(pj)))), 2)
    else:
        out["z_jogo"] = None
    # CEGO: ultimos ~30% das apostas por tempo (holdout de sequencia)
    corte = int(n * 0.7)
    tr, cg = d.iloc[:corte], d.iloc[corte:]
    if len(cg) >= 10:
        out["roi_treino"] = round(float(tr["u"].sum()) / max(len(tr), 1) * 100, 2)
        out["roi_cego"] = round(float(cg["u"].sum()) / len(cg) * 100, 2)
        out["ap_cego"] = int(len(cg))
        out["desvio_cego"] = round(out["roi_cego"] - out["roi_treino"], 2)
    else:
        out["roi_cego"] = None
    # CONCENTRACAO por PAR (alerta cetico 3): quanto do lucro os 3 melhores
    # pares carregam. So faz sentido com lucro positivo.
    pp = d.groupby("par")["u"].sum().sort_values(ascending=False)
    lucro = float(pp.sum())
    if lucro > 0 and len(pp) >= 3:
        out["top3_par_pct"] = round(float(pp.head(3).clip(lower=0).sum())
                                    / lucro * 100, 1)
        out["pares"] = int(len(pp))
    else:
        out["top3_par_pct"] = None
        out["pares"] = int(len(pp))
    return out


# =============================================================================
#  ALERTAS CETICOS (todos nasceram de erro real do projeto)
# =============================================================================
def alertas_do_item(m: dict, baseline: dict | None) -> dict:
    """Por item: (3) concentracao top-3 pares; (4) magnitude implausivel
    (premio sobre o baseline do mercado alem da regua historica 9-21)."""
    al = {}
    if baseline and baseline.get("ROI") is not None and m.get("ROI") is not None:
        premio = round(m["ROI"] - baseline["ROI"], 2)
        al["premio_pts"] = premio
        if premio > ALERTA_PREMIO_PTS:
            al["premio_implausivel"] = (
                f"premio de {premio} pts sobre o mercado (regua do projeto: "
                f"9-21; acima de {ALERTA_PREMIO_PTS:g} e' suspeito)")
    t3 = m.get("top3_par_pct")
    if t3 is not None and t3 > ALERTA_TOP3_PCT:
        al["lucro_concentrado"] = (
            f"top-3 pares carregam {t3}% do lucro "
            f"(alerta acima de {ALERTA_TOP3_PCT:g}%)")
    if m.get("desvio_cego") is not None and m["desvio_cego"] < -15:
        al["cego_despencou"] = (
            f"ROI no cego caiu {abs(m['desvio_cego'])} pts vs treino")
    return al


def alertas_da_rodada(linhas: list, baseline: dict | None) -> dict:
    """Da rodada: (1) ranking inverteu? correlacao (spearman) treino x cego
    entre as estrategias (no garimpo 10 deu -0,685 e a #1 era a pior);
    (2) eixo derivado: comparacao estrategia x CONTROLE quando existir."""
    al = {}
    est = [l for l in linhas if l.get("papel") in ("estrategia", "variacao")
           and l.get("metricas", {}).get("roi_treino") is not None
           and l.get("metricas", {}).get("roi_cego") is not None]
    if len(est) >= 5:
        df = pd.DataFrame([{"t": l["metricas"]["roi_treino"],
                            "c": l["metricas"]["roi_cego"]} for l in est])
        # spearman = pearson dos RANKS — calculado assim de proposito:
        # method='spearman' do pandas importa scipy por baixo, e scipy nao
        # existe no venv da VPS (foi exatamente onde o teste 23 quebrou la).
        rho = float("nan")
        if df["t"].nunique() > 1 and df["c"].nunique() > 1:
            try:
                rho = float(df["t"].rank().corr(df["c"].rank()))
            except Exception:
                rho = float("nan")
        if rho == rho:
            al["corr_treino_cego"] = round(rho, 3)
            if rho < 0:
                al["ranking_invertido"] = (
                    f"correlacao treino x cego = {rho:.2f} (NEGATIVA): a ordem "
                    f"do treino nao vale no cego — nao escolher pela #1")
    ctrl = next((l for l in linhas if l.get("papel") == "controle"
                 and l.get("metricas", {}).get("apostas")), None)
    if ctrl:
        melhor = max((l for l in linhas if l.get("papel") == "estrategia"
                      and l.get("metricas", {}).get("ROI") is not None),
                     key=lambda l: l["metricas"]["ROI"], default=None)
        if melhor:
            al["controle"] = {
                "nome": ctrl.get("nome"),
                "roi_controle": ctrl["metricas"].get("ROI"),
                "roi_melhor": melhor["metricas"].get("ROI"),
                "delta_eixo_pts": round((melhor["metricas"].get("ROI") or 0)
                                        - (ctrl["metricas"].get("ROI") or 0), 2),
            }
    if baseline:
        al["baseline"] = {k: baseline.get(k) for k in
                          ("apostas", "G-R", "WR", "ROI", "unidades")}
    return al


def _json_safe(o):
    """NaN/np types quebram o json do banco — sanear antes de gravar."""
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        o = float(o)
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (datetime, date)):
        return str(o)
    return o


def _jdump(o) -> str:
    return json.dumps(_json_safe(o), ensure_ascii=False, default=str)


# =============================================================================
#  BASE (parquet) — fonte arquivo com recorte, upload pronto, ou banco
# =============================================================================
def _coluna_ts(df) -> str:
    for c in ("ts", "timestamp", "data_hora", "datahora", "time", "hora_ts",
              "created_at", "dt"):
        if c in df.columns:
            return c
    for c in df.columns:
        if "ts" == str(c).lower() or "time" in str(c).lower():
            return c
    return ""


def preparar_base(params: dict, log) -> tuple:
    """Devolve (upload_id, base_hash, data_inicio, data_fim).
    fonte arquivo -> grava o recorte em UPLOAD_DIR (upload_id = caminho);
    upload_id pronto -> usa direto; fonte banco -> upload_id None + datas."""
    from workers.backtest_upload import UPLOAD_DIR

    fonte = str(params.get("fonte") or "").strip().lower()
    up = params.get("upload_id")
    if up:
        if not os.path.exists(str(up)):
            raise EsteiraErro(f"upload_id nao existe no disco: {up}")
        h = hashlib.sha1(Path(str(up)).read_bytes()).hexdigest()[:16]
        log(f"base: upload pronto ({Path(str(up)).name})")
        return str(up), h, None, None

    if fonte == "banco":
        casa = _txt(params.get("casa"))
        d_ini, d_fim = params.get("data_inicio"), params.get("data_fim")
        if not (casa and d_ini and d_fim):
            raise EsteiraErro("fonte banco exige casa, data_inicio e data_fim "
                              "em params")
        di = date.fromisoformat(str(d_ini)[:10])
        df_ = date.fromisoformat(str(d_fim)[:10])
        log(f"base: BANCO {casa} {di} a {df_}")
        return None, f"banco_{casa}_{di}_{df_}", di, df_

    origem = Path(str(params.get("fonte_arquivo") or ""))
    if not origem.name:
        raise EsteiraErro("params precisa de upload_id, fonte_arquivo ou "
                          "fonte='banco' (casa+data_inicio+data_fim)")
    if not origem.is_file():
        alt = RAIZ / origem.name
        if alt.is_file():
            origem = alt
        else:
            raise EsteiraErro(f"fonte_arquivo nao encontrado: {origem}")

    dias = int(_num(params.get("dias")) or 0)
    bruto = hashlib.sha1(origem.read_bytes()).hexdigest()[:16]
    h = f"{bruto}_d{dias or 'tudo'}"
    Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    destino = str(Path(UPLOAD_DIR) / f"esteira_{h}.parquet")
    if os.path.exists(destino):
        log(f"base ja preparada ({dias or 'tudo'} dias) — reusando")
        return destino, h, None, None

    eh_csv = origem.suffix.lower() in (".csv", ".txt")
    if not eh_csv and not dias:
        shutil.copyfile(origem, destino)
        log(f"base preparada (parquet inteiro) em {destino}")
        return destino, h, None, None

    log(f"lendo {origem.name} ...")
    try:
        df = (pd.read_csv(origem, low_memory=False) if eh_csv
              else pd.read_parquet(origem))
    except UnicodeDecodeError:
        df = pd.read_csv(origem, low_memory=False, encoding="latin-1")
    n0 = len(df)
    if dias:
        col = _coluna_ts(df)
        if not col:
            log(f"AVISO: sem coluna de tempo em {list(df.columns)[:8]} — "
                f"seguindo SEM recorte")
        else:
            ts = pd.to_datetime(df[col], errors="coerce")
            fim = ts.max()
            if pd.isna(fim):
                log("AVISO: coluna de tempo sem data valida — sem recorte")
            else:
                ini = fim - pd.Timedelta(days=dias)
                df = df[ts >= ini].copy()
                _tr = ts[ts >= ini]
                _real = (_tr.max() - _tr.min()).days + 1
                log(f"recorte pedido: {dias}d | REAL no arquivo: {_real}d "
                    f"({_tr.min():%d/%m} a {_tr.max():%d/%m}) -> "
                    f"{len(df):,} de {n0:,} linhas")
                if df.empty:
                    raise EsteiraErro(
                        f"o recorte de {dias} dias nao deixou nenhuma linha — "
                        f"confira 'dias' ou a coluna de tempo ({col})")
    df.to_parquet(destino, index=False)
    log(f"base preparada em {destino}")
    return destino, h, None, None


# =============================================================================
#  O CICLO
# =============================================================================
def _log_factory(job_id: int):
    def log(msg):
        print(f"[esteira {job_id}] {msg}", flush=True)
    return log


async def _carimbo_h2h(pool, log):
    """MAX(inserted_at) do h2h_historico (o fix v14 provou que e' o relogio
    certo). Blindado: coluna ausente/tabela ausente -> None + aviso, nunca
    derruba a rodada."""
    for col in ("inserted_at", "ts"):
        try:
            async with pool.acquire() as conn:
                v = await conn.fetchval(f"SELECT MAX({col}) FROM h2h_historico")
            if v is not None:
                return v, col
        except Exception:
            continue
    log("AVISO: nao consegui carimbar o h2h_historico (tabela/coluna) — "
        "rodada segue sem deteccao de mudanca no meio")
    return None, None


async def _atualizar_job(pool, job_id: int, **campos):
    sets, vals = [], []
    for i, (k, v) in enumerate(campos.items(), start=2):
        if k in ("baseline", "alertas"):
            sets.append(f"{k} = ${i}::jsonb")
            vals.append(_jdump(v) if v is not None else None)
        else:
            sets.append(f"{k} = ${i}")
            vals.append(v)
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE esteira_jobs SET {', '.join(sets)} WHERE id = $1",
            job_id, *vals)


async def _checar_cancelamento(pool, job_id: int):
    async with pool.acquire() as conn:
        st = await conn.fetchval(
            "SELECT status FROM esteira_jobs WHERE id = $1", job_id)
    if str(st) == "cancelado":
        raise EsteiraCancelada()


async def _criar_item(pool, job_id: int, ordem: int, nome: str, papel: str,
                      snap: dict, ass: str, pai_id=None) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO esteira_itens
                   (esteira_job_id, ordem, nome, papel, pai_item_id,
                    assinatura, snapshot)
               VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
               RETURNING id""",
            job_id, ordem, nome, papel, pai_id, ass, _jdump(snap))


async def _montar_itens(pool, job, params, base_hash, log) -> int:
    """Se a rodada veio sem itens (origem planilha/manual), monta aqui.
    Itens ja criados pelo router (origem varredura) sao respeitados."""
    job_id = job["id"]
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM esteira_itens WHERE esteira_job_id = $1",
            job_id)
    linhas = []
    if n == 0:
        plan = params.get("planilha")
        if plan:
            cam = Path(str(plan))
            if not cam.is_file():
                alt = RAIZ / cam.name
                if alt.is_file():
                    cam = alt
                else:
                    raise EsteiraErro(f"planilha nao encontrada: {plan}")
            est = pd.read_excel(cam)
            est.columns = [str(c).strip().lower() for c in est.columns]
            if "nome" not in est.columns:
                est["nome"] = [f"estrategia_{i+1}" for i in range(len(est))]
            linhas = [dict(r) for _, r in est.iterrows()]
        elif params.get("itens"):
            linhas = [dict(x) for x in params["itens"]]
        else:
            raise EsteiraErro("rodada sem itens e sem params.planilha/itens — "
                              "nada a rodar")
        extra = []
        for e in linhas:
            if int(_num(e.get("variar")) or 0) == 1:
                extra += gerar_variacoes(e)
        ordem = 0
        mae_ids = {}
        for e in linhas + extra:
            papel = "variacao" if e.get("_mae") else str(
                e.get("papel") or "estrategia").strip().lower()
            if papel not in ("controle", "estrategia", "variacao"):
                papel = "estrategia"
            snap = montar_snapshot(e, params.get("casa_padrao"),
                                   params.get("esporte_padrao"))
            ass = assinatura(snap, base_hash)
            pai = mae_ids.get(str(e.get("_mae"))) if e.get("_mae") else None
            iid = await _criar_item(pool, job_id, ordem, str(e.get("nome")),
                                    papel, snap, ass, pai)
            if not e.get("_mae"):
                mae_ids[str(e.get("nome"))] = iid
            ordem += 1
        log(f"{len(linhas)} estrategias + {len(extra)} variacoes montadas")

    # SENTINELA obrigatoria (params.sem_sentinela=true desliga, explicito)
    async with pool.acquire() as conn:
        tem_sent = await conn.fetchval(
            """SELECT count(*) FROM esteira_itens
                WHERE esteira_job_id = $1 AND papel = 'sentinela'""", job_id)
        ref = await conn.fetchrow(
            """SELECT snapshot FROM esteira_itens
                WHERE esteira_job_id = $1 AND papel <> 'sentinela'
                ORDER BY ordem LIMIT 1""", job_id)
    if not tem_sent and not params.get("sem_sentinela") and ref:
        rs = ref["snapshot"]
        rs = json.loads(rs) if isinstance(rs, str) else dict(rs)
        snap_s = {
            "nome": "Esteira: CALIBRACAO (universo escancarado)",
            "casa": rs.get("casa"), "esporte": rs.get("esporte"),
            "mercado": rs.get("mercado"),
            "linha_min": None, "linha_max": None,
            "odd_min": None, "odd_max": None,
            "torneios": [], "torneios_excluir": [],
            "whitelist_pares": [], "blacklist_pares": [],
            "whitelist_cenarios": [],
            "max_apostas_partida": None,
            "filtros": {"evitarLinhasSeq": False},
            "_planilha": {"nome": "CALIBRACAO", "papel": "sentinela"},
        }
        await _criar_item(pool, job_id, -1, "CALIBRACAO (universo escancarado)",
                          "sentinela", snap_s, assinatura(snap_s, base_hash))
        log("sentinela injetada (universo escancarado do mesmo mercado)")

    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT count(*) FROM esteira_itens WHERE esteira_job_id = $1",
            job_id)
        await conn.execute(
            "UPDATE esteira_jobs SET total_itens = $2 WHERE id = $1",
            job_id, total)
    return int(total)


async def _rodar_item_no_motor(pool, job, item, upload_id, d_ini, d_fim,
                               params, log):
    """INSERT em backtest_jobs + motor real inline + metricas do detalhe.
    Mesmo INSERT da esteira_vps (que reproduziu o job 229 numero a numero)."""
    from workers.backtest_runner import executar_backtest

    snap = item["snapshot"]
    snap = json.loads(snap) if isinstance(snap, str) else dict(snap)
    stake = float(_num(params.get("stake")) or STAKE_PADRAO)
    banca = float(_num(params.get("banca")) or BANCA_PADRAO)
    timeout_s = float(_num(params.get("timeout_min")) or TIMEOUT_ITEM_MIN) * 60

    async with pool.acquire() as conn:
        bt_id = await conn.fetchval(
            """INSERT INTO backtest_jobs
                   (bot_id, data_inicio, data_fim, stake_modo, stake_valor,
                    banca_inicial, bot_snapshot, status, progresso,
                    upload_id, user_id)
               VALUES (NULL, $1, $2, 'fixo', $3, $4, $5::jsonb,
                       'pendente', 0, $6, $7)
               RETURNING id""",
            d_ini, d_fim, stake, banca, _jdump(snap), upload_id,
            job.get("user_id"))
    try:
        await asyncio.wait_for(executar_backtest(bt_id), timeout=timeout_s)
    except asyncio.TimeoutError:
        raise EsteiraErro(f"backtest {bt_id} passou de "
                          f"{timeout_s/60:.0f}min (watchdog)")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT status, erro, apostas_detalhe
                 FROM backtest_jobs WHERE id = $1""", bt_id)
    if row is None:
        raise EsteiraErro(f"backtest {bt_id} sumiu do banco")
    if str(row["status"]).lower() == "erro":
        raise EsteiraErro(f"backtest {bt_id} ERRO no motor: {row['erro']}")
    det = row["apostas_detalhe"]
    return bt_id, metricas_do_detalhe(det), lucro_por_jogo(det), snap


def _diagnostico_zerado(snap: dict) -> str:
    tem_chip = bool((snap.get("filtros") or {}).get("filtrosHistAdicionados"))
    if tem_chip:
        return ("0 apostas: ou a faixa (linha/atropelo/tot_env) nao existe "
                "neste arquivo, ou casa/esporte "
                f"({snap.get('casa')!r}/{snap.get('esporte')!r}) nao batem "
                "com o h2h — as rejeicoes no log do runner dizem qual")
    return "filtro cortou tudo (mecanica pura)"


async def executar_esteira(job_id: int):
    """O ciclo inteiro. Levanta EsteiraErro (negocio) ou Exception (bug)."""
    from database import get_pool
    pool = get_pool()
    log = _log_factory(job_id)

    async with pool.acquire() as conn:
        job = await conn.fetchrow("SELECT * FROM esteira_jobs WHERE id = $1",
                                  job_id)
    if job is None:
        raise EsteiraErro(f"rodada {job_id} nao existe")
    job = dict(job)
    status = str(job["status"])

    # aceita pendente E preparando (daemon reservou); 'rodando' com pid morto
    # e' RETOMADA (substitui o state.json); resto recusa alto e claro
    if status == "rodando":
        pid_ant = job.get("pid")
        if pid_ant and _pid_vivo_local(pid_ant):
            raise EsteiraErro(f"rodada {job_id} ja esta rodando no pid "
                              f"{pid_ant} — nao vou duplicar")
        log(f"retomada: pid anterior {pid_ant} morreu; sigo dos itens "
            f"pendentes")
    elif status not in ("pendente", "preparando"):
        raise EsteiraErro(f"rodada {job_id} esta '{status}' — nada a fazer")

    params = job.get("params") or {}
    if isinstance(params, str):
        params = json.loads(params or "{}")

    # itens presos em 'rodando' por execucao anterior morta (watchdog/queda)
    # voltam pra fila: so existe UM worker por rodada — se estamos assumindo,
    # ninguem os esta rodando. Sem isso, o item orfao sumia do placar (54/55).
    async with pool.acquire() as conn:
        res = await conn.execute(
            """UPDATE esteira_itens SET status='pendente', erro=NULL
                WHERE esteira_job_id=$1 AND status='rodando'""", job_id)
    try:
        n_orf = int(str(res).split()[-1])
    except Exception:
        n_orf = 0
    if n_orf:
        log(f"{n_orf} item(ns) orfao(s) de execucao anterior de volta a fila")

    await _atualizar_job(pool, job_id, status="preparando", pid=os.getpid(),
                         iniciado_em=datetime.now(), erro=None,
                         progresso_msg="preparando a base e os itens")

    # carimbo de abertura do h2h
    h2h_ini, h2h_col = await _carimbo_h2h(pool, log)
    if h2h_ini is not None:
        await _atualizar_job(pool, job_id, h2h_ts_inicio=h2h_ini)
        log(f"h2h carimbado ({h2h_col}): {h2h_ini}")

    upload_id, base_hash, d_ini, d_fim = preparar_base(params, log)
    total = await _montar_itens(pool, job, params, base_hash, log)
    log(f"{total} itens na rodada")

    await _atualizar_job(pool, job_id, status="rodando",
                         progresso_msg=f"0/{total} itens")

    max_zerados = int(_num(params.get("max_zerados"))
                      if params.get("max_zerados") is not None
                      else MAX_ZERADOS_PADRAO)

    baseline = None
    sentinela_ok = None
    zerados_seguidos = 0
    async with pool.acquire() as conn:
        prontos = int(await conn.fetchval(
            """SELECT count(*) FROM esteira_itens
                WHERE esteira_job_id = $1 AND status = 'concluido'""",
            job_id) or 0)
    cache_ass: dict = {}      # assinatura -> (bt_id, metricas) nesta rodada
    t_ini = time.time()

    async def _pendentes():
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM esteira_itens
                    WHERE esteira_job_id = $1 AND status = 'pendente'
                    ORDER BY (papel <> 'sentinela'), ordem, id""", job_id)
        return [dict(r) for r in rows]

    fila = await _pendentes()
    # sentinela obrigatoria roda primeiro; se a rodada e' retomada e a
    # sentinela ja concluiu, recupera o baseline dela
    async with pool.acquire() as conn:
        sent_feita = await conn.fetchrow(
            """SELECT metricas FROM esteira_itens
                WHERE esteira_job_id = $1 AND papel = 'sentinela'
                  AND status = 'concluido'""", job_id)
    if sent_feita and sent_feita["metricas"]:
        m = sent_feita["metricas"]
        baseline = json.loads(m) if isinstance(m, str) else dict(m)
        sentinela_ok = bool(baseline.get("apostas"))

    while fila:
        item = fila.pop(0)
        await _checar_cancelamento(pool, job_id)
        nome, papel, iid = item["nome"], item["papel"], item["id"]
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE esteira_itens SET status='rodando',
                          iniciado_em=NOW() WHERE id=$1""", iid)
        t0 = time.time()
        try:
            ass = item.get("assinatura") or ""
            if ass and ass in cache_ass:
                bt_id, m = cache_ass[ass]
                snap = item["snapshot"]
                snap = json.loads(snap) if isinstance(snap, str) else dict(snap)
                pj = None
                log(f"'{nome}': identica a outra desta rodada — "
                    f"reaproveitando (job {bt_id})")
            else:
                log(f"'{nome}' ({papel}): rodando no motor...")
                bt_id, m, pj, snap = await _rodar_item_no_motor(
                    pool, job, item, upload_id, d_ini, d_fim, params, log)
                if ass:
                    cache_ass[ass] = (bt_id, m)
            al = alertas_do_item(m, baseline) if papel != "sentinela" else {}
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE esteira_itens
                          SET status='concluido', backtest_job_id=$2,
                              metricas=$3::jsonb, alertas=$4::jsonb,
                              finalizado_em=NOW()
                        WHERE id=$1""",
                    iid, bt_id, _jdump(m), _jdump(al) if al else None)
            prontos += 1
            await _atualizar_job(
                pool, job_id, itens_prontos=prontos,
                progresso_msg=f"{prontos}/{total} itens · ultimo: {nome} "
                              f"({m.get('apostas', 0)} ap)")

            if papel == "sentinela":
                baseline = m
                sentinela_ok = bool(m.get("apostas"))
                await _atualizar_job(pool, job_id, sentinela_ok=sentinela_ok,
                                     baseline=m)
                if not sentinela_ok:
                    raise EsteiraErro(
                        "SENTINELA ZEROU: o universo escancarado nao gerou "
                        "nenhuma aposta. Quase sempre e': (1) parquet/fonte "
                        "errada, (2) coluna mercado, (3) casa/esporte. "
                        "Nada mais foi rodado — corrija e rode de novo.")
                min_ap = int(_num(params.get("sentinela_ap_min")) or 1)
                if m["apostas"] < min_ap:
                    raise EsteiraErro(
                        f"SENTINELA ABAIXO DO MINIMO: {m['apostas']} apostas "
                        f"< sentinela_ap_min ({min_ap}) — base suspeita")
                log(f"sentinela OK: {m['apostas']} ap | baseline do mercado "
                    f"ROI {m.get('ROI')}%")
                continue

            if not m.get("apostas"):
                zerados_seguidos += 1
                log(f"  job {bt_id} em {time.time()-t0:.0f}s -> 0 apostas "
                    f"<- {_diagnostico_zerado(snap)}")
                if max_zerados and zerados_seguidos >= max_zerados:
                    raise EsteiraErro(
                        f"{zerados_seguidos} itens SEGUIDOS com 0 apostas — "
                        f"abortando (mercado {snap.get('mercado')!r}, "
                        f"casa/esporte {snap.get('casa')!r}/"
                        f"{snap.get('esporte')!r}). O que ja rodou esta "
                        f"gravado; corrija e rode de novo que a retomada "
                        f"pula o concluido.")
            else:
                zerados_seguidos = 0
                log(f"  job {bt_id} em {time.time()-t0:.0f}s -> "
                    f"{m['apostas']} ap | {m.get('G-R')} | WR {m.get('WR')} "
                    f"| ROI {m.get('ROI')} | 3d {m.get('roi_3d')}")

            # HILL-CLIMB: variacao que melhorou a mae anda mais um passo
            if (HILL_CLIMB and papel == "variacao" and m.get("apostas", 0) > 0
                    and item.get("pai_item_id")):
                plan = snap.get("_planilha") or {}
                passo = int(_num(plan.get("_passo")) or 1)
                if passo < HILL_MAX_PASSOS:
                    async with pool.acquire() as conn:
                        mae = await conn.fetchrow(
                            """SELECT metricas FROM esteira_itens
                                WHERE id = $1 AND status = 'concluido'""",
                            item["pai_item_id"])
                    mm = None
                    if mae and mae["metricas"]:
                        mm = (json.loads(mae["metricas"])
                              if isinstance(mae["metricas"], str)
                              else dict(mae["metricas"]))
                    if mm and m.get("ROI") is not None:
                        u_mae = mm.get("unidades")
                        piso_u = (u_mae * HILL_TOL_U
                                  if isinstance(u_mae, (int, float))
                                  and u_mae > 0 else -9)
                        if (m["ROI"] > (mm.get("ROI") or -9)
                                and m.get("unidades", -9) >= piso_u):
                            prox = _proximo_passo(plan)
                            if prox is not None:
                                snap_p = montar_snapshot(
                                    prox, params.get("casa_padrao"),
                                    params.get("esporte_padrao"))
                                iid_p = await _criar_item(
                                    pool, job_id, int(item["ordem"]),
                                    str(prox["nome"]), "variacao", snap_p,
                                    assinatura(snap_p, base_hash),
                                    item["pai_item_id"])
                                total += 1
                                await _atualizar_job(pool, job_id,
                                                     total_itens=total)
                                log(f"  hill-climb: '{prox['nome']}' entra "
                                    f"na fila")
                                fila.insert(0, {
                                    "id": iid_p, "nome": str(prox["nome"]),
                                    "papel": "variacao",
                                    "pai_item_id": item["pai_item_id"],
                                    "ordem": item["ordem"],
                                    "assinatura": assinatura(snap_p, base_hash),
                                    "snapshot": snap_p, "status": "pendente"})
        except (EsteiraErro, EsteiraCancelada):
            raise
        except Exception as ex:
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE esteira_itens SET status='erro', erro=$2,
                              finalizado_em=NOW() WHERE id=$1""",
                    iid, f"{type(ex).__name__}: {ex}"[:800])
            log(f"  ERRO no item '{nome}': {ex} (fila segue)")

    # ---------------- fecho da rodada ----------------
    # placar/carteira reconstruidos do BANCO: cobre retomada (itens de
    # execucoes anteriores entram no xlsx e nos alertas) e deixa uma
    # fonte de verdade so
    async with pool.acquire() as conn:
        rows_f = await conn.fetch(
            """SELECT i.nome, i.papel, i.snapshot, i.metricas, i.alertas,
                      b.apostas_detalhe
                 FROM esteira_itens i
                 LEFT JOIN backtest_jobs b ON b.id = i.backtest_job_id
                WHERE i.esteira_job_id = $1 AND i.status = 'concluido'
                ORDER BY i.ordem, i.id""", job_id)
    linhas_placar, por_jogo = [], {}
    for r in rows_f:
        if r["papel"] == "sentinela":
            continue
        _s = r["snapshot"]
        _s = json.loads(_s) if isinstance(_s, str) else dict(_s or {})
        _m = r["metricas"]
        _m = json.loads(_m) if isinstance(_m, str) else dict(_m or {})
        _a = r["alertas"]
        _a = json.loads(_a) if isinstance(_a, str) else dict(_a or {})
        linhas_placar.append({"nome": r["nome"], "papel": r["papel"],
                              "snapshot": _s, "metricas": _m, "alertas": _a})
        if _m.get("apostas") and r["apostas_detalhe"]:
            try:
                por_jogo[r["nome"]] = lucro_por_jogo(r["apostas_detalhe"])
            except Exception:
                pass
    al_rodada = alertas_da_rodada(linhas_placar, baseline)
    h2h_fim, _ = await _carimbo_h2h(pool, log)
    suspeita, motivo = False, None
    if h2h_ini is not None and h2h_fim is not None and h2h_fim != h2h_ini:
        tem_chip = any((l["snapshot"].get("filtros") or {})
                       .get("filtrosHistAdicionados")
                       for l in linhas_placar)
        if tem_chip:
            suspeita = True
            motivo = (f"h2h_historico mudou durante a rodada "
                      f"({h2h_ini} -> {h2h_fim}) e havia config com chip: "
                      f"numeros nao comparaveis com re-run")
            log(f"AVISO: {motivo}")

    try:
        arq = _gravar_xlsx(job_id, linhas_placar, baseline, por_jogo)
        log(f"placar salvo em {arq}")
    except Exception as ex:
        log(f"AVISO: xlsx nao gerado ({ex}) — resultado integro no banco")

    await _atualizar_job(
        pool, job_id, status="concluido", finalizado_em=datetime.now(),
        h2h_ts_fim=h2h_fim, suspeita=suspeita, suspeita_motivo=motivo,
        alertas=al_rodada,
        progresso_msg=f"{prontos}/{total} itens em "
                      f"{(time.time()-t_ini)/60:.0f} min")
    log(f"rodada concluida: {prontos}/{total} itens")


def _pid_vivo_local(pid) -> bool:
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except ImportError:
        pass
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        cod = ctypes.c_ulong()
        k32.GetExitCodeProcess(h, ctypes.byref(cod))
        k32.CloseHandle(h)
        return cod.value == 259
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


# =============================================================================
#  XLSX (PLACAR / VARIACOES / EVOLUCAO / CARTEIRA) — o painel le do banco,
#  mas o xlsx continua saindo pra analise manual
# =============================================================================
def _gravar_xlsx(job_id: int, linhas: list, baseline, por_jogo: dict) -> str:
    pasta = RAIZ / "esteiras"
    pasta.mkdir(parents=True, exist_ok=True)
    arq = str(pasta / f"esteira_{job_id}.xlsx")
    reg = []
    for l in linhas:
        m = dict(l["metricas"] or {})
        al = l.get("alertas") or {}
        reg.append({"estrategia": l["nome"], "papel": l["papel"],
                    **resumo_config(l["snapshot"]),
                    "mae": (l["snapshot"].get("_planilha") or {}).get("_mae", ""),
                    "eixo": (l["snapshot"].get("_planilha") or {}).get("_eixo", ""),
                    "job": None, **m,
                    "alertas": "; ".join(f"{k}" for k in al
                                         if k not in ("premio_pts",))})
    P = pd.DataFrame(reg)
    if not len(P):
        raise RuntimeError("nada a gravar")
    maes = P[P["mae"].astype(str) == ""].copy()
    w0 = REC_JANELAS[0]
    if f"roi_{w0}d" in maes.columns:
        maes = maes.sort_values([f"roi_{w0}d", "ROI"], ascending=False,
                                na_position="last")
    variacoes = P[P["mae"].astype(str) != ""].copy()
    if len(variacoes) and len(maes):
        ref = maes.set_index("estrategia")

        def _delta(r, col):
            try:
                return round(r[col] - ref.loc[r["mae"], col], 2)
            except Exception:
                return None
        for col, novo in (("ROI", "dROI"), ("apostas", "dAp"),
                          ("unidades", "dU")):
            if col in variacoes.columns:
                variacoes[novo] = variacoes.apply(
                    lambda r, c=col: _delta(r, c), axis=1)
        u_mae_col = variacoes.apply(
            lambda r: (ref.loc[r["mae"], "unidades"]
                       if r["mae"] in ref.index else np.nan), axis=1)
        if "unidades" in variacoes.columns:
            variacoes["ret_u"] = (variacoes["unidades"] / u_mae_col).round(3)
            variacoes["veredito"] = np.where(
                (variacoes.get("dROI", pd.Series(dtype=float)).fillna(-9) > 0)
                & (variacoes["ret_u"].fillna(0) >= HILL_TOL_U),
                "MELHOROU (roi+ mantendo lucro)",
                np.where(variacoes.get("dROI", pd.Series(dtype=float))
                         .fillna(-9) > 0, "roi+ mas custa lucro", "-"))
    cart = None
    tops = [n for n in maes["estrategia"].head(TOP_CARTEIRA)
            if n in por_jogo] if len(maes) else []
    if len(tops) >= 2:
        M = pd.DataFrame({n: por_jogo[n] for n in tops}).fillna(0.0)
        cart = M.corr().round(2)
    with pd.ExcelWriter(arq) as w:
        maes.to_excel(w, sheet_name="PLACAR", index=False)
        if len(variacoes):
            (variacoes.sort_values("dROI", ascending=False)
             if "dROI" in variacoes.columns else variacoes).to_excel(
                w, sheet_name="VARIACOES", index=False)
        if baseline:
            pd.DataFrame([baseline]).to_excel(w, sheet_name="BASELINE",
                                              index=False)
        if cart is not None:
            cart.to_excel(w, sheet_name="CARTEIRA")
    return arq


# =============================================================================
#  SELF-TEST — python -m workers.esteira_job --teste  (sem banco, sem motor)
# =============================================================================
def _teste():
    ok = [0]

    def check(cond, nome):
        if not cond:
            print(f"FALHOU: {nome}")
            sys.exit(1)
        ok[0] += 1
        print(f"ok {ok[0]:>2}: {nome}")

    # snapshot basico com chip/folga/atropelo/tot_env/teto
    e = {"nome": "t1", "mercado": "ah_ft", "linha_min": 9.5, "teto": 4,
         "chip_janela": "all", "chip_wr_min": 65, "chip_conf": 0,
         "chip_conf_max": 100, "folga_min": 3.5, "atropelo_max": 22,
         "tot_env_min": 58, "evitar_linhas_seq": 0}
    s = montar_snapshot(e)
    f = s["filtros"]
    check(s["mercado"] == "ah_ft" and s["linha_min"] == 9.5
          and s["max_apostas_partida"] == 4, "snapshot: campos basicos")
    check(f["filtrosHistAdicionados"][0]["prob"][0] == 65
          and f["filtrosHistAdicionados"][0]["maxPartidas"] == 100,
          "snapshot: chip com teto de confrontos (v15)")
    check(f["folgaAtivo"] and f["folgaMin"] == 3.5, "snapshot: folga")
    check(f["atropeloAtivo"] and f["atropeloMax"] == 22,
          "snapshot: atropelo (v16)")
    check(f["totEnvAtivo"] and f["totEnvMin"] == 58,
          "snapshot: tot_env (v17), nao momento")
    check("momentoAtivo" not in f, "snapshot: momento nao vaza do tot_env")
    check(s["casa"] == CASA_PADRAO and s["esporte"] == ESPORTE_PADRAO,
          "snapshot: casa/esporte default quando planilha vazia")

    # assinatura ignora nome/_planilha; muda com base e config
    s2 = montar_snapshot({**e, "nome": "outro_nome"})
    check(assinatura(s, "b1") == assinatura(s2, "b1"),
          "assinatura: nome fora do hash")
    check(assinatura(s, "b1") != assinatura(s, "b2"),
          "assinatura: base entra no hash")
    s3 = montar_snapshot({**e, "linha_min": 10.5})
    check(assinatura(s, "b1") != assinatura(s3, "b1"),
          "assinatura: config entra no hash")

    # metricas de um detalhe sintetico conhecido
    base_ts = datetime(2026, 8, 10, 12, 0, 0)
    det = []
    for i, (res, u) in enumerate([("green", 0.9), ("green", 0.9),
                                  ("red", -1.0), ("green", 0.9)]):
        det.append({"resultado": res, "lucro_unidades": u,
                    "ts": (base_ts + timedelta(days=i * 2)).isoformat(),
                    "jogador_a": "A", "jogador_b": f"B{i}"})
    m = metricas_do_detalhe(det)
    check(m["apostas"] == 4 and m["G-R"] == "3-1", "metricas: ap e G-R")
    check(abs(m["unidades"] - 1.7) < 1e-9 and abs(m["ROI"] - 42.5) < 1e-9,
          "metricas: unidades e ROI")
    check(abs(m["DD"] - 1.0) < 1e-9, "metricas: DD pico->vale")
    check(m["top3_par_pct"] is not None and m["pares"] == 4,
          "metricas: concentracao por par calculada")

    # lucro_por_jogo separa jogos do mesmo par por gap > 45min
    det2 = [{"resultado": "green", "lucro_unidades": 1.0,
             "ts": base_ts.isoformat(), "jogador_a": "X", "jogador_b": "Y"},
            {"resultado": "red", "lucro_unidades": -1.0,
             "ts": (base_ts + timedelta(minutes=10)).isoformat(),
             "jogador_a": "X", "jogador_b": "Y"},
            {"resultado": "green", "lucro_unidades": 1.0,
             "ts": (base_ts + timedelta(hours=3)).isoformat(),
             "jogador_a": "X", "jogador_b": "Y"}]
    pj = lucro_por_jogo(det2)
    check(len(pj) == 2, "lucro_por_jogo: 2 jogos do mesmo par (gap 45min)")

    # variacoes e hill-climb
    vs = gerar_variacoes({"nome": "m", "linha_min": 9.5, "teto": 4,
                          "variar": 1})
    check(len(vs) == 4, "variacoes: linha_min +-1 e teto +-2")
    v = next(x for x in vs if x["_eixo"] == "linha_min+1")
    check(v["linha_min"] == 10.5, "variacoes: passo de linha pequeno = 1.0")
    p = _proximo_passo(v)
    check(p and p["linha_min"] == 11.5 and p["_passo"] == 2,
          "hill-climb: proximo passo na mesma direcao")
    vg = gerar_variacoes({"nome": "ou", "linha_max": 150, "variar": 1})
    vgm = next(x for x in vg if x["_eixo"] == "linha_max+1")
    check(vgm["linha_max"] == 155, "variacoes: passo escala p/ linha de total")

    # alertas
    al = alertas_do_item({"ROI": 40.0, "top3_par_pct": 70.0,
                          "desvio_cego": -20.0},
                         {"ROI": 5.0})
    check("premio_implausivel" in al and al["premio_pts"] == 35.0,
          "alerta: premio > 25 pts sobre o baseline")
    check("lucro_concentrado" in al, "alerta: top-3 pares > 60%")
    check("cego_despencou" in al, "alerta: cego caiu 15+ pts")
    linhas = [{"papel": "estrategia",
               "metricas": {"roi_treino": t, "roi_cego": c, "ROI": t}}
              for t, c in [(30, 2), (25, 5), (20, 8), (15, 12), (10, 15)]]
    ar = alertas_da_rodada(linhas, {"ROI": 1.0})
    check("ranking_invertido" in ar and ar["corr_treino_cego"] < 0,
          "alerta: ranking invertido (spearman negativo)")

    # json safe
    j = json.loads(_jdump({"a": float("nan"), "b": np.int64(3),
                           "c": np.float64(1.5), "d": datetime(2026, 1, 1)}))
    check(j["a"] is None and j["b"] == 3 and j["c"] == 1.5,
          "json: NaN vira null, tipos numpy viram nativos")

    print(f"\n=== SELF-TEST ESTEIRA: {ok[0]}/{ok[0]} PASSARAM ===")


if __name__ == "__main__":
    if "--teste" in sys.argv:
        _teste()
    else:
        print("uso: python -m workers.esteira_job --teste\n"
              "(o ciclo real e' chamado pelo workers.run_esteira)")
