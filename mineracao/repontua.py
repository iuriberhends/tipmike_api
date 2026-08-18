# -*- coding: utf-8 -*-
"""
===============================================================================
 REPONTUA — reaproveita um garimpo do varredor e RECALCULA tudo
===============================================================================
 Por que existe: a busca (--modo total) leva horas; a CONTA de cada config e
 barata. Se o insumo dos chips estava errado (ex.: vazamento de fuso), nao
 precisa re-garimpar — basta re-pontuar as MESMAS combinacoes sobre as
 apostas corrigidas. Reaproveita 100% do tempo de busca.

 Uso:
   python repontua.py --garimpo garimpo_v6.xlsx --apostas ap_limpo.parquet \
                      --out garimpo_v6_CORRIGIDO.xlsx
===============================================================================
"""
import argparse
import numpy as np
import pandas as pd

JAN = {'Últ. 10': 'Últ. 10', 'Últ. 20': 'Últ. 20', 'Últ. 30': 'Últ. 30',
       'Últ. 50': 'Últ. 50', 'Todas': 'Todas'}


def _n(v):
    try:
        f = float(str(v).replace(',', '.'))
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def preparar(ap: pd.DataFrame) -> dict:
    """Colunas cruas em vetores numpy — tudo o que as mascaras precisam."""
    d = {}
    d['u'] = pd.to_numeric(ap['Lucro/Prej.'], errors='coerce').values
    d['green'] = ap['Resultado'].astype(str).str.lower().eq('green').values
    ts = pd.to_datetime(ap['Data'].astype(str) + ' ' + ap['Hora'].astype(str),
                        dayfirst=True)
    d['ts'] = ts.values
    d['lin'] = pd.to_numeric(ap['Linha'], errors='coerce').values
    d['odd'] = pd.to_numeric(ap['Odd'], errors='coerce').values
    pe = ap['Placar Envio'].astype(str).str.split('-', expand=True)
    pa = pd.to_numeric(pe[0], errors='coerce')
    pb = pd.to_numeric(pe[1], errors='coerce')
    nick = ap['Tip'].astype(str).str.extract(r'\(([^()]+)\)\s*\(')[0] \
        .str.upper().str.strip()
    eA = (nick == ap['Jogador A'].astype(str).str.upper().str.strip()).values
    d['folga'] = np.abs(d['lin']) - np.where(eA, (pb - pa).values,
                                             (pa - pb).values)
    d['tot_env'] = (pa + pb).values
    d['momento'] = d['tot_env']          # alias: garimpo antigo dizia 'momento'
    # v2: os demais eixos derivados do varredor. Sem eles, config com
    # `desloc<=-5` era pontuada COMO SE O FILTRO NAO EXISTISSE — o numero de
    # holdout saia de outra config, calado.
    d['dif'] = np.abs(pa - pb).values
    _ts = pd.to_datetime(ap['Data'].astype(str) + ' ' + ap['Hora'].astype(str),
                         dayfirst=True, errors='coerce')
    _o4 = np.argsort(_ts.values, kind='stable')
    _s4 = pd.DataFrame({'c': ap['Confronto'].astype(str).values[_o4],
                        't': _ts.values[_o4]})
    _g4 = _s4.groupby('c')['t'].diff().dt.total_seconds().div(60).fillna(9e9)
    _b4 = (_g4 > 240).groupby(_s4['c']).cumsum().astype(str)
    _ev4 = np.empty(len(ap), object)
    _ev4[_o4] = (_s4['c'] + '|' + _b4).values
    _pr = (pd.DataFrame({'ev': _ev4, 't': _ts.values, 'lin': d['lin']})
           .sort_values('t', kind='stable').drop_duplicates('ev')
           .set_index('ev')['lin'])
    d['lin_ini'] = pd.Series(_ev4).map(_pr).astype(float).values
    d['desloc'] = d['lin'] - d['lin_ini']
    d['atropelo'] = np.full(len(ap), np.nan)     # preenchido abaixo, com `ev`
    for j in JAN:
        d[j] = (pd.to_numeric(ap[j], errors='coerce').values
                if j in ap.columns else np.full(len(ap), np.nan))
    d['qtd'] = pd.to_numeric(ap['Qtd Todas'], errors='coerce').values
    # jogo: event_id quando existir; senao Confronto + intervalo de 45min
    # (mesma regra do varredor — sem isso o TETO conta degrau errado)
    if 'event_id' in ap.columns:
        ev = ap['event_id'].astype(str).values
    else:
        _o = np.argsort(ts.values, kind='stable')
        _s = pd.DataFrame({'c': ap['Confronto'].astype(str).values[_o],
                           't': ts.values[_o]})
        _gap = _s.groupby('c')['t'].diff().dt.total_seconds().div(60).fillna(9e9)
        _blk = (_gap > 45).groupby(_s['c']).cumsum().astype(str)
        ev = np.empty(len(ap), object)
        ev[_o] = (_s['c'] + '|' + _blk).values
    d['ev_cod'] = pd.factorize(ev)[0]
    # ATROPELO: % dos jogos ANTERIORES do jogador com |margem| >= 15; vale o
    # PIOR dos dois; menos de 6 jogos usa a media corrente da liga. Mesma
    # regra do varredor — se divergir, o holdout mede outra coisa.
    try:
        _pf = ap['Placar Final'].astype(str).str.extract(r'(\d+)\s*[-x:]\s*(\d+)')
        _mg = (pd.to_numeric(_pf[0], errors='coerce')
               - pd.to_numeric(_pf[1], errors='coerce')).abs()
        _jg = (pd.DataFrame({'j': ev, 't': d['ts'],
                             'A': ap['Jogador A'].astype(str).str.upper().str.strip(),
                             'B': ap['Jogador B'].astype(str).str.upper().str.strip(),
                             'm': _mg}).dropna(subset=['m'])
               .sort_values('t', kind='stable').drop_duplicates('j'))
        _nn, _bb, _tx = {}, {}, {}
        _tn = _tb = 0
        for _j, _A, _B, _m in zip(_jg.j.values, _jg.A.values, _jg.B.values, _jg.m.values):
            _lig = (_tb / _tn) if _tn >= 30 else 0.11
            _r = []
            for _p in (_A, _B):
                _q = _nn.get(_p, 0)
                _r.append((_bb.get(_p, 0) / _q) if _q >= 6 else _lig)
            _tx[_j] = max(_r) * 100.0
            _at = 1 if _m >= 15 else 0
            for _p in (_A, _B):
                _nn[_p] = _nn.get(_p, 0) + 1
                _bb[_p] = _bb.get(_p, 0) + _at
            _tn += 1
            _tb += _at
        d['atropelo'] = pd.Series(ev).map(_tx).astype(float).values
    except Exception as _e:
        print(f'  aviso: nao calculei o atropelo ({_e}) — configs desse eixo serao PULADAS')
    ordem = np.argsort(d['ts'], kind='stable')
    for k in list(d):
        d[k] = d[k][ordem]
    d['n'] = len(ap)
    d['nj_tot'] = int(d['ev_cod'].max()) + 1
    fim = pd.Timestamp(d['ts'].max()).normalize() + pd.Timedelta(days=1)
    d['ini_3d'] = np.datetime64(fim - pd.Timedelta(days=3))
    d['ini_7d'] = np.datetime64(fim - pd.Timedelta(days=7))
    d['meio'] = d['ts'][d['n'] // 2]
    d['dias'] = max((pd.Timestamp(d['ts'].max())
                     - pd.Timestamp(d['ts'].min())).days + 1, 1)
    return d


class ExtraDesconhecido(Exception):
    """Eixo que este script nao sabe calcular. NUNCA ignorar: pontuar a config
    sem o filtro dela devolve o numero de OUTRA config, e ninguem percebe."""


def _extra_mask(extra: str, D: dict) -> np.ndarray:
    e = str(extra).strip()
    if e in ('-', 'nan', ''):
        return None
    for campo in ('folga', 'tot_env', 'momento', 'desloc', 'lin_ini', 'dif',
                  'atropelo'):
        if not e.startswith(campo):
            continue
        v = D[campo]
        resto = e[len(campo):].strip()
        if resto.startswith('>='):
            return v >= float(resto[2:])
        if resto.startswith('<='):
            return v <= float(resto[2:])
        if '~' in resto:                       # faixa "a~b"
            a, b = resto.split('~')
            return (v >= float(a)) & (v <= float(b))
    raise ExtraDesconhecido(e)


def mascara(cfg, D: dict):
    m = np.ones(D['n'], bool)
    jan = str(cfg['janela']).strip()
    if jan in JAN:
        v = D[jan]
        wmin, wmax = _n(cfg['wr_min']), _n(cfg['wr_max'])
        if wmin is not None:
            m &= np.nan_to_num(v, nan=-1) >= wmin
        if wmax is not None:
            m &= np.nan_to_num(v, nan=2) <= wmax
    jan2 = str(cfg['janela2']).strip()
    if jan2 in JAN:
        v2, w2 = D[jan2], _n(cfg['wr2'])
        if w2 is not None:
            m &= (np.nan_to_num(v2, nan=2) <= w2 if str(cfg['op2']).strip() == '<='
                  else np.nan_to_num(v2, nan=-1) >= w2)
    cmin, cmax = _n(cfg['conf_min']), _n(cfg['conf_max'])
    if cmin:
        m &= np.nan_to_num(D['qtd'], nan=-1) >= cmin
    if cmax is not None:
        m &= np.nan_to_num(D['qtd'], nan=1e9) <= cmax
    for k, campo, op in (('linha_min', 'lin', 'ge'), ('linha_max', 'lin', 'le'),
                         ('odd_min', 'odd', 'ge'), ('odd_max', 'odd', 'le')):
        v = _n(cfg[k])
        if v is None:
            continue
        m &= (D[campo] >= v) if op == 'ge' else (D[campo] <= v)
    ex = _extra_mask(cfg['extra'], D)
    if ex is not None:
        m &= ex
    teto = _n(cfg['teto'])
    if teto and teto > 0:
        # escada RECALCULADA pos-mascara — MESMA rotina do varredor
        # (degrau_no_indice): ordena por JOGO com sort estavel (blocos
        # contiguos preservando o tempo) e conta a posicao dentro do bloco.
        # Contar direto na ordem temporal esta ERRADO: jogos simultaneos se
        # intercalam e o contador reinicia no meio do jogo.
        idx = np.flatnonzero(m)
        if idx.size:
            gs = D['ev_cod'][idx]
            o = np.argsort(gs, kind='stable')
            gso = gs[o]
            novo = np.empty(gso.size, bool)
            novo[0] = True
            novo[1:] = gso[1:] != gso[:-1]
            arr = np.arange(gso.size)
            pos_o = arr - np.maximum.accumulate(np.where(novo, arr, 0))
            deg = np.empty(gso.size, np.int64)
            deg[o] = pos_o
            m = np.zeros(D['n'], bool)
            m[idx[deg < int(teto)]] = True
    return m


def metricas(m: np.ndarray, D: dict) -> dict:
    n = int(m.sum())
    if n == 0:
        return {'apostas': 0}
    u, g, ts = D['u'][m], D['green'][m], D['ts'][m]
    G = int(g.sum())
    tot = float(np.nansum(u))
    cum = np.nancumsum(u)
    dd = float(np.max(np.maximum.accumulate(cum) - cum))
    ev = D['ev_cod'][m]
    ju = np.bincount(ev, weights=u, minlength=D['nj_tot'])
    jc = np.bincount(ev, minlength=D['nj_tot'])
    soma_jogo = ju[jc > 0]
    nj = int(soma_jogo.size)
    sd = float(soma_jogo.std(ddof=1)) if nj > 4 else 0.0
    z = round(float(soma_jogo.mean() / (sd / np.sqrt(nj))), 2) if sd > 0 else None
    corte = int(n * 0.7)
    tr, cg = u[:corte], u[corte:]
    m1, m2 = ts < D['meio'], ts >= D['meio']
    r = lambda x: round(float(np.nansum(x)) / max(len(x), 1) * 100, 2)
    out = {'apostas': n, 'jogos': nj, 'G': G, 'R': n - G,
           'WR': round(G / n * 100, 1), 'unidades': round(tot, 2),
           'ROI': round(tot / n * 100, 2), 'u_dia': round(tot / D['dias'], 2),
           'DD': round(dd, 1),
           'lucro_dd': round(tot / dd, 2) if dd > 0 else None,
           'z_jogo': z, 'roi_m1': r(u[m1]), 'roi_m2': r(u[m2]),
           'roi_treino': r(tr), 'roi_cego': r(cg) if len(cg) >= 10 else None,
           'ap_cego': int(len(cg))}
    if out['roi_cego'] is not None:
        out['desvio_cego'] = round(out['roi_cego'] - out['roi_treino'], 2)
    for w, ini in ((3, D['ini_3d']), (7, D['ini_7d'])):
        f = ts >= ini
        uw, gw = u[f], g[f]
        out[f'ap_{w}d'] = int(f.sum())
        out[f'G_{w}d'] = int(gw.sum())
        out[f'R_{w}d'] = int(f.sum() - gw.sum())
        out[f'u_{w}d'] = round(float(np.nansum(uw)), 2)
        out[f'roi_{w}d'] = r(uw) if f.sum() >= 10 else None
    out['vivo'] = 1 if (out['ap_7d'] >= 10
                        and all((out[f'ap_{w}d'] < 10) or (out[f'u_{w}d'] > 0)
                                for w in (3, 7))) else 0
    out['queda_ponta'] = (round(out['roi_3d'] - out['roi_m2'], 2)
                          if out.get('roi_3d') is not None else None)
    return out


def barra_sorte(D: dict, n_boot=400, seed=7):
    """Teto de sorte por numero de JOGOS (bootstrap no universo)."""
    rng = np.random.default_rng(seed)
    soma = np.bincount(D['ev_cod'], weights=D['u'], minlength=D['nj_tot'])
    cnt = np.bincount(D['ev_cod'], minlength=D['nj_tot'])
    tam = np.array([20, 40, 80, 150, 300, 600, 1200, 2400])
    p95 = []
    for k in tam:
        k = min(k, len(soma))
        idx = rng.integers(0, len(soma), size=(n_boot, k))
        rois = soma[idx].sum(1) / np.maximum(cnt[idx].sum(1), 1) * 100
        p95.append(float(np.percentile(rois, 95)))
    return tam, np.array(p95)


def main():
    ap_ = argparse.ArgumentParser()
    ap_.add_argument('--garimpo', required=True)
    ap_.add_argument('--apostas', required=True)
    ap_.add_argument('--out', default='garimpo_CORRIGIDO.xlsx')
    ap_.add_argument('--de', default=None,
                     help='so pontua apostas A PARTIR desta data (AAAA-MM-DD). '
                          'Use com a data do --ate do garimpo: assim voce mede '
                          'as configs NO HOLDOUT que a busca nunca viu')
    ap_.add_argument('--ate', default=None, help='so pontua apostas ate esta data')
    ap_.add_argument('--aba', default='TUDO')
    ap_.add_argument('--topo', type=int, default=800)
    a = ap_.parse_args()

    cfgs = (pd.read_excel(a.garimpo, sheet_name=a.aba)
            if a.garimpo.lower().endswith(('.xlsx', '.xlsm'))
            else pd.read_csv(a.garimpo, low_memory=False))
    ap = (pd.read_parquet(a.apostas) if a.apostas.lower().endswith('.parquet')
          else (pd.read_excel(a.apostas)
                if a.apostas.lower().endswith(('.xlsx', '.xlsm'))
                else pd.read_csv(a.apostas, low_memory=False)))
    if a.de or a.ate:
        _ts = pd.to_datetime(ap['Data'].astype(str) + ' ' + ap['Hora'].astype(str),
                             dayfirst=True, errors='coerce')
        _n0 = len(ap)
        if a.de:
            ap = ap[_ts >= pd.Timestamp(a.de)]
        if a.ate:
            ap = ap[_ts < pd.Timestamp(a.ate) + pd.Timedelta(days=1)]
        ap = ap.reset_index(drop=True)
        print(f'janela: {len(ap):,} de {_n0:,} apostas ({a.de or "inicio"} a '
              f'{a.ate or "fim"}) — HOLDOUT se a busca usou --ate {a.de}')
    D = preparar(ap)
    print(f'{len(cfgs):,} configs x {D["n"]:,} apostas — recalculando...')
    tam, p95 = barra_sorte(D)

    chaves = ['passe', 'janela', 'wr_min', 'wr_max', 'janela2', 'op2', 'wr2',
              'conf_min', 'conf_max', 'linha_min', 'linha_max', 'odd_min',
              'odd_max', 'lado', 'extra', 'teto']
    base = cfgs[chaves].to_dict('records')
    linhas = []
    pulados = {}
    for i, cfg in enumerate(base):
        try:
            m = mascara(cfg, D)
        except ExtraDesconhecido as e:
            pulados[str(e)] = pulados.get(str(e), 0) + 1
            continue
        r = metricas(m, D)
        if r['apostas'] == 0:
            continue
        r['acima_sorte'] = round(r['ROI'] - float(np.interp(r['jogos'], tam, p95)), 2)
        linhas.append({**cfg, **r})
        if (i + 1) % 20000 == 0:
            print(f'  {i + 1:,}...')
    if pulados:
        tot = sum(pulados.values())
        print(f'\n  ATENCAO: {tot:,} configs PULADAS por eixo nao calculavel '
              f'(melhor faltar que mentir):')
        for k, v in sorted(pulados.items(), key=lambda x: -x[1])[:8]:
            print(f'    {v:>6,}x  {k}')
    R = pd.DataFrame(linhas)
    print(f'{len(R):,} configs com apostas > 0')

    aprov = ((R.acima_sorte > 0) & (R.z_jogo >= 2) & (R.roi_m1 > 0)
             & (R.roi_m2 > 0) & (R.roi_cego > 0))
    R['aprovada'] = aprov.astype(int)
    cols = (['aprovada', 'passe', 'janela', 'wr_min', 'wr_max', 'janela2', 'op2',
             'wr2', 'conf_min', 'conf_max', 'linha_min', 'linha_max', 'odd_min',
             'odd_max', 'extra', 'teto', 'apostas', 'jogos', 'G', 'R', 'WR',
             'unidades', 'ROI', 'vivo', 'queda_ponta', 'roi_3d', 'ap_3d',
             'G_3d', 'R_3d', 'u_3d', 'roi_7d', 'ap_7d', 'u_7d', 'DD', 'lucro_dd',
             'u_dia', 'z_jogo', 'roi_m1', 'roi_m2', 'roi_treino', 'roi_cego',
             'desvio_cego', 'acima_sorte'])
    cols = [c for c in cols if c in R.columns]
    VIV = R[(R.aprovada == 1) & (R.vivo == 1)].sort_values(
        ['roi_3d', 'ROI'], ascending=False, na_position='last')
    SNI = R[(R.aprovada == 1) & (R.apostas >= 150)].sort_values(
        'WR', ascending=False)
    with pd.ExcelWriter(a.out) as w:
        VIV.head(a.topo)[cols].to_excel(w, sheet_name='VIVAS_AGORA', index=False)
        SNI.head(a.topo)[cols].to_excel(w, sheet_name='SNIPERS', index=False)
        R[R.aprovada == 1].nlargest(a.topo, 'unidades')[cols].to_excel(
            w, sheet_name='POR_UNIDADES', index=False)
        R[R.aprovada == 1].nlargest(a.topo, 'lucro_dd')[cols].to_excel(
            w, sheet_name='POR_LUCRO_DD', index=False)
    R[cols].to_csv(a.out.replace('.xlsx', '_tudo.csv'), index=False)
    print(f'aprovadas: {int(aprov.sum()):,} | vivas: {len(VIV):,}')
    print(f'salvo: {a.out} (+ _tudo.csv)')


if __name__ == '__main__':
    main()
