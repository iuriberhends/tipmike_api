"""
SONDA 2: qual odd_status e' SUSPENSO na Superbet?
Segue cada (evento, mercado, linha, selecao) do PLAYER_TOTAL da Mixed no gz
de hoje e mede, tick a tick:
  - quantas vezes a odd MUDA entre ticks consecutivos, por status;
  - quantas vezes o PLACAR mudou e a odd ficou igual, por status;
  - quanto tempo cada status dura (mediana de ticks seguidos);
  - transicoes 0->1 e 1->0 e o que aconteceu com o placar nelas.
Suspenso = odd congelada enquanto o placar anda.
Uso: ..\\.venv\\Scripts\\python.exe sonda_status2.py > C:\\Users\\Administrator\\Downloads\\status2.txt
"""
import gzip, json, glob, collections

PASTA = r"C:\Users\Administrator\PyCharmMiscProject\MikeBacktest"
fs = sorted(glob.glob(PASTA + r"\superbet_ticks_2026-09-*.jsonl.gz"))
gz = fs[-1]
print("gz:", gz)

ult = {}                       # chave -> (status, odd, placar)
muda_odd = collections.Counter()      # status -> (odd mudou, odd igual)
placar_andou_odd_igual = collections.Counter()
placar_andou_odd_mudou = collections.Counter()
trans = collections.Counter()  # (de,para) -> n
trans_placar = collections.Counter()  # (de,para,'placar mudou'/'igual')
n = 0
with gzip.open(gz, "rt", errors="replace") as f:
    while True:
        try:
            l = f.readline()
        except (EOFError, OSError):
            break
        if not l:
            break
        try:
            t = json.loads(l)
        except Exception:
            continue
        if t.get("mercado_tipo") != "PLAYER_TOTAL" or "Mixed" not in str(t.get("liga")):
            continue
        n += 1
        k = (t.get("event_id"), t.get("mercado"), t.get("linha"), t.get("selecao"))
        st = str(t.get("odd_status"))
        try:
            odd = float(t.get("odds") or 0)
        except (TypeError, ValueError):
            odd = 0.0
        pl = (t.get("score_home"), t.get("score_away"))
        prev = ult.get(k)
        if prev is not None:
            pst, podd, ppl = prev
            odd_mudou = abs(odd - podd) > 1e-9
            placar_mudou = pl != ppl
            muda_odd[(st, odd_mudou)] += 1
            if placar_mudou:
                (placar_andou_odd_mudou if odd_mudou else placar_andou_odd_igual)[st] += 1
            if pst != st:
                trans[(pst, st)] += 1
                trans_placar[(pst, st, "placar mudou" if placar_mudou else "placar igual")] += 1
        ult[k] = (st, odd, pl)

print("ticks:", n, "| linhas distintas:", len(ult))
print("\nodd muda entre ticks consecutivos da MESMA linha, por status:")
for st in ("0", "1"):
    m, i = muda_odd[(st, True)], muda_odd[(st, False)]
    print(f"  status {st}: odd mudou {m:,} | odd igual {i:,} | taxa de mudanca {m/max(1,m+i):.1%}")
print("\nquando o PLACAR andou, a odd:")
for st in ("0", "1"):
    a, b = placar_andou_odd_mudou[st], placar_andou_odd_igual[st]
    print(f"  status {st}: mudou {a:,} | ficou igual {b:,} | congelada em {b/max(1,a+b):.1%}")
print("\ntransicoes de status:", dict(trans))
print("transicoes x placar:", dict(trans_placar))
