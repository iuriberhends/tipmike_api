# -*- coding: utf-8 -*-
r"""
workers/esteira_conversor.py — garimpo -> linha de planilha.

O ELO QUE FALTAVA
=================
As tres camadas da esteira ja preveem `origem='varredura'` (o ORIGENS do
router lista, e o esteira_job diz "Itens ja criados pelo router (origem
varredura) sao respeitados"), mas ninguem traduz o garimpo. Era isso que
sobrava pra mim fazer na mao montando o estrategias.xlsx.

POR QUE DEVOLVE FORMATO DE PLANILHA, E NAO SNAPSHOT
===================================================
O `montar_snapshot()` do esteira_job ja recebe uma linha com os nomes de
coluna da planilha e produz o snapshot que o motor entende. Reusar esse
caminho significa que sentinela, controle, variacoes e hill-climb continuam
funcionando sem uma linha de mudanca. Montar o snapshot aqui criaria um
SEGUNDO lugar que precisa concordar com o motor — e divergencia entre dois
lugares que deviam concordar foi a fonte de metade dos bugs deste projeto.

O QUE ELE RECUSA (de proposito)
===============================
Eixo que o garimpo sabe cortar mas o MOTOR nao sabe aplicar vira erro
explicito, nunca silencio. `desloc`, `lin_ini` e `dif` estao nessa lista: o
varredor os calcula, o runner nao tem filtro pra eles. Deixar passar
produziria uma config que roda SEM aquele corte e entrega o numero de outra
estrategia — exatamente o bug do `_extra_mask` do repontua, que pontuava
config com `atropelo<=10` como se o filtro nao existisse.

A regra: filtro que eu nao sei aplicar nunca vira filtro que eu nao aplico.
"""
from __future__ import annotations

import re

# ------------------------------------------------------------------ janelas --
# o garimpo escreve "Últ. 10" / "Todas"; a planilha quer "last_10" / "all"
_JANELA = {
    'todas': 'all', 'all': 'all',
    'últ. 10': 'last_10', 'ult. 10': 'last_10', 'last_10': 'last_10',
    'últ. 20': 'last_20', 'ult. 20': 'last_20', 'last_20': 'last_20',
    'últ. 30': 'last_30', 'ult. 30': 'last_30', 'last_30': 'last_30',
    'últ. 50': 'last_50', 'ult. 50': 'last_50', 'last_50': 'last_50',
    'últ. 100': 'last_100', 'ult. 100': 'last_100', 'last_100': 'last_100',
}

# eixo complementar -> par de colunas (minimo, maximo) da planilha.
# `momento` do GARIMPO e' soma de placar = `tot_env` no motor; o `momento` do
# motor e' o ESTAGIO do jogo (1Q/1T/3Q). Nomes parecidos, coisas diferentes —
# por isso os dois mapeiam pra tot_env aqui.
_EIXOS = {
    'folga':    ('folga_min', 'folga_max'),
    # v23: erro da casa na linha de total (motor errAtivo/errMin/errMax)
    'err':      ('err_min', 'err_max'),
    'tot_env':  ('tot_env_min', 'tot_env_max'),
    'momento':  ('tot_env_min', 'tot_env_max'),
    'atropelo': ('atropelo_min', 'atropelo_max'),
}

# o varredor corta por estes, o motor NAO tem filtro. Recusar e' o certo.
_SEM_FILTRO_NO_MOTOR = {
    'desloc':  'deslocamento da linha desde a abertura',
    'lin_ini': 'linha de abertura do jogo',
    'dif':     'diferenca entre os winrates dos dois jogadores',
}


class ConfigNaoReproduzivel(Exception):
    """A config usa um corte que o motor nao sabe aplicar. Levantar em vez de
    deixar passar: rodar sem o corte devolve o numero de OUTRA estrategia."""


def _num(v):
    if v is None:
        return None
    s = str(v).strip().replace(',', '.')
    if s in ('', '-', 'nan', 'None', 'NaN'):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _pct(v):
    """O garimpo grava winrate como fracao (0.75); a planilha quer 75."""
    n = _num(v)
    if n is None:
        return None
    return round(n * 100, 2) if n <= 1.0 else round(n, 2)


def _janela(v):
    if v is None:
        return None
    s = str(v).strip().lower()
    return _JANELA.get(s)


def parse_extra(extra):
    """'atropelo>=36.49' -> ('atropelo_min', 36.49). Entende >=, <= e faixa
    'a~b'. Devolve lista de (coluna, valor) — a faixa vira duas."""
    e = str(extra or '').strip()
    if e in ('', '-', 'nan', 'None'):
        return []
    m = re.match(r'^([a-z_]+)\s*(>=|<=|=)?\s*(-?[\d.]+)?\s*~?\s*(-?[\d.]+)?$', e)
    if not m:
        raise ConfigNaoReproduzivel(f'não entendi o corte "{e}"')
    eixo, op, a, b = m.group(1), m.group(2), _num(m.group(3)), _num(m.group(4))
    if eixo in _SEM_FILTRO_NO_MOTOR:
        raise ConfigNaoReproduzivel(
            f'"{eixo}" ({_SEM_FILTRO_NO_MOTOR[eixo]}) o garimpo sabe cortar, '
            f'mas o motor não tem esse filtro — rodar sem ele daria o número '
            f'de outra estratégia')
    if eixo not in _EIXOS:
        raise ConfigNaoReproduzivel(f'eixo desconhecido: "{eixo}"')
    col_min, col_max = _EIXOS[eixo]
    if b is not None:                      # faixa a~b
        return [(col_min, a), (col_max, b)]
    if op == '<=':
        return [(col_max, a)]
    return [(col_min, a)]                  # >= e = viram piso


