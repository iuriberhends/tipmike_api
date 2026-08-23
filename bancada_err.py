#!/usr/bin/env python3
"""Bancada v23 — testa as funcoes REAIS do runner/executor patchados contra
cenarios sinteticos que cobrem cada armadilha da ficha do ERR."""
import sys, types, asyncio
from datetime import datetime, timedelta

# stub do modulo database (o container nao tem o banco)
db = types.ModuleType('database')
db.get_pool = lambda: None
sys.modules['database'] = db

from workers.backtest_runner import (
    _err_reduzir_eventos, _err_montar_historico, _checar_err,
    _err_params_do_filtro, ERR_ABERTURA_SOMA_MAX,
)

OK = FAIL = 0
def check(nome, cond, extra=''):
    global OK, FAIL
    if cond:
        OK += 1; print(f"  ok  {nome}")
    else:
        FAIL += 1; print(f"  FAIL {nome} {extra}")

T0 = datetime(2026, 8, 20, 12, 0, 0)
CASA = 'bet365'   # usa o mapa real: over_under_ft='1450', over_under_ht='180062'

def tk(evt, dt_min, ja='ALFA', jb='BRAVO', liga='B-EBASKBAT4X5', lt=None,
       sh=None, sa=None, mercado='Total de Pontos', mtipo='1450', linha=None):
    return {'event_id': evt, 'ts': T0 + timedelta(minutes=dt_min),
            'jogador_a': ja, 'jogador_b': jb, 'liga': liga, 'live_time': lt,
            'score_home': sh, 'score_away': sa, 'mercado': mercado,
            'mercado_tipo': mtipo, 'linha': linha, 'selecao': 'Mais de',
            'selecao_id': 'o', 'mercado_id': 'm1', 'odds': 1.85}

def jogo(evt, ini_min, ja, jb, abertura, total, liga='B-EBASKBAT4X5'):
    """Jogo completinho: abertura no ini, meio de jogo, END com placar final."""
    h = total // 2
    return [
        tk(evt, ini_min, ja, jb, liga, lt='1Q', sh=0, sa=0, linha=str(abertura)),
        tk(evt, ini_min + 8, ja, jb, liga, lt='3Q', sh=h - 10, sa=h - 20,
           linha=str(abertura + 6)),
        tk(evt, ini_min + 20, ja, jb, liga, lt='END', sh=h, sa=total - h,
           mercado='SCORE_UPDATE', mtipo='SCORE_UPDATE', linha=None),
    ]

print("== T1: conta basica — err do jogo, media da janela, MIN dos dois ==")
ticks = []
# ALFA joga 6 jogos com err +5 (total 140, abre 135); BRAVO 6 jogos com err +2
for i in range(6):
    ticks += jogo(f'A{i}', i * 30, 'ALFA', f'RIVAL{i}', 135.5, 141)   # err +5.5
    ticks += jogo(f'B{i}', i * 30 + 5, 'BRAVO', f'XIS{i}', 138.5, 141)  # err +2.5
jogos, cont = _err_reduzir_eventos(ticks, CASA, 'ft', agora=None)
check("12 jogos fechados", len(jogos) == 12, f"veio {len(jogos)} cont={cont}")
check("err do jogo = total - abertura", abs(jogos['A0']['err'] - 5.5) < 1e-9)
hist = _err_montar_historico(jogos)
tick_novo = tk('NOVO', 6 * 30 + 60, 'ALFA', 'BRAVO', linha='140.5')
ok, mot, val = _checar_err(tick_novo, hist, None, 5, 5, None, None)
check("min dos dois (2.5, nao 5.5)", ok and abs(val - 2.5) < 1e-9, f"{ok} {mot} {val}")
ok, mot, val = _checar_err(tick_novo, hist, None, 5, 5, 1.5, None)
check("corte >=1.5 passa", ok, f"{mot}")
ok, mot, val = _checar_err(tick_novo, hist, None, 5, 5, 3.0, None)
check("corte >=3.0 rejeita com motivo 'err'", (not ok) and mot == 'err', f"{mot}")

print("== T2: anti-vazamento — o proprio jogo NUNCA entra ==")
# BRAVO tem 5 jogos err +2.5 e um 6o jogo GIGANTE (err +40) que termina DEPOIS
# do tick avaliado. Se o codigo vazasse, a media mudaria.
ticks2 = []
for i in range(5):
    ticks2 += jogo(f'C{i}', i * 30, 'BRAVO', f'XIS{i}', 138.5, 141)
