# -*- coding: utf-8 -*-
r"""
investigar_ticks_dia21.py — as duas fontes de tick, lado a lado.

O replay do dia 21 (backtest 1394 x bot 117) divergiu em 3 jogos. O backtest
leu o parquet do coletor BETSAPI; o bot leu os ticks do coletor BET365 (JBot,
tabela `ticks`). Este script poe as duas fontes lado a lado NESSES jogos e
responde, por aposta divergente: o tick existia na outra fonte?

Uso (na VPS, raiz do tipmike_api):
    ..\.venv\Scripts\python.exe investigar_ticks_dia21.py <caminho_do_parquet_dia21>

So le — nao escreve nada em lugar nenhum.
"""
import asyncio
import sys

import pandas as pd

DSN = "postgresql://postgres:mikedb0702@127.0.0.1:5432/mikedb"
FUSO_H = 3          # parquet BetsAPI em UTC; vivo em local (UTC-3)

# os 3 jogos divergentes (hora LOCAL) + folga
JOGOS = [
    ("faLcOn", "Lalkoff", "2026-08-21 03:15", "2026-08-21 03:40",
     "backtest fez -3.5 e +1.5(!); bot fez -4.5(red) e -3.5"),
    ("Lalkoff", "faLcOn", "2026-08-21 06:25", "2026-08-21 06:45",
     "bot fez -4.5 e -3.5 (greens); backtest so a -2.5"),
    ("faLcOn", "lucker", "2026-08-21 07:50", "2026-08-21 08:15",
     "backtest fez -4.5 e -2.5; bot 117 nao apostou (116/118 pegaram -5.5)"),
]


def _acha_col(cols, candidatos):
    baixo = {str(c).lower(): c for c in cols}
    for cand in candidatos:
        if cand in baixo:
            return baixo[cand]
    for cand in candidatos:            # match por prefixo
        for k, v in baixo.items():
            if k.startswith(cand):
                return v
    return None


def carregar_parquet(caminho):
    df = pd.read_parquet(caminho)
    print(f"[parquet] {len(df):,} ticks · colunas: {list(df.columns)[:18]}")
    c_ts = _acha_col(df.columns, ["ts", "timestamp", "datahora", "created"])
    c_ja = _acha_col(df.columns, ["jogador_a", "player_a", "home"])
    c_jb = _acha_col(df.columns, ["jogador_b", "player_b", "away"])
    c_li = _acha_col(df.columns, ["linha", "handicap", "line", "hc"])
    c_od = _acha_col(df.columns, ["odd", "odds", "price"])
    c_sa = _acha_col(df.columns, ["score_home", "placar_a", "sa"])
    c_sb = _acha_col(df.columns, ["score_away", "placar_b", "sb"])
    c_me = _acha_col(df.columns, ["mercado", "market", "tipo"])
    faltam = [n for n, c in [("ts", c_ts), ("jogador_a", c_ja),
                             ("jogador_b", c_jb), ("linha", c_li)] if c is None]
    if faltam:
        print(f"!! nao achei no parquet: {faltam} — me mande a lista de "
              "colunas acima que eu ajusto o script")
        sys.exit(1)
    out = pd.DataFrame({
        "ts": pd.to_datetime(df[c_ts]) - pd.Timedelta(hours=FUSO_H),
        "ja": df[c_ja].astype(str), "jb": df[c_jb].astype(str),
        "linha": pd.to_numeric(df[c_li], errors="coerce"),
        "odd": pd.to_numeric(df[c_od], errors="coerce") if c_od else None,
        "sa": pd.to_numeric(df[c_sa], errors="coerce") if c_sa else None,
        "sb": pd.to_numeric(df[c_sb], errors="coerce") if c_sb else None,
        "mercado": df[c_me].astype(str) if c_me else "",
    })
    return out