def converter(g: dict, *, casa='bet365', esporte='nba2k', mercado=None,
              nome=None, grupo='varredura') -> dict:
    """Uma linha do `_tudo.csv` -> uma linha no formato da planilha.

    Levanta ConfigNaoReproduzivel se a config usa corte que o motor nao
    aplica. Quem chama DEVE tratar (pular o item com o motivo visivel), nunca
    engolir — a config pularia o filtro e mentiria o numero.
    """
    L = {k.strip().lower(): v for k, v in g.items()}
    lin = {
        'nome': nome or '',
        'grupo': grupo,
        'casa': casa,
        'esporte': esporte,
        'evitar_linhas_seq': 0,
        'variar': 0,
    }
    lin['mercado'] = mercado or L.get('mercado') or 'ah_ft'

    # --- chips de winrate (ate dois) ---
    j1 = _janela(L.get('janela'))
    if j1:
        lin['chip_janela'] = j1
        lin['chip_wr_min'] = _pct(L.get('wr_min'))
        lin['chip_wr_max'] = _pct(L.get('wr_max'))
        cmin, cmax = _num(L.get('conf_min')), _num(L.get('conf_max'))
        if cmin:
            lin['chip_conf'] = int(cmin)
        if cmax:
            lin['chip_conf_max'] = int(cmax)
    j2 = _janela(L.get('janela2'))
    if j2:
        lin['chip2_janela'] = j2
        # op2 diz o lado: '>=' vira piso, '<=' vira teto
        if str(L.get('op2') or '>=').strip() == '<=':
            lin['chip2_wr_max'] = _pct(L.get('wr2'))
        else:
            lin['chip2_wr_min'] = _pct(L.get('wr2'))

    # --- linha e odd. No HC o LADO e' o SINAL: favorito = negativo ---
    lmin, lmax = _num(L.get('linha_min')), _num(L.get('linha_max'))
    favorito = str(L.get('lado') or '').strip().lower() == 'favorito'
    if favorito:
        # O lado do handicap e' o SINAL da linha (o motor nao tem campo
        # `lado`; foi assim que o job 1127 saiu 100% negativo). Espelhar
        # inverte E troca os extremos: [1.5, 4.5] vira [-4.5, -1.5].
        # CUIDADO com o lado ABERTO: se o garimpo trouxe so' o max, o
        # espelho vira o MIN. Tratar isso errado transformava
        # "favorito de 1,5 a 4,5" em "qualquer favorito ate -4,5" — outra
        # estrategia, com o mesmo nome.
        lmin, lmax = (None if lmax is None else -lmax), (None if lmin is None else -lmin)
    if lmin is not None:
        lin['linha_min'] = lmin
    if lmax is not None:
        lin['linha_max'] = lmax
    lin['_lado'] = 'favorito' if favorito else 'zebra'
    for a, b in (('odd_min', 'odd_min'), ('odd_max', 'odd_max')):
        v = _num(L.get(a))
        if v:
            lin[b] = v

    # --- o eixo complementar ---
    for col, val in parse_extra(L.get('extra')):
        lin[col] = val

    teto = _num(L.get('teto'))
    if teto and teto > 0:
        lin['teto'] = int(teto)

    if not lin['nome']:
        lin['nome'] = resumir(lin)      # usa _lado, por isso vem antes do pop
    lin.pop('_lado', None)
    return {k: v for k, v in lin.items() if v is not None}


def resumir(lin: dict) -> str:
    """Nome curto e legivel, pro placar e pro log."""
    p = []
    if lin.get('chip_wr_min'):
        p.append(f"{lin['chip_janela']}>={lin['chip_wr_min']:.0f}")
    if lin.get('chip2_wr_min'):
        p.append(f"{lin['chip2_janela']}>={lin['chip2_wr_min']:.0f}")
    if lin.get('chip_conf_max'):
        p.append(f"conf<={lin['chip_conf_max']}")
    lo, hi = lin.get('linha_min'), lin.get('linha_max')
    if lo is not None and hi is not None:
        a, b = sorted((abs(lo), abs(hi)))
        p.append(f"L{a:g}-{b:g}")
    elif lo is not None:
        p.append(f"L>={abs(lo):g}" if lo > 0 else f"L<={abs(lo):g}")
    elif hi is not None:
        p.append(f"L<={abs(hi):g}" if hi > 0 else f"L>={abs(hi):g}")
    for c, r in (('atropelo_min', 'atr>='), ('atropelo_max', 'atr<='),
                 ('err_min', 'err>='), ('err_max', 'err<='),
                 ('tot_env_min', 'tot>='), ('tot_env_max', 'tot<='),
                 ('folga_min', 'folga>='), ('folga_max', 'folga<=')):
        if lin.get(c) is not None:
            p.append(f"{r}{lin[c]:g}")
    if lin.get('teto'):
        p.append(f"t{lin['teto']}")
    lado = 'FAV' if lin.get('_lado') == 'favorito' else 'ZEB'
    return f"{lado} " + ' '.join(p) if p else f"{lado} escancarado"


def converter_lote(linhas, **kw):
    """Converte varias. Devolve (ok, recusadas) — recusada leva o MOTIVO,
    pra aparecer na tela em vez de sumir."""
    ok, recusadas = [], []
    for i, g in enumerate(linhas):
        try:
            ok.append(converter(g, **kw))
        except ConfigNaoReproduzivel as e:
            recusadas.append({'i': i, 'extra': g.get('extra'), 'motivo': str(e)})
    return ok, recusadas
