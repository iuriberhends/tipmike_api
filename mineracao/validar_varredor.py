# -*- coding: utf-8 -*-
"""
===============================================================================
 VALIDAR_VARREDOR — prova, com numero, que o garimpo bate com o backtest
===============================================================================
 Responde as 3 perguntas, uma por vez, com VEREDITO PASSOU/FALHOU:

   T1  LIQUIDACAO — o export do motor esta certo?
       Recalcula green/red de cada aposta pelo Placar Final + linha e
       compara com a coluna Resultado. Se falhar, o problema e o MOTOR,
       nao o varredor — para tudo aqui.

   T2  LEITURA — o varredor aplica os filtros certos nos jogos certos?
       Sorteia N configs do garimpo e recalcula CADA UMA por um caminho
       independente (mascara montada do zero sobre o mesmo export).
       apostas, G, R e unidades tem que bater CASA A CASA. Qualquer
       divergencia aponta o eixo culpado (chip, conf, linha, odd,
       complementar ou teto).

   T3  COBERTURA — ele testa tudo mesmo?
       Para cada eixo, compara os valores que EXISTEM no dado com os
       valores que o garimpo REALMENTE testou. Mostra buraco de grade.

   T4  (opcional, com --parquet) FONTE — o modo tick reproduz o motor?
       So faz sentido no modo tick; use varredura.py --paridade.

 Uso:
   python validar_varredor.py --export job_201.xlsx --garimpo garimpo_tudo.csv
   python validar_varredor.py --export job_201.xlsx --garimpo g.xlsx --aba TUDO --amostra 500
===============================================================================
"""
import argparse
import sys

import numpy as np
import pandas as pd

JAN = ['Últ. 10', 'Últ. 20', 'Últ. 30', 'Últ. 50', 'Últ. 100', 'Todas']


