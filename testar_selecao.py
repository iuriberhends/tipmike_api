# -*- coding: utf-8 -*-
r"""
testar_selecao.py — prova as duas pecas antes de plugar no sistema.

Roda o caminho inteiro OFFLINE: garimpo -> conversor -> alertas -> planilha
que a esteira consumiria. Nao toca no banco, nao precisa de API.

USO
    python testar_selecao.py --garimpo varredura_11_tudo.csv --baseline -4.58
    python testar_selecao.py --garimpo varredura_10_tudo.csv --baseline -4.88 ^
                             --holdout h10_tudo.csv --top 30 --criterio ldd

O QUE ELE CONFERE (cada um ja quebrou de verdade neste projeto)
  1. o conversor recusa eixo que o motor nao aplica, com motivo visivel
  2. o espelho do favorito nao perde extremo da faixa
  3. a linha convertida bate com o montar_snapshot da esteira
  4. os alertas disparam no dado que ja enganou
  5. a planilha sai no formato que a esteira le
"""
import argparse
import importlib.util as iu
import os
import sys

import pandas as pd

AQUI = os.path.dirname(os.path.abspath(__file__))


def carregar(nome, arquivo):
    for base in (AQUI, os.getcwd(), os.path.join(AQUI, 'workers')):
        p = os.path.join(base, arquivo)
        if os.path.isfile(p):
            spec = iu.spec_from_file_location(nome, p)
            m = iu.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
    raise SystemExit(f'nao achei {arquivo} (procurei em {AQUI} e no diretorio atual)')


