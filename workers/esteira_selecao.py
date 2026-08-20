# -*- coding: utf-8 -*-
r"""
workers/esteira_selecao.py — o garimpo vira lista escolhível, com desconfiança.

O QUE ESTE MÓDULO É
===================
Das nove coisas que eu fazia na mão ao receber um garimpo, três eram ranking
e SEIS eram ceticismo. Ranking o varredor já sabe fazer. O que não existia
automatizado eram as checagens que fazem dizer "não use isso" — e são elas
que evitam prejuízo. Este módulo é essa parte.

OS QUATRO ALERTAS (cada um nasceu de um erro real deste projeto)
================================================================
1. O RANKING SE SUSTENTA?
   Correlação entre o desempenho no treino e no holdout. No garimpo 10 deu
   **−0,685**: a config #1 por lucro/DD foi a PIOR fora da amostra (−17,42) e
   a #9, de menor ROI no treino, foi a melhor (+10,85). Escolher pelo topo
   teria sido pior que sortear. Correlação negativa = a ordenação não vale.

2. DEPENDE DE EIXO DERIVADO?
   `momento` deu +22,70 no treino e −4,90 no holdout, com 20% positivas.
   `lin_ini`: 13%. Configs SEM eixo derivado: 99% positivas. Um corte por
   esse campo separa quase tudo.

3. O LUCRO ESTÁ ESPALHADO?
   Numa liga de 17 jogadores o top-3 carrega mais que o lucro inteiro
   (`conc_alvo` passa de 100% — o resto é negativo). Não é estratégia, é
   whitelist com outro nome.

4. A MAGNITUDE É PLAUSÍVEL?
   O garimpo 11 prometia ROI +40,21; medido no universo completo deu +14,80.
   A diferença era o export de origem estar pré-filtrado (18% dos jogos).
   O prêmio histórico do projeto fica entre 9 e 21 pontos sobre o mercado —
   acima de 25 é suspeito por construção, não por opinião.

O PRÊMIO, E NÃO O ROI
=====================
Todo julgamento aqui é sobre **quanto rende ACIMA do mercado**, não sobre o
ROI absoluto. ROI +15% num mercado de −12% é excelente; ROI +5% num mercado
de +2% é ruído. Sem o baseline, o número não quer dizer nada — foi assim que
a BATTLE pareceu promissora por um dia inteiro.
"""
from __future__ import annotations

import math

# ---------------------------------------------------------------- limiares --
CORR_MIN = 0.0          # abaixo disso o ranking está invertido
EIXO_DERIVADO_MAX = 0.4  # até 40% das escolhidas podem depender de eixo derivado
CONC_MAX = 60.0          # top-3 carregando mais que isso = whitelist
PREMIO_SUSPEITO = 25.0   # acima disso, provável origem pré-filtrada
PREMIO_TIPICO = (9.0, 21.0)   # a faixa histórica do projeto

# eixos que o varredor cria e que historicamente não sobrevivem
DERIVADOS = ('momento', 'tot_env', 'desloc', 'lin_ini', 'dif', 'atropelo', 'folga')


def _f(v, pad=None):
    try:
        x = float(v)
        return pad if (x != x or x in (float('inf'), float('-inf'))) else x
    except (TypeError, ValueError):
        return pad


def correlacao(xs, ys):
    """Pearson, tolerante a buraco. None se não dá pra calcular."""
    par = [(a, b) for a, b in zip(xs, ys)
           if _f(a) is not None and _f(b) is not None]
    n = len(par)
    if n < 8:
        return None
    mx = sum(p[0] for p in par) / n
    my = sum(p[1] for p in par) / n
    sx = math.sqrt(sum((p[0] - mx) ** 2 for p in par))
    sy = math.sqrt(sum((p[1] - my) ** 2 for p in par))
    if sx == 0 or sy == 0:
        return None
    return sum((p[0] - mx) * (p[1] - my) for p in par) / (sx * sy)


def _check(pergunta, valor, ok, detalhe, o_que_fazer=None):
    return {'pergunta': pergunta, 'valor': valor, 'ok': bool(ok),
            'detalhe': detalhe, 'o_que_fazer': o_que_fazer}


def _pct(p, t):
    return round(p / t * 100, 1) if t else 0.0


def _mil(n):
    return f'{int(n):,}'.replace(',', '.')