ticks2 += jogo('FUTURO', 200, 'BRAVO', 'ZULU', 100.5, 141)  # err +40.5, fecha ~220min
jg2, _ = _err_reduzir_eventos(ticks2, CASA, 'ft', agora=None)
h2 = _err_montar_historico(jg2)
tick_meio = tk('AVAL', 205, 'BRAVO', 'ALFA', linha='140.5')  # jogo FUTURO ainda aberto as 205
# ALFA nao tem historico aqui -> uso um tick de teste so pra BRAVO via par (BRAVO, BRAVO2)?
# _checar_err exige os dois; monto ALFA com 5 jogos tb
for i in range(5):
    ticks2 += jogo(f'D{i}', i * 30 + 2, 'ALFA', f'YY{i}', 138.5, 141)
jg2, _ = _err_reduzir_eventos(ticks2, CASA, 'ft', agora=None)
h2 = _err_montar_historico(jg2)
ok, mot, val = _checar_err(tick_meio, h2, None, 5, 5, None, None)
check("jogo que fecha depois do tick fica FORA (media 2.5)",
      ok and abs(val - 2.5) < 1e-9, f"{ok} {mot} {val}")
# pro FUTURO aparecer no min(), o adversario precisa de media MAIOR que a do
# BRAVO pos-FUTURO: ZETA com 5 jogos de err +50.5
for i in range(5):
    ticks2 += jogo(f'Z{i}', i * 30 + 4, 'ZETA', f'WW{i}', 90.5, 141)
jg2, _ = _err_reduzir_eventos(ticks2, CASA, 'ft', agora=None)
h2 = _err_montar_historico(jg2)
tick_dep = tk('AVAL2', 260, 'BRAVO', 'ZETA', linha='140.5')  # depois do FUTURO fechar
ok, mot, val = _checar_err(tick_dep, h2, None, 5, 5, None, None)
check("depois de fechar, ENTRA (media do BRAVO vira 10.1 e e' o min)",
      ok and abs(val - ((2.5 * 4 + 40.5) / 5)) < 1e-9, f"{val}")

print("== T3: FT x HT separados ==")
t3 = []
# evento com total FT (1450, linha 140.5) e total HT (180062, linha 65.5)
t3.append(tk('H1', 0, 'ALFA', 'BRAVO', lt='1Q', sh=0, sa=0, linha='140.5'))
t3.append(tk('H1', 1, 'ALFA', 'BRAVO', lt='1Q', sh=0, sa=0,
             mercado='1st Half Total', mtipo='180062', linha='65.5'))
t3.append(tk('H1', 9, 'ALFA', 'BRAVO', lt='HT', sh=36, sa=35, mercado='SCORE_UPDATE',
             mtipo='SCORE_UPDATE'))
t3.append(tk('H1', 20, 'ALFA', 'BRAVO', lt='END', sh=70, sa=72, mercado='SCORE_UPDATE',
             mtipo='SCORE_UPDATE'))
jft, _ = _err_reduzir_eventos(t3, CASA, 'ft', agora=None)
jht, _ = _err_reduzir_eventos(t3, CASA, 'ht', agora=None)
check("FT: err = 142-140.5 = 1.5", abs(jft['H1']['err'] - 1.5) < 1e-9, str(jft))
check("HT: err = 71-65.5 = 5.5 (linha E total do 1o tempo)",
      abs(jht['H1']['err'] - 5.5) < 1e-9, str(jht))

print("== T4: fechamento — jogo cortado nao entra ==")
t4 = jogo('OKAY', 0, 'ALFA', 'BRAVO', 140.5, 141)
t4 += [tk('CEGO', 0, 'CHARLIE', 'DELTA', lt='1Q', sh=0, sa=0, linha='140.5'),
       tk('CEGO', 3, 'CHARLIE', 'DELTA', lt='2Q', sh=20, sa=18, linha='142.5')]
j4, c4 = _err_reduzir_eventos(t4, CASA, 'ft', agora=None)
check("cortado no 2Q fica FORA (aberto=1)", 'CEGO' not in j4 and c4['aberto'] == 1, str(c4))
check("jogo com END entra", 'OKAY' in j4)

print("== T5: abertura tardia ==")
t5 = [tk('TARDE', 0, 'ALFA', 'BRAVO', lt='2Q', sh=30, sa=28, linha='150.5'),
      tk('TARDE', 12, 'ALFA', 'BRAVO', lt='END', sh=70, sa=71, mercado='SCORE_UPDATE',
         mtipo='SCORE_UPDATE')]
