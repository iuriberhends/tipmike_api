# -*- coding: utf-8 -*-
"""
===============================================================================
 PARIDADE T4 — um eixo por vez, varredor x motor
===============================================================================
 Por que existe: o varredor MEDE no export; o motor MEDE no parquet. Se um
 eixo e' traduzido diferente (folga com sinal invertido no favorito, janela
 do comp em 'last_30' em vez de 30, mercado default ah_ft...), o numero do
 garimpo e' de OUTRA estrategia e ninguem ve. O T4 pega cada eixo da grade,
 monta UMA config so com ele, calcula o que o varredor preve e manda pro
 motor. Eixo que nao crava dentro da tolerancia SAI da grade com o motivo.

 Uso (na VPS, dentro de tipmike_api, venv ativo):

   1) gerar a planilha da esteira + o previsto de cada item
      python mineracao\\paridade_t4.py gerar ^
          --entrada varreduras\\varredura_34_entrada.csv ^
          --casa estrelabet --esporte fifa --mercado over_under_ft --lado under ^
          --out paridade_34.xlsx

      -> paridade_34.xlsx  (rodar na esteira, fonte = parquet do job-mae)
      -> paridade_34.previsto.json

   2) depois da esteira concluir, baixar o placar (esteira_N.xlsx) e:
      python mineracao\\paridade_t4.py comparar ^
          --placar esteira_N.xlsx --previsto paridade_34.previsto.json

      -> tabela eixo a eixo: apostas/unidades previsto x motor, desvio %, e o
         veredito CRAVA / DIVERGE por eixo.

 Tolerancia: 5% em apostas E 5% em unidades (ou 2u absolutas, o que for
 maior). Eixo com previsto = 0 apostas e' pulado (nao ha o que comparar).
===============================================================================
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import varredura as V  # noqa: E402  (preparar_D / mascara_config — fonte unica)

TOL_PCT = 5.0
TOL_ABS_U = 2.0


# ----------------------------------------------------------------------------
# a grade do T4: cada item = UM eixo. Os cortes sao escolhidos por quantil do
# proprio export, pra garantir que cada item tenha volume (nada de 0 apostas).
# ----------------------------------------------------------------------------
def _q(v, p):
    v = np.asarray(v, dtype=float)
    v = v[np.isfinite(v)]
    return float(np.round(np.quantile(v, p), 2)) if v.size else None


def montar_grade(D):
    WR, COMP = D['WR'], D['COMP']
    lin, odd, qtd = D['lin'], D['odd'], D['qtd']
    base = {'janela': '-', 'wr_min': 0, 'wr_max': '-', 'janela2': '-', 'op2': '-',
            'wr2': '-', 'conf_min': 0, 'conf_max': '-', 'linha_min': '-',
            'linha_max': '-', 'odd_min': '-', 'odd_max': '-', 'lado': '-',
            'extra': '-', 'teto': '-'}
    itens = [('T0 SENTINELA (nada)', dict(base))]

    # --- chips de PAR e INDIVIDUAIS: um por janela, piso na mediana ---
    for jan in WR:
        v = WR[jan]
        med = _q(v[v > 0], 0.5)
        if med is None:
            continue
        itens.append((f'chip {jan} >= {med:g}', dict(base, janela=jan, wr_min=med)))
        # teto tambem (a familia do garimpo 34 usava wr_max)
        p75 = _q(v[v > 0], 0.75)
        if p75 and p75 > med:
            itens.append((f'chip {jan} <= {p75:g}', dict(base, janela=jan, wr_min=0, wr_max=p75)))

    # --- conf (maturidade do h2h) ---
    if np.isfinite(qtd).any() and np.nanmax(qtd) < 1e8:
        for p, rot in ((0.5, 'conf >='), (0.5, 'conf <=')):
            c = int(_q(qtd, p))
            if rot == 'conf >=':
                itens.append((f'conf >= {c}', dict(base, conf_min=c)))
            else:
                itens.append((f'conf <= {c}', dict(base, conf_max=c)))

    # --- linha / odd ---
    for p, k in ((0.5, 'linha_min'), (0.5, 'linha_max')):
        c = _q(lin, p)
        itens.append((f'{k} {c:g}', dict(base, **{k: c})))
    for p, k in ((0.5, 'odd_min'), (0.5, 'odd_max')):
        c = _q(odd, p)
        itens.append((f'{k} {c:g}', dict(base, **{k: c})))

    # --- teto por jogo ---
    itens.append(('teto 2', dict(base, teto=2)))

    # --- complementares: mecanicos e comp (um piso e um teto por eixo) ---
    for nome, v in COMP.items():
        low = nome.lower()
        if low.startswith(('desvio', 'atropelo')) or 'ind a' in low or 'ind b' in low:
            continue                      # o motor nao filtra por eles / posicionais
        med = _q(v, 0.5)
        if med is None:
            continue
        itens.append((f'{nome} >= {med:g}', dict(base, extra=f'{nome}>={med:g}')))
        p25 = _q(v, 0.25)
        if p25 is not None and p25 < med:
            itens.append((f'{nome} <= {p25:g}', dict(base, extra=f'{nome}<={p25:g}')))
    return itens


def gerar(a):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'workers'))
    import esteira_conversor as C
    D = V.preparar_D(a.entrada)
    itens = montar_grade(D)
    linhas, previsto = [], {}
    for nome, cfg in itens:
        try:
            m = V.mascara_config(cfg, D)
        except Exception as e:
            print(f'  pulo {nome}: {e}')
            continue
        n = int(m.sum())
        if n < 30:
            print(f'  pulo {nome}: so {n} apostas no varredor')
            continue
        u = float(D['u'][m].sum())
        g = int(D['green'][m].sum())
        try:
            l = C.converter(dict(cfg, lado='-'), casa=a.casa, esporte=a.esporte,
                            mercado=a.mercado, nome=f'T4 {nome}')
        except C.ConfigNaoReproduzivel as e:
            print(f'  FORA DA GRADE (motor nao tem): {nome} -> {e}')
            continue
        if a.lado in ('over', 'under'):
            l['lado'] = a.lado
        l['papel'] = 'controle' if nome.startswith('T0') else 'estrategia'
        l['variar'] = 0
        if a.as_of:
            l['h2h_as_of'] = a.as_of      # cada item herda o carimbo do job-mae
        linhas.append(l)
        previsto[f'T4 {nome}'] = {'apostas': n, 'G': g, 'R': n - g,
                                 'unidades': round(u, 2),
                                 'ROI': round(u / n * 100, 2)}
    df = pd.DataFrame(linhas)
    df.to_excel(a.out, index=False)
    jpath = os.path.splitext(a.out)[0] + '.previsto.json'
    with open(jpath, 'w', encoding='utf-8') as f:
        json.dump(previsto, f, ensure_ascii=False, indent=1)
    print(f'\n{len(linhas)} itens -> {a.out}\nprevisto -> {jpath}')
    print('agora: rodar a planilha na esteira, fonte = parquet do job-mae, e depois `comparar`.')


def comparar(a):
    prev = json.load(open(a.previsto, encoding='utf-8'))
    P = pd.read_excel(a.placar, 'PLACAR')
    col_nome = 'estrategia' if 'estrategia' in P.columns else P.columns[0]
    out = []
    for _, r in P.iterrows():
        nome = str(r[col_nome]).strip()
        chave = next((k for k in prev if nome.startswith(k)), None)
        if chave is None:
            continue
        pv = prev[chave]
        ap_m = float(r.get('apostas') or 0)
        u_m = float(r.get('unidades') or 0)
        d_ap = (ap_m - pv['apostas']) / max(pv['apostas'], 1) * 100
        d_u = u_m - pv['unidades']
        tol_u = max(TOL_ABS_U, abs(pv['unidades']) * TOL_PCT / 100)
        ok = abs(d_ap) <= TOL_PCT and abs(d_u) <= tol_u
        out.append({'eixo': chave.replace('T4 ', ''), 'ap_prev': pv['apostas'], 'ap_motor': int(ap_m),
                    'd_ap%': round(d_ap, 1), 'u_prev': pv['unidades'], 'u_motor': round(u_m, 2),
                    'd_u': round(d_u, 2), 'veredito': 'CRAVA' if ok else 'DIVERGE'})
    R = pd.DataFrame(out)
    pd.set_option('display.width', 200)
    print(R.to_string(index=False))
    n_ok = int((R.veredito == 'CRAVA').sum())
    print(f'\n{n_ok}/{len(R)} eixos cravam (tolerancia {TOL_PCT}% / {TOL_ABS_U}u)')
    div = R[R.veredito == 'DIVERGE']
    if len(div):
        print('\nSAEM DA GRADE ate' + " serem consertados:")
        for _, r in div.iterrows():
            print(f'  - {r.eixo}: apostas {r.ap_prev} -> {r.ap_motor} ({r["d_ap%"]:+.1f}%), '
                  f'unidades {r.u_prev} -> {r.u_motor}')
    R.to_csv(os.path.splitext(a.placar)[0] + '.paridade.csv', index=False)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    g = sub.add_parser('gerar')
    g.add_argument('--entrada', required=True)
    g.add_argument('--casa', required=True)
    g.add_argument('--esporte', required=True)
    g.add_argument('--mercado', required=True)
    g.add_argument('--lado', default='')
    g.add_argument('--out', default='paridade_t4.xlsx')
    g.add_argument('--as-of', dest='as_of', default='',
                   help='carimbo do h2h do job-mae (backtest_jobs.h2h_as_of), ex.: "2026-09-11 17:38:03"')
    c = sub.add_parser('comparar')
    c.add_argument('--placar', required=True)
    c.add_argument('--previsto', required=True)
    a = ap.parse_args()
    (gerar if a.cmd == 'gerar' else comparar)(a)
