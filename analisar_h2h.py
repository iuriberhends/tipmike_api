# -*- coding: utf-8 -*-
r"""
analisar_h2h.py — QUANTO HISTORICO O MOTOR ENXERGA, de verdade?

O PROBLEMA QUE ESTE SCRIPT RESOLVE
==================================
Perguntar "o h2h esta completo?" olhando UMA tabela leva a resposta errada.
Aconteceu tres vezes em 19/ago:
  - `h2h_historico` agrupado por `liga` parecia PARADO em 17/06 -> era so' o
    campo `liga` que deixou de ser gravado; a TM nunca parou (24.781 jogos de
    E-Basketball ate hoje, com o rotulo vazio);
  - `h2h_matches` da BATTLE parecia ter BURACO de 44% -> era o coletor bet365
    comecando (existe desde 07/05), nao queda de servico;
  - e o motor, o tempo todo, lia as DUAS e nao reclamava.

Entao a unica pergunta honesta e': **quanto historico o RUNNER enxerga?** E a
unica forma de responder e' ler as MESMAS fontes com as MESMAS regras que ele.

O QUE O RUNNER FAZ (copiado do workers/backtest_runner.py, nao inferido)
=======================================================================
Duas pernas em UNION ALL, filtradas por SPORT + NICK — nunca por liga:

  perna 'tick'  h2h_matches   WHERE bookmaker=$1 AND sport=$2 AND par
                (o resumo dos ticks do proprio coletor, 1 linha por
                 evento/casa, mantido pelo atualizar_h2h a cada 60s)
  perna 'hist'  h2h_historico WHERE sport=$2 AND par
                ts convertido: (ts AT TIME ZONE 'America/Sao_Paulo')
                — a coluna e' timestamp SEM fuso gravada em horario de
                Brasilia pelo seeder da TM

  dedup entre fontes: mesmo PLACAR NORMALIZADO + mesmo PAR dentro de 45min,
  e SO entre fontes diferentes. Mantem o 'hist' (placar oficial da TM) e
  descarta o 'tick'.

Este script replica isso. Se ele disser que falta, falta de verdade.

USO
    python analisar_h2h.py --parquet 15d.parquet --casa bet365
    python analisar_h2h.py --parquet 15d.parquet --casa bet365 --janelas 10,20,30,50
"""
import argparse
import sys
import unicodedata
from collections import defaultdict

import pandas as pd

DSN = "postgresql://postgres:mikedb0702@localhost:5432/mikedb"
JANELA_DEDUP_MIN = 45          # igual ao runner
MIN_ATROPELO = 6               # minimo de jogos do filtro de atropelo


def norm(s):
    if s is None:
        return ''
    t = unicodedata.normalize('NFKD', str(s))
    return ' '.join(''.join(c for c in t if not unicodedata.combining(c)).upper().split())


# ---------------------------------------------------------------- consulta --
# MESMA do runner: duas pernas, por sport+nick, com a conversao de fuso do
# lado hist. Aqui puxamos de uma vez todos os jogadores da liga.
SQL = """
SELECT jogador, ts, score_home, score_away, fonte FROM (
    SELECT UPPER(m.jogador_a) AS jogador, m.ts_fim AS ts,
           m.score_a AS score_home, m.score_b AS score_away, 'tick' AS fonte
      FROM h2h_matches m
     WHERE m.bookmaker = %(casa)s AND m.sport = %(sport)s
       AND UPPER(m.jogador_a) = ANY(%(nicks)s)
       AND m.score_a IS NOT NULL AND m.score_b IS NOT NULL
    UNION ALL
    SELECT UPPER(m.jogador_b), m.ts_fim, m.score_b, m.score_a, 'tick'
      FROM h2h_matches m
     WHERE m.bookmaker = %(casa)s AND m.sport = %(sport)s
       AND UPPER(m.jogador_b) = ANY(%(nicks)s)
       AND m.score_a IS NOT NULL AND m.score_b IS NOT NULL
    UNION ALL
    SELECT UPPER(h.jogador_a), (h.ts AT TIME ZONE 'America/Sao_Paulo'),
           h.score_home, h.score_away, 'hist'
      FROM h2h_historico h
     WHERE h.sport = %(sport)s AND UPPER(h.jogador_a) = ANY(%(nicks)s)
       AND h.score_home IS NOT NULL AND h.score_away IS NOT NULL
    UNION ALL
    SELECT UPPER(h.jogador_b), (h.ts AT TIME ZONE 'America/Sao_Paulo'),
           h.score_away, h.score_home, 'hist'
      FROM h2h_historico h
     WHERE h.sport = %(sport)s AND UPPER(h.jogador_b) = ANY(%(nicks)s)
       AND h.score_home IS NOT NULL AND h.score_away IS NOT NULL
) u
ORDER BY jogador, ts
"""