def _n(v):
    try:
        f = float(str(v).replace(',', '.'))
        return None if f != f else f
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ preparo --
def preparar(ap: pd.DataFrame) -> dict:
    d = {}
    ap = ap.copy()
    ap.columns = [str(c).strip() for c in ap.columns]
    d['u'] = pd.to_numeric(ap['Lucro/Prej.'], errors='coerce').values
    res = ap['Resultado'].astype(str).str.strip().str.lower()
    d['green'] = res.eq('green').values
    ts = pd.to_datetime(ap['Data'].astype(str) + ' ' + ap['Hora'].astype(str),
                        dayfirst=True, errors='coerce')
    d['ts'] = ts.values
    ex = ap['Tip'].astype(str).str.extract(
        r'\(([^()]+)\)\s*\(([+-]?\d+(?:[.,]\d+)?)\)\s*$')
    nick = ex[0].str.upper().str.strip()
    lin = pd.to_numeric(ex[1].str.replace(',', '.', regex=False)
                        .str.replace('+', '', regex=False), errors='coerce')
    if 'Linha' in ap.columns:
        lin = lin.fillna(pd.to_numeric(ap['Linha'], errors='coerce'))
    d['lin'] = lin.values
    d['odd'] = pd.to_numeric(ap['Odd'], errors='coerce').values
    pe = ap['Placar Envio'].astype(str).str.extract(r'(\d+)\s*[-x:]\s*(\d+)')
    pa, pb = pd.to_numeric(pe[0], errors='coerce'), pd.to_numeric(pe[1], errors='coerce')
    eA = (nick == ap['Jogador A'].astype(str).str.upper().str.strip()).values
    d['eA'] = eA
    d['folga'] = np.abs(d['lin']) - np.where(eA, (pb - pa).values, (pa - pb).values)
    d['momento'] = (pa + pb).values
    # v2: MERCADO. A liquidacao do handicap (placar do lado + linha vs o outro)
    # NAO vale pra total de pontos (soma dos dois vs linha). Aplicar a regra
    # errada da ~50% de divergencia — parece motor quebrado e nao e.
    _txt = ' '.join(ap.get('Mercado', pd.Series(['']*len(ap))).astype(str).head(80)) \
        + ' ' + ' '.join(ap['Tip'].astype(str).head(80))
    _t = _txt.lower()
    d['mercado'] = ('total' if any(k in _t for k in
                                   ('over', 'under', 'mais de', 'menos de', 'total'))
                    else 'handicap')
    tip_l = ap['Tip'].astype(str).str.lower()
    d['under'] = (tip_l.str.contains('under') | tip_l.str.contains('menos')).values
    pf = ap['Placar Final'].astype(str).str.extract(r'(\d+)\s*[-x:]\s*(\d+)')
    d['fa'] = pd.to_numeric(pf[0], errors='coerce').values
    d['fb'] = pd.to_numeric(pf[1], errors='coerce').values
    for j in JAN:
        d[j] = (pd.to_numeric(ap[j], errors='coerce').values
                if j in ap.columns else np.full(len(ap), np.nan))
    qcols = [c for c in ap.columns if str(c).lower().startswith('qtd')]
    qmax, qbest = -1, None
    for c in qcols:
        v = pd.to_numeric(ap[c], errors='coerce')
        if v.max(skipna=True) is not None and float(v.max(skipna=True) or 0) > qmax:
            qmax, qbest = float(v.max(skipna=True)), v.values
    d['qtd'] = qbest if qbest is not None else np.full(len(ap), 1e9)
    d['qtd_col'] = ([c for c in qcols
                     if float(pd.to_numeric(ap[c], errors='coerce')
                              .max(skipna=True) or 0) == qmax] or ['-'])[0]
    if 'event_id' in ap.columns:
        jog = ap['event_id'].astype(str).values
        d['jogo_fonte'] = 'event_id'
    else:                                  # sem event_id: par + intervalo 45min
        conf = ap['Confronto'].astype(str).values
        o = np.argsort(ts.values, kind='stable')
        s = pd.DataFrame({'c': conf[o], 't': ts.values[o]})
        gap = s.groupby('c')['t'].diff().dt.total_seconds().div(60).fillna(9e9)
        blk = (gap > 45).groupby(s['c']).cumsum().astype(str)
        jog_o = (s['c'] + '|' + blk).values
        jog = np.empty(len(ap), object)
        jog[o] = jog_o
        d['jogo_fonte'] = 'Confronto + intervalo 45min'
    d['ev'] = pd.factorize(jog)[0]
    # ATROPELO (v16 do varredor): % dos jogos ANTERIORES do jogador que
    # terminaram com |diferenca| >= 15; vale o PIOR dos dois. So jogos ja
    # encerrados antes da aposta entram (o proprio jogo NUNCA), e jogador com
    # menos de 6 jogos usa a media corrente da liga. Mesma regra do varredor —
    # se divergir aqui, o T2 acusa erro que nao existe.
    d['atropelo'] = np.nan
    try:
        _pf2 = ap['Placar Final'].astype(str).str.extract(r'(\d+)\s*[-x:]\s*(\d+)')
        _mg = (pd.to_numeric(_pf2[0], errors='coerce')
               - pd.to_numeric(_pf2[1], errors='coerce')).abs()
        _jg = (pd.DataFrame({'j': jog, 't': ts.values,
                             'A': ap['Jogador A'].astype(str).str.upper().str.strip(),
                             'B': ap['Jogador B'].astype(str).str.upper().str.strip(),
                             'm': _mg})
               .dropna(subset=['m']).sort_values('t', kind='stable')
               .drop_duplicates('j'))
        _n, _b, _taxa = {}, {}, {}
        _tn = _tb = 0
        for _j, _A, _B, _m in zip(_jg.j.values, _jg.A.values, _jg.B.values, _jg.m.values):
            _lig = (_tb / _tn) if _tn >= 30 else 0.11
            _r = []
            for _p in (_A, _B):
                _np_ = _n.get(_p, 0)
                _r.append((_b.get(_p, 0) / _np_) if _np_ >= 6 else _lig)
            _taxa[_j] = max(_r) * 100.0
            _ate = 1 if _m >= 15 else 0
            for _p in (_A, _B):
                _n[_p] = _n.get(_p, 0) + 1
                _b[_p] = _b.get(_p, 0) + _ate
            _tn += 1
            _tb += _ate
        d['atropelo'] = pd.Series(jog).map(_taxa).astype(float).values
    except Exception:
        pass
    # eixos derivados de TOTAL DE PONTOS — mesma regra do varredor:
    # lin_ini = 1a linha ofertada no jogo (chave = event_id, ou Confronto+4h)
    d['dif'] = np.abs((pa - pb).values)
    try:
        if 'event_id' in ap.columns:
            chave = ap['event_id'].astype(str)
        else:
            chave = (ap['Confronto'].astype(str) + '|'
                     + ts.dt.floor('4h').astype(str))
        ordem_t = np.argsort(ts.values, kind='stable')
        prim = {}
        for k, lv in zip(chave.values[ordem_t], d['lin'][ordem_t]
                         if len(d['lin']) == len(ap) else lin.values[ordem_t]):
            if k not in prim and np.isfinite(lv):
                prim[k] = lv
        d['lin_ini'] = chave.map(prim).astype(float).values
        d['desloc'] = lin.values - d['lin_ini']
    except Exception:
        d['lin_ini'] = np.full(len(ap), np.nan)
        d['desloc'] = np.full(len(ap), np.nan)
    ordem = np.argsort(d['ts'], kind='stable')
    for k in list(d):
        if isinstance(d[k], np.ndarray) and d[k].shape[0] == len(ap):
            d[k] = d[k][ordem]
    d['n'] = len(ap)
    return d


