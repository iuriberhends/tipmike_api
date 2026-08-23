#!/usr/bin/env python3
"""Bancada v23 (parte 2) — ErrVivoCache do executor com FakePool."""
import sys, types, asyncio
from datetime import datetime, timedelta

db = types.ModuleType('database')
db.get_pool = lambda: None
sys.modules['database'] = db

import workers.bot_executor as be
from workers.backtest_runner import _checar_err

OK = FAIL = 0
def check(nome, cond, extra=''):
    global OK, FAIL
    if cond:
        OK += 1; print(f"  ok  {nome}")
    else:
        FAIL += 1; print(f"  FAIL {nome} {extra}")

T0 = datetime.now() - timedelta(hours=2)

def row(evt, ts, ja='ALFA', jb='BRAVO', liga='B-EBASKBAT4X5', lt=None,
        sh=None, sa=None, mercado='Total de Pontos', mtipo='1450', linha=None):
    return {'event_id': evt, 'ts': ts, 'jogador_a': ja, 'jogador_b': jb,
            'liga': liga, 'live_time': lt, 'score_home': sh, 'score_away': sa,
            'mercado': mercado, 'mercado_tipo': mtipo, 'linha': linha}

def jogo(evt, ini, ja, jb, abertura, total):
    h = total // 2
    return [row(evt, ini, ja, jb, lt='1Q', sh=0, sa=0, linha=str(abertura)),
            row(evt, ini + timedelta(minutes=18), ja, jb, lt='Q4 01:00',
                sh=h - 4, sa=h, linha=str(abertura + 4)),
            row(evt, ini + timedelta(minutes=20), ja, jb, lt='END',
                sh=h, sa=total - h, mercado='SCORE_UPDATE', mtipo='SCORE_UPDATE')]

class FakePool:
    """Simula a tabela ticks: fetch aplica os filtros do SQL da v23."""
    def __init__(self):
        self.rows = []
        self.chamadas = []
    async def fetch(self, sql, *params):
        casa, sport, de = params[0], params[1], params[2]
        ids = set(params[3]) if len(params) >= 5 else None
        lts = set(params[4]) if len(params) >= 6 else set()
        self.chamadas.append(de)
        out = []
        for r in self.rows:
            if r['ts'] < de:
                continue
            if ids is not None:
                lt_ok = r['live_time'] and any(
                    str(r['live_time']).upper().startswith(l) and
                    str(r['live_time']).upper() in {str(x) for x in lts} or
                    str(r['live_time']).upper() == str(l) for l in lts)
                # o SQL real compara igualdade exata live_time = ANY(...);
                # replica: live_time exatamente na lista OU mercado na lista
                lt_ok = str(r['live_time'] or '') in {str(x) for x in lts}
                if str(r['mercado_tipo']) not in {str(x) for x in ids} and not lt_ok:
                    continue
            out.append(r)
        return sorted(out, key=lambda r: r['ts'])

async def main():
    pool = FakePool()
    # 5 jogos FECHADOS do ALFA e do BRAVO (err +2.5), terminados ha >30min
    for i in range(5):
        pool.rows += jogo(f'A{i}', T0 + timedelta(minutes=i * 15), 'ALFA', f'R{i}', 138.5, 141)
        pool.rows += jogo(f'B{i}', T0 + timedelta(minutes=i * 15 + 5), 'BRAVO', f'S{i}', 138.5, 141)
    # 1 jogo AINDA ABERTO (END ha 30s nao existe; ultimo tick e' 3Q agora)
    agora = datetime.now()
    pool.rows += [row('ABERTO', agora - timedelta(minutes=10), 'ALFA', 'BRAVO',
                      lt='1Q', sh=0, sa=0, linha='140.5'),
                  row('ABERTO', agora - timedelta(minutes=6), 'ALFA', 'BRAVO',
                      lt='2Q', sh=30, sa=28, linha='146.5')]

    print("== V1: boot reconstrói o dia ==")
    c = be.ErrVivoCache(pool, 'bet365', 'E-Basketball')
    h = await c.historico('ft')
    check("10 jogos no hist (5+5), aberto fora",
          c.stats['jogos_ft'] == 10, str(c.stats))
    k = ('B-EBASKBAT4X5', 'ALFA')
    check("ALFA com 5 jogos", k in h and len(h[k][0]) == 5)
    tick = {'event_id': 'ABERTO', 'ts': agora, 'jogador_a': 'ALFA',
            'jogador_b': 'BRAVO', 'liga': 'B-EBASKBAT4X5'}
    ok, mot, val = _checar_err(tick, h, None, 5, 5, 1.5, None)
    check("err5 do par = 2.5, corte 1.5 passa", ok and abs(val - 2.5) < 1e-9,
          f"{ok} {mot} {val}")

    print("== V2: TTL — 2a chamada dentro de 60s NAO refaz query ==")
    n_q = len(pool.chamadas)
    await c.historico('ft')
    check("sem query nova", len(pool.chamadas) == n_q)

    print("== V3: incremento fecha jogo novo sem duplicar os antigos ==")
    # o jogo ABERTO termina agora (END com idade que ja passa de 180s no refresh)
    pool.rows += [row('ABERTO', agora - timedelta(seconds=240), 'ALFA', 'BRAVO',
                      lt='END', sh=70, sa=75, mercado='SCORE_UPDATE',
                      mtipo='SCORE_UPDATE')]
    c._ultimo_refresh = 0.0   # forca passar o TTL
    h = await c.historico('ft')
    check("agora 11 jogos (o ABERTO fechou 1x, sem duplicar)",
          c.stats['jogos_ft'] == 11, str(c.stats))
    check("ALFA com 6 jogos, ultimo err = 145-140.5 = 4.5",
          len(h[k][0]) == 6 and abs(h[k][1][-1] - 4.5) < 1e-9,
          str(h.get(k)))
    # refresh de novo (com margem que revarre o END) nao pode duplicar
    c._ultimo_refresh = 0.0
    h = await c.historico('ft')
    check("re-refresh nao duplica (segue 11)", c.stats['jogos_ft'] == 11,
          str(c.stats))
    check("ALFA segue com 6", len(h[k][0]) == 6)

    print("== V4: refresh quebrado nao derruba (fail-closed a jusante) ==")
    async def boom(*a, **kw):
        raise RuntimeError('banco caiu')
    pool_ok_fetch = pool.fetch
    pool.fetch = boom
    c._ultimo_refresh = 0.0
    h2 = await c.historico('ft')
    check("excecao engolida, hist antigo devolvido",
          c.stats['falhas'] == 1 and h2 is h, str(c.stats))
    pool.fetch = pool_ok_fetch

    print("== V5: _get_err_cache defensivo no state ==")
    class S: pass
    be.state = S()
    be.state.pool = pool
    c1 = be._get_err_cache('bet365', 'E-Basketball')
    c2 = be._get_err_cache('bet365', 'E-Basketball')
    c3 = be._get_err_cache('superbet', 'E-Basketball')
    check("mesma chave = mesmo cache; chave nova = outro", c1 is c2 and c1 is not c3)

    print(f"\n===== {OK} ok, {FAIL} FAIL =====")
    return 1 if FAIL else 0

sys.exit(asyncio.run(main()))