# =============================================================== os alertas ==
def alertas(escolhidas, *, todas=None, baseline_treino=None,
            baseline_holdout=None):
    """`escolhidas`: as configs que o usuário selecionou (dicts com as
    métricas do garimpo). `todas`: o conjunto inteiro, para a correlação —
    medir só nas escolhidas mede a própria escolha, não o método."""
    out = []
    n = len(escolhidas)
    if not n:
        return out, 'nao_use', 'Nenhuma estratégia selecionada.'

    # --- 1. o ranking se sustenta fora da amostra? ---
    pop = todas if todas else escolhidas
    tem_ho = [c for c in pop if _f(c.get('ROI_ho')) is not None]
    if len(tem_ho) >= 8:
        r = correlacao([_f(c.get('ROI')) for c in tem_ho],
                       [_f(c.get('ROI_ho')) for c in tem_ho])
        if r is not None:
            out.append(_check(
                'O ranking se sustenta fora da amostra?',
                f'{r:+.2f}'.replace('.', ','), r > CORR_MIN,
                f'Comparando o desempenho no treino com o dos dias que a busca '
                f'nunca viu, em {_mil(len(tem_ho))} estratégias',
                None if r > CORR_MIN else
                'A ordem se inverteu: as melhores no treino foram as piores '
                'depois. Escolher pelo topo desta lista seria pior que sortear '
                '— não use esta varredura para escolher.'))
    else:
        out.append(_check(
            'O ranking se sustenta fora da amostra?', 'sem holdout', True,
            'Esta varredura não separou dias para conferência, então não dá '
            'para saber se a ordem se sustenta',
            'Rode a varredura com holdout para poder confiar na ordenação.'))

    # --- 2. depende de eixo derivado? ---
    def _der(c):
        e = str(c.get('extra') or '').strip().lower()
        return next((d for d in DERIVADOS if e.startswith(d)), None)
    com = [c for c in escolhidas if _der(c)]
    frac = len(com) / n
    quais = sorted({_der(c) for c in com})
    out.append(_check(
        'As escolhidas dependem de eixo derivado?',
        f'{len(com)} de {n}', frac <= EIXO_DERIVADO_MAX,
        (f'Usam {", ".join(quais)} — cortes que o garimpo inventa e que '
         f'costumam brilhar no treino e afundar depois' if com
         else 'Nenhuma depende desse tipo de corte'),
        None if frac <= EIXO_DERIVADO_MAX else
        f'{_pct(len(com), n):.0f}% das escolhidas dependem de um corte '
        f'derivado. No histórico deste projeto eles sobrevivem em 13% a 29% '
        f'dos casos, contra 99% das configs sem eles. Prefira as sem.'))

    # --- 3. o lucro está espalhado? ---
    concs = [_f(c.get('conc3'), None) for c in escolhidas]
    concs = [x for x in concs if x is not None and x < 900]
    if concs:
        pior = max(concs)
        med = sum(concs) / len(concs)
        ruins = sum(1 for x in concs if x > CONC_MAX)
        out.append(_check(
            'O lucro está espalhado entre os jogadores?',
            f'top-3 com {med:.0f}%', ruins <= n * 0.3,
            f'Em média os 3 jogadores mais lucrativos respondem por {med:.0f}% '
            f'do resultado; na pior das escolhidas, {pior:.0f}%',
            None if ruins <= n * 0.3 else
            f'{ruins} das {n} escolhidas dependem de poucos jogadores. Quando '
            f'eles saem de forma, a estratégia acaba junto — isso é uma lista '
            f'de nomes disfarçada de estratégia.'))

    # --- 4. a magnitude é plausível? ---
    if baseline_treino is not None:
        premios = [_f(c.get('ROI')) - baseline_treino for c in escolhidas
                   if _f(c.get('ROI')) is not None]
        if premios:
            med = sum(premios) / len(premios)
            ok = med <= PREMIO_SUSPEITO
            out.append(_check(
                'O ganho é plausível?',
                f'+{med:.0f} pontos', ok,
                f'Quanto rendem acima do mercado, que está em '
                f'{baseline_treino:+.2f}%. O normal deste projeto é '
                f'{PREMIO_TIPICO[0]:.0f} a {PREMIO_TIPICO[1]:.0f} pontos',
                None if ok else
                f'Ganho de {med:.0f} pontos é bem acima de tudo que já se '
                f'mediu aqui. Quase sempre significa que o backtest de origem '
                f'já vinha filtrado — o número encolhe quando medido no '
                f'universo completo. Confira a origem antes de usar.'))

    graves = [c for c in out if not c['ok']]
    if not graves:
        return out, 'confiavel', f'{n} estratégias prontas para testar.'
    if len(graves) == 1 and graves[0]['pergunta'].startswith('As escolhidas'):
        return out, 'atencao', ('Dá para testar, mas parte das escolhidas '
                                'depende de cortes frágeis.')
    return out, 'nao_use', ('O que está selecionado não se sustenta. Ajustar '
                            'o critério de ordenação não resolve.')


# ============================================================ empacotamento ==
# formato COLUNAR: pesa ~40% menos que uma lista de objetos, e com 23 mil
# configs isso é a diferença entre 7 MB e 3 MB no navegador. Medido.
COLUNAS = ['nome', 'lado', 'desc', 'teto', 'ap', 'G', 'R', 'WR', 'u', 'ROI',
           'ap_dia', 'u_dia', 'DD', 'ldd', 'dias_pos', 'dias_neg', 'seq_neg',
           'pior_dia', 'm1', 'm2', 'conc3', 'n_alvos', 'premio', 'extra',
           'ROI_ho', 'ap_ho']


def empacotar(configs, baseline_treino=None):
    linhas = []
    for c in configs:
        roi = _f(c.get('ROI'))
        linhas.append([
            c.get('nome'), 1 if c.get('lado') == 'favorito' else 0,
            c.get('desc') or c.get('_desc'), c.get('teto'),
            c.get('ap'), c.get('G'), c.get('R'), _f(c.get('WR')),
            _f(c.get('u')), roi, _f(c.get('ap_dia')), _f(c.get('u_dia')),
            _f(c.get('DD')), _f(c.get('ldd')), c.get('dias_pos'),
            c.get('dias_neg'), c.get('seq_neg'), _f(c.get('pior_dia')),
            _f(c.get('m1')), _f(c.get('m2')), _f(c.get('conc3')),
            c.get('n_alvos'),
            None if (roi is None or baseline_treino is None) else round(roi - baseline_treino, 1),
            c.get('extra'), _f(c.get('ROI_ho')), c.get('ap_ho'),
        ])
    return {'cols': COLUNAS, 'rows': linhas}