def ok(c):
    return '  OK ' if c else '  XX '


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--garimpo', required=True)
    p.add_argument('--holdout', default=None, help='_tudo.csv do holdout (opcional)')
    p.add_argument('--baseline', type=float, default=None, help='ROI do mercado no treino')
    p.add_argument('--top', type=int, default=30)
    p.add_argument('--criterio', default='lucro_dd')
    p.add_argument('--saida', default='estrategias_da_varredura.xlsx')
    a = p.parse_args()

    C = carregar('conv', 'esteira_conversor.py')
    S = carregar('sel', 'esteira_selecao.py')

    print('=' * 74)
    print(' TESTE DE PONTA A PONTA — garimpo -> planilha da esteira')
    print('=' * 74)

    g = pd.read_csv(a.garimpo, low_memory=False)
    print(f'\ngarimpo: {len(g):,} configs · {a.garimpo}')

    # ---------------------------------------------------- 1. o conversor ----
    print('\n' + '-' * 74)
    print(' 1. CONVERSOR — recusa o que o motor nao sabe aplicar?')
    print('-' * 74)
    for e in ('atropelo>=36.49', 'tot_env<=24', 'folga -0.5~3.5', 'momento>=58'):
        try:
            print(f'{ok(True)}{e:<20} -> {C.parse_extra(e)}')
        except Exception as x:
            print(f'{ok(False)}{e:<20} -> RECUSOU (nao devia): {x}')
    for e in ('desloc<=-5', 'lin_ini>=4.5', 'dif>=6'):
        try:
            C.parse_extra(e)
            print(f'{ok(False)}{e:<20} -> PASSOU, e nao devia — o motor nao tem esse filtro')
        except C.ConfigNaoReproduzivel:
            print(f'{ok(True)}{e:<20} -> recusado (certo: rodaria sem o corte)')

    # ------------------------------------------- 2. o espelho do favorito ---
    print('\n' + '-' * 74)
    print(' 2. LADO FAVORITO — o espelho da linha preserva a faixa?')
    print('-' * 74)
    casos = [
        ({'lado': 'favorito', 'linha_min': 1.5, 'linha_max': 4.5}, (-4.5, -1.5)),
        ({'lado': 'favorito', 'linha_max': 4.5}, (-4.5, None)),
        ({'lado': 'favorito', 'linha_min': 1.5}, (None, -1.5)),
        ({'lado': 'zebra', 'linha_min': 5.5, 'linha_max': 9.5}, (5.5, 9.5)),
    ]
    for entrada, esperado in casos:
        r = C.converter(dict(entrada, janela='Todas', wr_min=0.7))
        got = (r.get('linha_min'), r.get('linha_max'))
        print(f'{ok(got == esperado)}{str(entrada):<52} -> {got}')

    # ------------------------------------------ 3. o lote no dado de verdade
    print('\n' + '-' * 74)
    print(' 3. LOTE — converte o garimpo inteiro sem perder nada?')
    print('-' * 74)
    linhas, recusadas = C.converter_lote(g.to_dict('records'))
    print(f'{ok(len(linhas) > 0)}{len(linhas):,} convertidas · {len(recusadas):,} recusadas')
    if recusadas:
        from collections import Counter
        for mot, n in Counter(r['motivo'][:60] for r in recusadas).most_common(4):
            print(f'      {n:>5}x  {mot}')
    faltando = [l for l in linhas if 'nome' not in l or not l['nome']]
    print(f'{ok(not faltando)}todas ganharam nome legivel')

    # --------------------------------- 4. bate com o snapshot da esteira? ---
    print('\n' + '-' * 74)
    print(' 4. SNAPSHOT — a linha convertida atravessa o montar_snapshot?')
    print('-' * 74)
    try:
        E = carregar('est', 'esteira_job.py')
        bons = 0
        for l in linhas[:200]:
            s = E.montar_snapshot(l, 'bet365', 'nba2k')
            if isinstance(s, dict) and s.get('filtros') is not None:
                bons += 1
        print(f'{ok(bons == min(200, len(linhas)))}{bons} de {min(200, len(linhas))} '
              f'viraram snapshot valido')
        ex = E.montar_snapshot(linhas[0], 'bet365', 'nba2k')
        print(f'      exemplo: {linhas[0]["nome"]}')
        print(f'      filtros: { {k: v for k, v in ex["filtros"].items() if k != "filtrosHistAdicionados"} }')
    except SystemExit:
        print('      (esteira_job.py nao esta aqui — rode na pasta do tipmike_api'
              ' para conferir esta parte)')

    # ------------------------------------------------------ 5. os alertas ---
    print('\n' + '-' * 74)
    print(' 5. ALERTAS — disparam no dado que ja enganou?')
    print('-' * 74)
    m = g.copy()
    ren = {'apostas': 'ap', 'unidades': 'u', 'lucro_dd': 'ldd',
           'max_reds': 'seq_neg', 'roi_m1': 'm1', 'roi_m2': 'm2',
           'conc_alvo': 'conc3', 'n_par': 'n_alvos'}
    m = m.rename(columns={k: v for k, v in ren.items() if k in m.columns})
    for c in ('ROI', 'ap', 'WR', 'u', 'ldd', 'conc3', 'DD', 'u_dia', 'ap_dia'):
        if c in m.columns:
            m[c] = pd.to_numeric(m[c], errors='coerce')
    if a.holdout and os.path.isfile(a.holdout):
        h = pd.read_csv(a.holdout, low_memory=False)
        K = ['janela', 'wr_min', 'wr_max', 'janela2', 'op2', 'wr2', 'conf_min',
             'conf_max', 'linha_min', 'linha_max', 'odd_min', 'odd_max', 'extra', 'teto']
        def nrm(v):
            s = str(v).strip()
            if s in ('-', 'nan', 'None', ''):
                return '-'
            try:
                return f'{float(s):.4f}'
            except ValueError:
                return s
        def key(d):
            return d[K].astype(str).apply(lambda c: c.map(nrm)).agg('|'.join, axis=1)
        m['_k'] = key(m); h['_k'] = key(h)
        h = h.rename(columns={'ROI': 'ROI_ho', 'apostas': 'ap_ho'})
        m = m.drop_duplicates('_k').merge(
            h[['_k', 'ROI_ho', 'ap_ho']].drop_duplicates('_k'), on='_k', how='left')
        print(f'      holdout cruzado em {int(m.ROI_ho.notna().sum()):,} configs')

    crit = a.criterio if a.criterio in m.columns else 'ROI'
    escolhidas = m.nlargest(a.top, crit).to_dict('records')
    alertas, veredito, resumo = S.alertas(
        escolhidas, todas=m.to_dict('records'), baseline_treino=a.baseline)
    print(f'\n      escolhendo as {a.top} melhores por "{crit}":')
    print(f'      => {veredito.upper()} — {resumo}\n')
    for al in alertas:
        print(f"{ok(al['ok'])}{al['pergunta']:<44} {al['valor']}")
        print(f"        {al['detalhe'][:96]}")
        if al['o_que_fazer']:
            print(f"        FAZER: {al['o_que_fazer'][:96]}")

    # ------------------------------------------------------- 6. a planilha --
    print('\n' + '-' * 74)
    print(' 6. PLANILHA — sai no formato que a esteira le?')
    print('-' * 74)
    sel, rec_sel = C.converter_lote(escolhidas)
    # AQUI eu quase repeti o erro do dia: `converter_lote` devolve as recusadas
    # e a primeira versao deste teste jogava fora com `_`. Resultado: 30
    # escolhidas viravam 11 linhas na planilha e ninguem via as 19 que
    # sumiram. Recusa TEM que ser visivel — inclusive pra quem escreve o teste.
    if rec_sel:
        print(f'{ok(False)}{len(rec_sel)} das {len(escolhidas)} escolhidas NAO '
              f'sao reproduziveis no motor:')
        from collections import Counter
        for mot, n in Counter(r['motivo'][:66] for r in rec_sel).most_common(4):
            print(f'      {n:>3}x  {mot}')
        print(f'      -> a planilha sai com {len(sel)}, nao {len(escolhidas)}. '
              f'Na tela isso vira aviso, nao surpresa.')
    cols = ['nome', 'grupo', 'mercado', 'casa', 'esporte', 'chip_janela',
            'chip_wr_min', 'chip_wr_max', 'chip_conf', 'chip_conf_max',
            'chip2_janela', 'chip2_wr_min', 'linha_min', 'linha_max',
            'odd_min', 'folga_min', 'folga_max', 'tot_env_min', 'tot_env_max',
            'atropelo_min', 'atropelo_max', 'teto', 'evitar_linhas_seq', 'variar']
    df = pd.DataFrame(sel).reindex(columns=cols)
    df.to_excel(a.saida, index=False)
    print(f'{ok(True)}{len(df)} linhas em {a.saida}')
    print(f'      colunas preenchidas: '
          f'{[c for c in cols if df[c].notna().any()]}')
    print('\n      as 5 primeiras:')
    for n in df.nome.head(5):
        print(f'        {n}')

    print('\n' + '=' * 74)
    print(' Se tudo acima deu OK, as duas pecas estao prontas pro endpoint.')
    print('=' * 74)


if __name__ == '__main__':
    sys.exit(main())