async def carregar_banco(ja, jb, ini, fim):
    import asyncpg
    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch(
            """SELECT * FROM ticks
                WHERE bookmaker = 'bet365'
                  AND ts BETWEEN $1::timestamp AND $2::timestamp
                  AND ((lower(jogador_a) = lower($3) AND lower(jogador_b) = lower($4))
                    OR (lower(jogador_a) = lower($4) AND lower(jogador_b) = lower($3)))
                ORDER BY ts""",
            pd.Timestamp(ini).to_pydatetime(), pd.Timestamp(fim).to_pydatetime(),
            ja, jb)
    finally:
        await conn.close()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    c_li = _acha_col(df.columns, ["linha", "handicap", "line"])
    c_od = _acha_col(df.columns, ["odd", "odds"])
    c_sa = _acha_col(df.columns, ["score_home", "placar_a"])
    c_sb = _acha_col(df.columns, ["score_away", "placar_b"])
    c_me = _acha_col(df.columns, ["mercado", "market", "mercado_tipo"])
    out = pd.DataFrame({
        "ts": pd.to_datetime(df["ts"]),
        "ja": df["jogador_a"].astype(str), "jb": df["jogador_b"].astype(str),
        "linha": pd.to_numeric(df[c_li], errors="coerce") if c_li else None,
        "odd": pd.to_numeric(df[c_od], errors="coerce") if c_od else None,
        "sa": pd.to_numeric(df[c_sa], errors="coerce") if c_sa else None,
        "sb": pd.to_numeric(df[c_sb], errors="coerce") if c_sb else None,
        "mercado": df[c_me].astype(str) if c_me else "",
    })
    return out


def _fmt(df, rotulo):
    if df is None or len(df) == 0:
        print(f"    {rotulo}: NENHUM tick na janela")
        return
    print(f"    {rotulo}: {len(df)} ticks")
    for _, r in df.iterrows():
        od = f"{r['odd']:.3f}" if pd.notna(r.get("odd")) else "  -  "
        pl = (f"{int(r['sa'])}-{int(r['sb'])}"
              if pd.notna(r.get("sa")) and pd.notna(r.get("sb")) else "?-?")
        me = str(r.get("mercado") or "")[:14]
        print(f"      {r['ts'].strftime('%H:%M:%S')}  L{r['linha']:+.1f}  "
              f"odd {od}  placar {pl}  {me}")


def main():
    if len(sys.argv) < 2:
        print("uso: python investigar_ticks_dia21.py <parquet_do_dia_21>")
        sys.exit(1)
    pq = carregar_parquet(sys.argv[1])

    for ja, jb, ini, fim, contexto in JOGOS:
        print("\n" + "=" * 74)
        print(f" {ja} x {jb} — {ini[11:]} a {fim[11:]} (hora local)")
        print(f" contexto: {contexto}")
        print("=" * 74)
        m = pq[(pq["ts"] >= pd.Timestamp(ini)) & (pq["ts"] <= pd.Timestamp(fim))
               & (((pq["ja"].str.lower() == ja.lower()) & (pq["jb"].str.lower() == jb.lower()))
                  | ((pq["ja"].str.lower() == jb.lower()) & (pq["jb"].str.lower() == ja.lower())))]
        # so HC quando o parquet tem varios mercados
        if "mercado" in m.columns and m["mercado"].str.len().gt(0).any():
            hc = m[m["mercado"].str.contains("ah|hand|hc", case=False, na=False)]
            if len(hc):
                m = hc
        _fmt(m.sort_values("ts"), "BETSAPI (parquet, ts ja convertido pra local)")
        try:
            b = asyncio.run(carregar_banco(ja, jb, ini, fim))
        except Exception as e:
            print(f"    BANCO: erro consultando ticks: {e}")
            continue
        if len(b) and "mercado" in b.columns and b["mercado"].str.len().gt(0).any():
            hcb = b[b["mercado"].str.contains("ah|hand|hc|1446", case=False, na=False)]
            if len(hcb):
                b = hcb
        _fmt(b.sort_values("ts") if len(b) else b, "COLETOR BET365 (tabela ticks)")

        # o veredito por linha: quem tem o que
        if len(m) and len(b):
            la = set(m["linha"].dropna().round(1))
            lb = set(b["linha"].dropna().round(1))
            so_api = sorted(la - lb)
            so_bot = sorted(lb - la)
            if so_api:
                print(f"    >> linhas SO na BetsAPI: {so_api}")
            if so_bot:
                print(f"    >> linhas SO no coletor bet365: {so_bot}")
            if not so_api and not so_bot:
                print("    >> mesmas linhas nas duas fontes (diferenca e' so timing)")

    print("\nleitura: aposta divergente cujo tick NAO existe na outra fonte = "
          "cobertura de coleta (nao e' bug de filtro). Tick presente nas duas "
          "e decisao diferente = ai sim e' o tradutor/filtro.")


if __name__ == "__main__":
    main()