# --------------------------------------------------- cruzamento das fontes --
# A TM (h2h_historico) vem do organizador do torneio; a h2h_matches vem do
# feed da casa via coletor proprio. Sao INDEPENDENTES — quando as duas
# cobrem o mesmo jogo, tem que concordar. Onde nao concordam, alguma esta
# errada, e todo chip calculado ali esta errado junto.
#
# NAO consultamos a TM ao vivo de proposito: o h2h_historico VEIO dela, entao
# comparar os dois seria espelho, nao testemunha. Se o seeder gravou torto,
# erraria de novo do mesmo jeito.
# A comparacao e' feita em HORARIO DE BRASILIA nos dois lados — a mesma
# escala que o runner usa. Comparar contra UTC dava pico em -3h (a diferenca
# BRT/UTC) e eu quase reportei isso como erro de fuso; e' o esperado.
SQL_CRUZA = """
WITH m AS (
    SELECT UPPER(jogador_a) a, UPPER(jogador_b) b,
           ts_fim, score_a, score_b
      FROM h2h_matches
     WHERE bookmaker = %(casa)s AND sport = %(sport)s
       AND score_a IS NOT NULL AND score_b IS NOT NULL
       AND ts_fim >= %(de)s
), h AS (
    SELECT UPPER(jogador_a) a, UPPER(jogador_b) b,
           ts, score_home, score_away
      FROM h2h_historico
     WHERE sport = %(sport)s
       AND score_home IS NOT NULL AND score_away IS NOT NULL
       AND ts >= %(de_h)s
)
SELECT
    ROUND(EXTRACT(EPOCH FROM (h.ts - (m.ts_fim AT TIME ZONE 'America/Sao_Paulo'))) / 3600.0) AS horas,
    (h.a = m.a) AS mesma_ordem,
    COUNT(*) AS n,
    COUNT(*) FILTER (WHERE
        CASE WHEN h.a = m.a THEN (h.score_home = m.score_a AND h.score_away = m.score_b)
             ELSE (h.score_home = m.score_b AND h.score_away = m.score_a) END) AS placar_ok,
    COUNT(*) FILTER (WHERE
        CASE WHEN h.a = m.a THEN (h.score_home = m.score_b AND h.score_away = m.score_a)
             ELSE (h.score_home = m.score_a AND h.score_away = m.score_b) END) AS espelhado
  FROM m JOIN h
    ON ((h.a = m.a AND h.b = m.b) OR (h.a = m.b AND h.b = m.a))
   AND ABS(EXTRACT(EPOCH FROM (h.ts - (m.ts_fim AT TIME ZONE 'America/Sao_Paulo')))) < 2700
 GROUP BY 1, 2
 ORDER BY n DESC
"""


def cruzar(con, casa, sport, de):
    """Compara TM x coletor no que as duas cobrem. Devolve (linhas, veredito)."""
    de_h = de - pd.Timedelta(hours=12)   # folga pro lado sem fuso
    df = pd.read_sql(SQL_CRUZA, con, params={'casa': casa, 'sport': sport,
                                             'de': de.to_pydatetime(),
                                             'de_h': de_h.to_pydatetime()})
    return df