# ---------------------------------------------------------------- mascaras --
def _extra(e, D):
    e = str(e).strip()
    if e in ('-', 'nan', ''):
        return None
    for campo in ('folga', 'momento', 'desloc', 'lin_ini', 'dif', 'atropelo'):
        if e.startswith(campo):
            v, r = D[campo], e[len(campo):].strip()
            if r.startswith('>='):
                return v >= float(r[2:])
            if r.startswith('<='):
                return v <= float(r[2:])
            if '~' in r:
                a, b = r.split('~')
                return (v >= float(a)) & (v <= float(b))
    return 'DESCONHECIDO'


def mascara(cfg, D):
    """Monta a cesta do zero. Devolve (mask, motivo_de_erro_ou_None)."""
    m = np.ones(D['n'], bool)
    jan = str(cfg.get('janela', '-')).strip()
    if jan in JAN:
        v = D[jan]
        if not np.isfinite(v).any():
            return None, f'coluna de chip ausente no export: {jan}'
        wmin, wmax = _n(cfg.get('wr_min')), _n(cfg.get('wr_max'))
        if wmin is not None:
            m &= np.nan_to_num(v, nan=-1) >= wmin
        if wmax is not None:
            m &= np.nan_to_num(v, nan=2) <= wmax
    jan2 = str(cfg.get('janela2', '-')).strip()
    if jan2 in JAN:
        w2 = _n(cfg.get('wr2'))
        if w2 is not None:
            v2 = D[jan2]
            m &= (np.nan_to_num(v2, nan=2) <= w2
                  if str(cfg.get('op2', '')).strip() == '<='
                  else np.nan_to_num(v2, nan=-1) >= w2)
    cmin, cmax = _n(cfg.get('conf_min')), _n(cfg.get('conf_max'))
    if cmin:
        m &= np.nan_to_num(D['qtd'], nan=-1) >= cmin
    if cmax is not None:
        m &= np.nan_to_num(D['qtd'], nan=1e9) <= cmax
    for k, campo, ge in (('linha_min', 'lin', True), ('linha_max', 'lin', False),
                         ('odd_min', 'odd', True), ('odd_max', 'odd', False)):
        v = _n(cfg.get(k))
        if v is not None:
            m &= (D[campo] >= v) if ge else (D[campo] <= v)
    ex = _extra(cfg.get('extra', '-'), D)
    if isinstance(ex, str):
        return None, f'complementar nao reconhecido: {cfg.get("extra")}'
    if ex is not None:
        m &= ex
    teto = _n(cfg.get('teto'))
    if teto and teto > 0:
        idx = np.flatnonzero(m)
        if idx.size:
            gs = D['ev'][idx]
            o = np.argsort(gs, kind='stable')       # blocos por jogo, tempo dentro
            gso = gs[o]
            novo = np.empty(gso.size, bool)
            novo[0] = True
            novo[1:] = gso[1:] != gso[:-1]
            arr = np.arange(gso.size)
            pos = arr - np.maximum.accumulate(np.where(novo, arr, 0))
            deg = np.empty(gso.size, np.int64)
            deg[o] = pos
            m = np.zeros(D['n'], bool)
            m[idx[deg < int(teto)]] = True
    return m, None