j5, c5 = _err_reduzir_eventos(t5, CASA, 'ft', agora=None)
check(f"1o tick com soma {30+28} > {ERR_ABERTURA_SOMA_MAX} descartado",
      'TARDE' not in j5 and c5['abertura_tardia'] == 1, str(c5))

print("== T6: separacao por LIGA ==")
t6 = []
for i in range(5):
    t6 += jogo(f'L{i}', i * 30, 'ALFA', f'R{i}', 138.5, 141, liga='B-EBASKBLITZ4X5')
    t6 += jogo(f'M{i}', i * 30 + 3, 'BRAVO', f'S{i}', 138.5, 141, liga='B-EBASKBLITZ4X5')
j6, _ = _err_reduzir_eventos(t6, CASA, 'ft', agora=None)
h6 = _err_montar_historico(j6)
tick_outra = tk('X', 999, 'ALFA', 'BRAVO', liga='B-EBASKBAT4X5', linha='140.5')
ok, mot, _ = _checar_err(tick_outra, h6, None, 5, 5, None, None)
check("hist da Blitz nao vale pra tick da Battle (err_hist_insuf)",
      (not ok) and mot == 'err_hist_insuf', mot)
tick_mesma = tk('X2', 999, 'ALFA', 'BRAVO', liga='B-EBASKBLITZ4X5', linha='140.5')
ok, _, _ = _checar_err(tick_mesma, h6, None, 5, 5, None, None)
check("na mesma liga funciona", ok)

print("== T7: hist insuficiente / min_jogos ==")
ok, mot, _ = _checar_err(tick_mesma, h6, None, 5, 6, None, None)
check("min_jogos=6 com 5 jogos -> err_hist_insuf", (not ok) and mot == 'err_hist_insuf', mot)

print("== T8: janela em HORAS ==")
# ALFA: 5 jogos velhos (err +2.5, ha 20h) + 5 recentes (err +6.5, ultimas 2h)
t8 = []
for i in range(5):
    t8 += jogo(f'V{i}', -1200 + i * 30, 'ALFA', f'RV{i}', 138.5, 141)   # ha ~20h
    t8 += jogo(f'W{i}', -110 + i * 22, 'ALFA', f'RW{i}', 134.5, 141)    # err 6.5, ult. 2h
    t8 += jogo(f'Y{i}', -1200 + i * 30 + 3, 'BRAVO', f'QV{i}', 138.5, 141)
    t8 += jogo(f'Z{i}', -110 + i * 22 + 3, 'BRAVO', f'QW{i}', 134.5, 141)
j8, _ = _err_reduzir_eventos(t8, CASA, 'ft', agora=None)
h8 = _err_montar_historico(j8)
tick8 = tk('T8', 5, 'ALFA', 'BRAVO', linha='140.5')
ok, _, v_cont = _checar_err(tick8, h8, None, 5, 5, None, None)          # por contagem: 5 recentes
ok2, _, v_hor = _checar_err(tick8, h8, None, 5, 5, None, None, janela_horas=3.0)
ok3, mot3, _ = _checar_err(tick8, h8, None, 5, 2, None, None, janela_horas=0.2)
check("contagem pega os 5 recentes (6.5)", ok and abs(v_cont - 6.5) < 1e-9, str(v_cont))
check("janela 3h = mesmos 5 recentes (6.5)", ok2 and abs(v_hor - 6.5) < 1e-9, str(v_hor))
check("janela 0.2h sem jogo suficiente -> err_hist_insuf",
      (not ok3) and mot3 == 'err_hist_insuf', str(mot3))

print("== T9: memo por evento ==")
memo = {}
_checar_err(tick_mesma, h6, memo, 5, 5, None, None)
r1 = memo.get('X2')
r2 = _checar_err(tick_mesma, h6, memo, 5, 5, None, None)
check("memo preenchido e reutilizado", r1 is not None and r2 == r1)

print("== T10: mediana de linhas na abertura (escada no mesmo ts) ==")
t10 = [tk('E1', 0, 'ALFA', 'BRAVO', lt='1Q', sh=0, sa=0, linha='138.5'),
       tk('E1', 0, 'ALFA', 'BRAVO', lt='1Q', sh=0, sa=0, linha='140.5'),
       tk('E1', 0, 'ALFA', 'BRAVO', lt='1Q', sh=0, sa=0, linha='142.5'),
       tk('E1', 20, 'ALFA', 'BRAVO', lt='END', sh=70, sa=72, mercado='SCORE_UPDATE',
          mtipo='SCORE_UPDATE')]