def dedup(jogos):
    """Dedup entre fontes, regra do runner: mesmo placar normalizado dentro de
    45min e SO entre fontes diferentes; mantem o 'hist'."""
    jogos = sorted(jogos, key=lambda x: x['ts'])
    manter = []
    for jg in jogos:
        chave = tuple(sorted((jg['sh'], jg['sa'])))
        dup = None
        for i, m in enumerate(manter):
            if m['fonte'] == jg['fonte']:
                continue
            if tuple(sorted((m['sh'], m['sa']))) != chave:
                continue
            if abs((jg['ts'] - m['ts']).total_seconds()) <= JANELA_DEDUP_MIN * 60:
                dup = i
                break
        if dup is None:
            manter.append(jg)
        elif manter[dup]['fonte'] == 'tick' and jg['fonte'] == 'hist':
            manter[dup] = jg          # o hist ganha
    return manter


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--parquet', required=True)
    p.add_argument('--casa', required=True, help='bookmaker (perna tick)')
    p.add_argument('--dsn', default=DSN)
    p.add_argument('--sport', default=None)
    p.add_argument('--janelas', default='10,20,30,50')
    a = p.parse_args()

    tk = pd.read_parquet(a.parquet)
    tk['ts'] = pd.to_datetime(tk['ts'])
    jg = tk.sort_values('ts').drop_duplicates('event_id')
    jg['A'] = jg.jogador_a.map(norm)
    jg['B'] = jg.jogador_b.map(norm)
    sport = a.sport or str(jg.sport.iloc[0])
    liga = str(jg.liga.iloc[0])
    nicks = sorted(set(jg.A) | set(jg.B))
    if jg.ts.dt.tz is not None:
        jg['ts'] = jg.ts.dt.tz_convert('America/Sao_Paulo').dt.tz_localize(None)
    d0, d1 = jg.ts.min(), jg.ts.max()

    print('=' * 78)
    print(' COBERTURA DE H2H — na régua do runner (as duas fontes, como ele lê)')
    print('=' * 78)
    print(f'\nPARQUET  {liga} | {len(jg):,} jogos | {d0:%d/%m} a {d1:%d/%m} '
          f'| {len(nicks)} jogadores')
    print(f'BUSCA    sport={sport!r}  casa={a.casa!r}  (NUNCA por liga — o runner '
          f'nao filtra por ela)')

    try:
        import psycopg2
        con = psycopg2.connect(a.dsn)
    except ImportError:
        print('\nERRO: pip install psycopg2-binary --break-system-packages')
        return 2
    df = pd.read_sql(SQL, con, params={'casa': a.casa, 'sport': sport, 'nicks': nicks})
    con.close()
    if df.empty:
        print('\nNADA ENCONTRADO. Confira o sport e a casa contra o banco:')
        print("  SELECT DISTINCT sport FROM h2h_historico;")
        print("  SELECT DISTINCT bookmaker FROM h2h_matches;")
        return 1
    # NORMALIZA O FUSO. A perna 'tick' vem de ts_fim (timestamptz, chega com
    # fuso) e a 'hist' de (ts AT TIME ZONE 'America/Sao_Paulo') (sem fuso).
    # Misturar as duas estoura em qualquer comparacao. Deixo TUDO em horario
    # de Brasilia SEM fuso — a mesma escala em que o runner compara.
    # As duas pernas voltam MISTURADAS: a 'tick' com fuso (ts_fim e'
    # timestamptz) e a 'hist' sem (ja convertida pra Brasilia no SQL).
    # `utc=True` transformaria os SEM-fuso em NaT e eu jogaria fora as 87k
    # linhas do historico. Entao converte cada valor conforme o que ele e'.
    def _brt(v):
        t = pd.Timestamp(v)
        if t.tzinfo is not None:
            return t.tz_convert('America/Sao_Paulo').tz_localize(None)
        return t                       # ja esta em Brasilia, sem fuso
    df['ts'] = df.ts.map(_brt)
    df['ts'] = pd.to_datetime(df.ts, errors='coerce')
    n_ruim = int(df.ts.isna().sum())
    if n_ruim:
        print(f'  aviso: {n_ruim} linhas com ts ilegivel — descartadas')
        df = df.dropna(subset=['ts'])

    print(f'\nENCONTRADO (bruto, antes do dedup): {len(df):,} linhas')
    for f, n in df.fonte.value_counts().items():
        sub = df[df.fonte == f]
        print(f'   {f:<5} {n:>8,} linhas | {sub.ts.min():%d/%m/%y} a {sub.ts.max():%d/%m/%y}')

    # ---- por jogador, com dedup igual ao runner ----
    por_jog = {}
    for nick, g in df.groupby('jogador'):
        jogos = [{'ts': r.ts, 'sh': int(r.score_home), 'sa': int(r.score_away),
                  'fonte': r.fonte} for r in g.itertuples()]
        por_jog[nick] = dedup(jogos)

    jan = [int(x) for x in a.janelas.split(',') if x.strip().isdigit()]
    print('\n' + '-' * 78)
    print(' POR JOGADOR — histórico DISPONÍVEL no instante da 1ª aposta do período')
    print('-' * 78)
    print(f'  {"jogador":<18} {"total":>6} {"antes":>6} {"tick":>6} {"hist":>6}  último')
    linhas = []
    for nick in nicks:
        js = por_jog.get(nick, [])
        antes = [j for j in js if j['ts'] < d0]
        t = sum(1 for j in antes if j['fonte'] == 'tick')
        h = len(antes) - t
        ult = max((j['ts'] for j in js), default=None)
        linhas.append((nick, len(js), len(antes), t, h, ult))
    for nick, tot, ant, t, h, ult in sorted(linhas, key=lambda x: x[2]):
        u = f'{ult:%d/%m/%y}' if ult is not None else '—'
        print(f'  {nick[:18]:<18} {tot:>6} {ant:>6} {t:>6} {h:>6}  {u}')

    # ---- a régua: quantas apostas teriam base suficiente ----
    print('\n' + '-' * 78)
    print(' A RÉGUA — % dos JOGOS do período com histórico suficiente por janela')
    print('   (conta o histórico ANTERIOR a cada jogo, para os DOIS jogadores)')
    print('-' * 78)
    idx = {n: sorted(j['ts'] for j in por_jog.get(n, [])) for n in nicks}
    import bisect
    res = defaultdict(int)
    for r in jg.itertuples():
        na = bisect.bisect_left(idx.get(r.A, []), r.ts)
        nb = bisect.bisect_left(idx.get(r.B, []), r.ts)
        pior = min(na, nb)
        for w in jan:
            if pior >= w:
                res[w] += 1
        if pior >= MIN_ATROPELO:
            res['atr'] += 1
    n = len(jg)
    for w in jan:
        pct = res[w] / n * 100
        marca = 'OK  ' if pct >= 80 else ('meia' if pct >= 50 else 'RUIM')
        print(f'  chip Últ.{w:<3}  {res[w]:>5} de {n:,} jogos ({pct:5.1f}%)  {marca}')
    pct = res['atr'] / n * 100
    print(f'  atropelo(≥{MIN_ATROPELO})  {res["atr"]:>5} de {n:,} jogos ({pct:5.1f}%)  '
          f'{"OK" if pct >= 80 else "PARCIAL"}')

    # ---- cruzamento TM x coletor ----
    print('\n' + '-' * 78)
    print(' AS DUAS FONTES CONCORDAM? (TM x coletor, onde as duas cobrem)')
    print('-' * 78)
    try:
        import psycopg2 as _pg
        con2 = _pg.connect(a.dsn)
        cz = cruzar(con2, a.casa, sport, d0 - pd.Timedelta(days=90))
        con2.close()
    except Exception as e:
        cz = None
        print(f'  nao consegui cruzar: {type(e).__name__}: {str(e)[:90]}')
    if cz is not None and not cz.empty:
        tot = int(cz.n.sum())
        print(f'  {tot:,} jogos cobertos pelas DUAS fontes\n')
        print(f'  {"desloc":>7} {"ordem":>7} {"jogos":>8} {"placar ok":>11} {"espelhado":>11}')
        for r in cz.head(6).itertuples():
            ordem = 'igual' if r.mesma_ordem else 'trocada'
            print(f'  {int(r.horas):>+6}h {ordem:>7} {int(r.n):>8,} '
                  f'{int(r.placar_ok):>11,} {int(r.espelhado):>11,}')
        pico = cz.loc[cz.n.idxmax()]
        h_pico = int(pico.horas)
        ok = int(cz.placar_ok.sum()); esp = int(cz.espelhado.sum())
        print()
        print(f'  FUSO       pico em {h_pico:+d}h  ->  '
              + ('OK — as duas fontes alinham na escala de Brasilia, que e a '
                 'que o runner usa'
                 if h_pico == 0 else
                 f'ATENCAO: {h_pico:+d}h de deslocamento REAL. O dedup de 45min '
                 'nao casa os jogos e cada partida conta DUAS vezes'))
        print(f'  PLACAR     {ok:,} de {tot:,} iguais ({ok/max(tot,1)*100:.1f}%)  ->  '
              + ('OK' if ok / max(tot, 1) >= 0.97 else
                 'DIVERGENCIA acima do ruido — investigar'))
        print(f'  ORIENTACAO {esp:,} espelhados ({esp/max(tot,1)*100:.1f}%)  ->  '
              + ('OK, nome e placar alinhados' if esp / max(tot, 1) <= 0.03 else
                 'INVERTIDO! toda conta de cobertura de handicap sai espelhada'))
    elif cz is not None:
        print('  as duas fontes nao cobrem nenhum jogo em comum neste periodo —')
        print('  sem sobreposicao nao da pra validar uma contra a outra.')

    print('\n' + '=' * 78)
    print(' LEITURA')
    print('=' * 78)
    pior_jan = min(jan)
    if res[pior_jan] / n >= 0.8:
        print(f' Dá pra garimpar. Até a janela Últ.{max(jan)} tem base em '
              f'{res[max(jan)]/n*100:.0f}% dos jogos.')
    else:
        print(' Histórico raso pra este período. Use janelas curtas (Últ.10/20) ou')
        print(' garimpe um período mais recente, onde o histórico já acumulou.')
    print(' O que NÃO é problema: campo `liga` vazio no h2h_historico — o runner')
    print(' busca por sport+nick e nunca olha esse campo.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