# ------------------------------------------------------------------ testes --
def t1_liquidacao(D):
    print('\n' + '=' * 78)
    print(' T1  LIQUIDACAO — o export do motor fecha sozinho?')
    print('=' * 78)
    ok = np.isfinite(D['fa']) & np.isfinite(D['fb'])
    if not ok.any():
        print('  SEM Placar Final no export — teste pulado')
        return None
    if D['mercado'] == 'total':
        tot = (D['fa'] + D['fb'])[ok]
        lin = D['lin'][ok]
        und = D['under'][ok]
        esperado = np.where(und, tot < lin, tot > lin)
        push = tot == lin
        print(f'  mercado detectado: TOTAL DE PONTOS '
              f'({int((~und).sum()):,} over / {int(und.sum()):,} under) — '
              'regra: soma dos placares vs linha')
    else:
        pl = np.where(D['eA'], D['fa'], D['fb'])[ok]
        po = np.where(D['eA'], D['fb'], D['fa'])[ok]
        marg = pl + D['lin'][ok] - po
        esperado = marg > 0
        push = marg == 0
        print('  mercado detectado: HANDICAP — regra: placar do lado + linha '
              'vs placar do adversario')
    bate = esperado == D['green'][ok]
    err = int((~bate & ~push).sum())
    print(f'  apostas com placar final: {int(ok.sum()):,} | push (linha inteira): {int(push.sum())}')
    print(f'  divergencias green/red: {err}')
    if err and D['mercado'] == 'total':
        print('  DICA: em mercado de 1o TEMPO, confira se a coluna Placar Final '
              'traz o placar do TEMPO ou do jogo inteiro — e a causa mais comum '
              'de divergencia aqui.')
    print(f'  VEREDITO: {"PASSOU" if err == 0 else "FALHOU — confira a DICA acima antes de culpar o motor"}')
    return err == 0


_PCT_T2 = [0.0]          # preenchido pelo t2_leitura, lido no resumo final


def t2_leitura(D, cfgs, n_amostra, seed=7):
    print('\n' + '=' * 78)
    print(' T2  LEITURA — o varredor filtra os jogos certos?')
    print('=' * 78)
    print(f'  unidade de jogo: {D["jogo_fonte"]} | coluna de confrontos: {D["qtd_col"]}')
    rng = np.random.default_rng(seed)
    k = min(n_amostra, len(cfgs))
    am = cfgs.iloc[rng.choice(len(cfgs), k, replace=False)]
    ok_ap = ok_g = ok_u = 0
    borda = [0]
    erros, culpados = [], {}
    for _, c in am.iterrows():
        cfg = c.to_dict()
        m, motivo = mascara(cfg, D)
        if m is None:
            erros.append((motivo, cfg.get('janela'), cfg.get('extra')))
            continue
        n = int(m.sum())
        G = int(D['green'][m].sum())
        u = float(np.nansum(D['u'][m]))
        dif = abs(n - int(cfg['apostas']))
        if dif == 0:
            ok_ap += 1
        elif (dif <= max(2, 0.002 * int(cfg['apostas']))) and _n(cfg.get('teto')):
            # BORDA DE JOGO: sem event_id no export, "o que e uma partida" e
            # ESTIMADO por par + intervalo. Duas implementacoes honestas cortam
            # um jogo em lugar diferente e trocam apostas de degrau. A tolerancia
            # e PROPORCIONAL (0,2% da cesta, minimo 2): cesta de 6.000 tem mais
            # fronteiras que cesta de 200. Some de vez com event_id no export.
            borda[0] += 1
            ok_ap += 1
        else:
            eixo = ('teto' if _n(cfg.get('teto')) else
                    'chip' if str(cfg.get('janela')) in JAN else
                    'complementar' if str(cfg.get('extra')) not in ('-', 'nan') else
                    'linha/odd/conf')
            culpados[eixo] = culpados.get(eixo, 0) + 1
            if len(erros) < 8:
                erros.append((f'apostas {cfg["apostas"]} != {n} (dif {dif})',
                              cfg.get('janela'), cfg.get('extra')))
        _tg = max(2, 0.002 * int(cfg['apostas'])) if dif else 0
        if abs(G - int(cfg['G'])) <= _tg:
            ok_g += 1
        if abs(u - float(cfg['unidades'])) < (max(2.5, _tg * 1.2) if dif else 0.02):
            ok_u += 1
    print(f'  configs conferidas: {k}')
    print(f'  apostas iguais : {ok_ap}/{k} ({ok_ap / k * 100:.1f}%)'
          + (f'  — dos quais {borda[0]} sao BORDA DE JOGO (dif de 1-2 apostas '
             'no teto, por falta de event_id no export)' if borda[0] else ''))
    print(f'  greens iguais  : {ok_g}/{k}')
    print(f'  unidades iguais: {ok_u}/{k}')
    if culpados:
        print(f'  eixo suspeito nas divergencias: {culpados}')
    for e in erros[:6]:
        print(f'    - {e}')
    _PCT_T2[0] = min(ok_ap, ok_g, ok_u) / max(k, 1) * 100
    passou = ok_ap == k and ok_g == k and ok_u == k
    print(f'  VEREDITO: {"PASSOU" if passou else "FALHOU — ver eixo suspeito acima"}')
    return passou