j10, _ = _err_reduzir_eventos(t10, CASA, 'ft', agora=None)
check("abertura = mediana 140.5 (err 1.5)", abs(j10['E1']['err'] - 1.5) < 1e-9, str(j10))

print("== T11: _err_params_do_filtro coercoes ==")
check("defaults", _err_params_do_filtro({}) == (5, 5, None))
check("valores", _err_params_do_filtro({'errJanela': 3, 'errMinJogos': 4,
      'errJanelaHoras': 2}) == (3, 4, 2.0))
check("lixo vira default", _err_params_do_filtro({'errJanela': 'x',
      'errMinJogos': -1, 'errJanelaHoras': 'y'}) == (5, 5, None))

print("== T12: fechamento AO VIVO por idade (regra do resolvedor) ==")
agora = T0 + timedelta(minutes=21)
t12 = jogo('VIVO1', 0, 'ALFA', 'BRAVO', 140.5, 141)      # END em T0+20 -> idade 60s
j12a, c12a = _err_reduzir_eventos(t12, CASA, 'ft', agora=agora)
check("END com idade 60s < 180s: ainda ABERTO", 'VIVO1' not in j12a and c12a['aberto'] == 1)
agora2 = T0 + timedelta(minutes=24)
j12b, _ = _err_reduzir_eventos(t12, CASA, 'ft', agora=agora2)
check("END com idade 240s >= 180s: FECHADO", 'VIVO1' in j12b)
# jogo cego (0-0, live_time so 1Q) nunca fecha nem com horas de idade
t12c = [tk('CEGO2', 0, 'ALFA', 'BRAVO', lt='1Q', sh=0, sa=0, linha='140.5')]
j12c, c12c = _err_reduzir_eventos(t12c, CASA, 'ft', agora=T0 + timedelta(hours=3))
check("jogo cego 0-0 NUNCA vira err", 'CEGO2' not in j12c and c12c['aberto'] == 1)
# 'Q4 03:24' (dialeto JBot, Q na frente) fecha como 2a metade
t12d = [tk('Q4J', 0, 'ALFA', 'BRAVO', lt='1Q', sh=0, sa=0, linha='140.5'),
        tk('Q4J', 18, 'ALFA', 'BRAVO', lt='Q4 03:24', sh=68, sa=70,
           mercado='SCORE_UPDATE', mtipo='SCORE_UPDATE')]
j12d, _ = _err_reduzir_eventos(t12d, CASA, 'ft', agora=T0 + timedelta(minutes=30))
check("'Q4 03:24' fecha com idade >=300s", 'Q4J' in j12d)
j12e, c12e = _err_reduzir_eventos(t12d, CASA, 'ft', agora=T0 + timedelta(minutes=20))
check("'Q4 03:24' com idade 120s ainda aberto", 'Q4J' not in j12e)

print("== T13: superbet (mercado_tipo texto + PERIOD_TOTAL no HT) ==")
t13 = [tk('S1', 0, 'ALFA', 'BRAVO', lt='1Q', sh=0, sa=0,
          mercado='Total de Pontos', mtipo='OVER_UNDER', linha='140.5'),
       tk('S1', 1, 'ALFA', 'BRAVO', lt='1Q', sh=0, sa=0,
          mercado='1º Tempo - Total de Pontos', mtipo='PERIOD_TOTAL', linha='65.5'),
       tk('S1', 9, 'ALFA', 'BRAVO', lt='HT', sh=34, sa=33, mercado='SCORE_UPDATE',
          mtipo='SCORE_UPDATE'),
       tk('S1', 20, 'ALFA', 'BRAVO', lt='END', sh=71, sa=70, mercado='SCORE_UPDATE',
          mtipo='SCORE_UPDATE')]
jS_ft, _ = _err_reduzir_eventos(t13, 'superbet', 'ft', agora=None)
jS_ht, _ = _err_reduzir_eventos(t13, 'superbet', 'ht', agora=None)
check("superbet FT: 141-140.5 = 0.5", abs(jS_ft['S1']['err'] - 0.5) < 1e-9, str(jS_ft))
check("superbet HT: 67-65.5 = 1.5", abs(jS_ht['S1']['err'] - 1.5) < 1e-9, str(jS_ht))
check("superbet FT nao pega a linha do PERIOD_TOTAL como abertura",
      abs(jS_ft['S1']['linha_abertura'] - 140.5) < 1e-9)

print(f"\n===== {OK} ok, {FAIL} FAIL =====")
sys.exit(1 if FAIL else 0)
