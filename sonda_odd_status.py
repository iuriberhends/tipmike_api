"""
SONDA: no gz de HOJE da Superbet, como vem odd_status x odd no PLAYER_TOTAL
(total por time) da H2H GG League Mixed. Tolera o gz ainda aberto.

Uso (na pasta tipmike_api):
  ..\\.venv\\Scripts\\python.exe sonda_odd_status.py > C:\\Users\\Administrator\\Downloads\\odd_status.txt
"""
import gzip, json, glob, collections

PASTA = r"C:\Users\Administrator\PyCharmMiscProject\MikeBacktest"
fs = sorted(glob.glob(PASTA + r"\superbet_ticks_2026-09-*.jsonl.gz"))
if not fs:
    print("nenhum gz da superbet encontrado em", PASTA)
    raise SystemExit(1)
gz = fs[-1]
print("gz:", gz)

st = collections.Counter()      # (odd_status, odd>1?) -> n
ex = {}                         # exemplo por chave
ligas = collections.Counter()
n = 0
with gzip.open(gz, "rt", errors="replace") as f:
    while True:
        try:
            l = f.readline()
        except (EOFError, OSError):
            break               # gz aberto: acabou o que da pra ler
        if not l:
            break
        try:
            t = json.loads(l)
        except Exception:
            continue
        if t.get("mercado_tipo") != "PLAYER_TOTAL":
            continue
        ligas[t.get("liga")] += 1
        if "Mixed" not in str(t.get("liga")):
            continue
        n += 1
        try:
            odd = float(t.get("odds") or 0)
        except (TypeError, ValueError):
            odd = 0.0
        k = (str(t.get("odd_status")), "odd>1" if odd > 1 else "odd<=1")
        st[k] += 1
        ex.setdefault(k, (t.get("mercado"), t.get("selecao"), t.get("linha"),
                          t.get("odds"), t.get("live_time"), t.get("score_home"),
                          t.get("score_away")))

print("ticks PLAYER_TOTAL Mixed hoje:", n)
for k, v in sorted(st.items(), key=lambda x: -x[1]):
    print(f"  odd_status={k[0]!r:16} {k[1]:7} -> {v:,}")
print("\nexemplos:")
for k, v in ex.items():
    print(" ", k, "->", v)
print("\nligas com PLAYER_TOTAL hoje:", dict(ligas.most_common(8)))