def t3_cobertura(D, cfgs):
    print('\n' + '=' * 78)
    print(' T3  COBERTURA — ele testou tudo que existe no dado?')
    print('=' * 78)
    print('  ATENCAO: este teste le as configs SALVAS, nao a grade testada — e um'
          ' PISO.\n  Um valor pode ter sido testado e nao ter sobrevivido (config'
          ' ruim).\n  A grade REAL sai no cabecalho do varredor ("eixo de linha").')
    linhas_dado = np.unique(D['lin'][np.isfinite(D['lin'])])
    tst = pd.to_numeric(cfgs.get('linha_min'), errors='coerce').dropna().unique()
    tet = pd.to_numeric(cfgs.get('linha_max'), errors='coerce')
    tet = tet[tet < 900].dropna().unique() if tet is not None else np.array([])
    print(f'  LINHA: {len(linhas_dado)} valores no dado '
          f'({linhas_dado.min():g} a {linhas_dado.max():g})')
    if len(tst):
        print(f'    pisos  (linha >= X): {len(tst):>3} testados '
              f'({np.min(tst):g} a {np.max(tst):g})')
        falta = [x for x in linhas_dado if x > np.max(tst)]
        if falta:
            print(f'      BURACO: {len(falta)} linhas do dado acima do maior '
                  f'piso testado (a partir de {falta[0]:g})')
    if len(tet):
        print(f'    tetos  (linha <= X): {len(tet):>3} testados '
              f'({np.min(tet):g} a {np.max(tet):g})')
        falta_t = [x for x in linhas_dado if x < np.min(tet)]
        if falta_t:
            print(f'      BURACO: {len(falta_t)} linhas do dado abaixo do menor '
                  f'teto testado (ate {falta_t[-1]:g})')
    else:
        print('    tetos  (linha <= X): NENHUM testado — em mercado de TOTAL '
              'o corte que decide costuma ser o TETO, nao o piso')
    for jan in JAN:
        v = D[jan]
        if not np.isfinite(v).any():
            continue
        sub = cfgs[cfgs['janela'].astype(str) == jan]
        w = pd.to_numeric(sub.get('wr_min'), errors='coerce').dropna().unique() \
            if len(sub) else np.array([])
        print(f'  CHIP {jan:<9}: {len(sub):>7,} configs | pisos de WR testados: '
              f'{len(w):>3} ({np.min(w):.2f}–{np.max(w):.2f})' if len(w)
              else f'  CHIP {jan:<9}: NAO TESTADO (coluna existe no export!)')
    q = D['qtd'][np.isfinite(D['qtd'])]
    qt = pd.to_numeric(cfgs.get('conf_min'), errors='coerce').dropna().unique()
    if len(qt):
        print(f'  CONFRONTOS: dado vai ate {int(np.nanmax(q))} | pisos testados '
              f'{sorted(set(int(x) for x in qt))[:10]}')
    ext = cfgs['extra'].astype(str).value_counts()
    print(f'  COMPLEMENTARES testados: {[k for k in ext.index[:8] if k != "-"]}')
    tet = cfgs['teto'].astype(str).value_counts().to_dict()
    print(f'  TETO: {tet}')
    if D['mercado'] == 'total':
        print('  AVISO DE EIXO: neste mercado o complementar `folga` NAO faz '
              'sentido (e conta de handicap). O `momento` equivale ao tot_env '
              '(soma do placar no envio) e ESSE vale. Os eixos fortes de total '
              '— desloc, Gap Ult.N, linha de abertura — o varredor ainda nao '
              'calcula: o garimpo aqui e piso, nao teto.')
    print('  (T3 nao tem veredito automatico — leia os BURACOs acima)')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--export', required=True, help='planilha de apostas do job (a verdade)')
    p.add_argument('--garimpo', required=True, help='saida do varredor (csv _tudo ou xlsx)')
    p.add_argument('--aba', default='TUDO')
    p.add_argument('--amostra', type=int, default=300)
    # v3: JANELA. Garimpo feito com pre-compromisso (--ate) so enxergou o
    # TREINO; conferir contra o export INTEIRO compara coisas diferentes e o
    # T2 reprova por construcao (as contagens batem na fracao do treino).
    # Use a MESMA data que voce passou pro varredor.
    p.add_argument('--ate', default=None,
                   help='so valida apostas ate esta data (AAAA-MM-DD) — use a '
                        'mesma do --ate do varredor')
    p.add_argument('--de', default=None, help='so valida a partir desta data')
    a = p.parse_args()

    print('=' * 78)
    print(' VALIDACAO DO VARREDOR contra o motor do backtest')
    print('=' * 78)
    ap = (pd.read_excel(a.export) if a.export.lower().endswith(('.xlsx', '.xlsm'))
          else pd.read_csv(a.export, low_memory=False))
    cfgs = (pd.read_excel(a.garimpo, sheet_name=a.aba)
            if a.garimpo.lower().endswith(('.xlsx', '.xlsm'))
            else pd.read_csv(a.garimpo, low_memory=False))
    if a.ate or a.de:
        _ts = pd.to_datetime(ap['Data'].astype(str) + ' ' + ap['Hora'].astype(str),
                             dayfirst=True, errors='coerce')
        _n0 = len(ap)
        if a.de:
            ap = ap[_ts >= pd.Timestamp(a.de)]
        if a.ate:
            ap = ap[_ts < pd.Timestamp(a.ate) + pd.Timedelta(days=1)]
        ap = ap.reset_index(drop=True)
        print(f'janela: {len(ap):,} de {_n0:,} apostas '
              f'({a.de or "inicio"} a {a.ate or "fim"})')
        if ap.empty:
            print('ERRO: a janela nao deixou nenhuma aposta')
            sys.exit(2)
    print(f'export: {len(ap):,} apostas | garimpo: {len(cfgs):,} configs')
    D = preparar(ap)

    r1 = t1_liquidacao(D)
    if r1 is False:
        print('\nPARANDO: se o motor nao fecha, comparar varredor com ele nao faz sentido.')
        sys.exit(2)
    r2 = t2_leitura(D, cfgs, a.amostra)
    t3_cobertura(D, cfgs)

    print('\n' + '=' * 78)
    print(f' RESUMO: T1 liquidacao {"OK" if r1 else "pulado"} | '
          f'T2 leitura {"OK" if r2 else "FALHOU"}')
    # linha estruturada: quem chama isto de um job le daqui em vez de tentar
    # entender o texto. t1 e' fatal (o export nao fecha); t2 e' percentual.
    print(f'GATE t1={"OK" if r1 else ("PULADO" if r1 is None else "FALHOU")} '
          f't2={_PCT_T2[0]:.1f}')
    print('=' * 78)
    sys.exit(0 if (r2 and r1 is not False) else 1)


if __name__ == '__main__':
    main()
