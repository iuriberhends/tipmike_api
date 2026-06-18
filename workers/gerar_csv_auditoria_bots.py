#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gerar_csv_auditoria_bots.py  (v1)

1 CSV por bot com TODAS as apostas que apitaram + os confrontos H2H que
entraram na decisao de cada aposta.

Reconstrucao 100% fiel:
- ts da decisao = ts do tick que gerou a aposta (via tick_id).
  Fallback: apostado_em (diferenca de segundos, nao muda jogos passados).
- Confrontos = MESMA consulta do bot_executor (H2HCache), mas com o corte
  (ts < decisao) e a exclusao do jogo atual aplicados DENTRO do SQL, antes
  do LIMIT 100 -> recupera o conjunto que existia no banco no momento da
  aposta (nao os 100 mais recentes de hoje).
- Janela mostrada = MAIOR janela dos filtros do bot (comp + hist).
  Sem filtro de janela -> usa 50 de contexto.

RODAR a partir da RAIZ do tipmike_api (onde fica a pasta workers/):
    python gerar_csv_auditoria_bots.py
    python gerar_csv_auditoria_bots.py --bot 19
    python gerar_csv_auditoria_bots.py --out "E:\\auditoria_csv"

Saida em E:\\auditoria_csv (disco C: esta cheio).
"""

import argparse
import asyncio
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncpg

from workers.backtest_runner import (
    _coletar_todos_filtros,
    _parse_linha,
    ESPORTE_UI_PARA_BANCO,
)

DB_DSN = "postgresql://postgres:mikedb0702@localhost:5432/mikedb"
OUT_DIR_DEFAULT = r"E:\auditoria_csv"
LIMITE_CONFRONTOS = 100      # = H2HCache.LIMITE_JOGOS_POR_PAR
JANELA_FALLBACK = 50         # qdo o bot nao tem filtro de janela

SQL_CONFRONTOS = """
    SELECT DISTINCT ON (event_id)
        event_id, ts, jogador_a, jogador_b, score_home, score_away
    FROM ticks
    WHERE bookmaker = $1
      AND sport = $2
      AND ((jogador_a = $3 AND jogador_b = $4)
        OR (jogador_a = $4 AND jogador_b = $3))
      AND score_home IS NOT NULL
      AND score_away IS NOT NULL
      AND ts < $5
      AND event_id::text <> $6
    ORDER BY event_id, ts DESC
    LIMIT $7
"""

CABECALHO = [
    "aposta_id", "apostado_em", "status", "resultado",
    "casa", "esporte", "liga", "event_id",
    "jogador_a", "jogador_b",
    "mercado", "mercado_tipo", "linha", "selecao", "odd", "stake",
    "placar_entrada", "live_time",
    "qtd_h2h", "motivo", "pnl", "lucro_unidades",
    "confronto_n", "confronto_data", "confronto_event_id",
    "confronto_jogador_a", "confronto_jogador_b",
    "confronto_placar", "confronto_total", "passou_linha",
]


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", (s or "").strip())
    return (s[:40] or "bot").strip("_")


def _parse_json(v):
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return None


def maior_janela_do_bot(filtros: dict):
    janelas = set()
    for f in _coletar_todos_filtros(filtros or {}) or []:
        if not isinstance(f, dict):
            continue
        try:
            j = int(f.get("janela"))
        except (TypeError, ValueError):
            continue
        if 1 <= j <= LIMITE_CONFRONTOS:
            janelas.add(j)
    return max(janelas) if janelas else None


async def gerar_csv_bot(pool, bot, out_dir):
    bot_id = bot["id"]
    nome = bot["nome"]
    casa = (bot["casa"] or "").lower()
    esporte_banco = ESPORTE_UI_PARA_BANCO.get(bot["esporte"], bot["esporte"])
    filtros = _parse_json(bot["filtros"]) or {}
    janela = maior_janela_do_bot(filtros) or JANELA_FALLBACK

    async with pool.acquire() as conn:
        apostas = await conn.fetch("""
            SELECT a.id, a.apostado_em, a.status, a.resultado,
                   a.casa, a.esporte, a.liga, a.event_id,
                   a.jogador_a, a.jogador_b,
                   a.mercado, a.mercado_tipo, a.linha, a.selecao, a.odd, a.stake,
                   a.score_home_no_momento, a.score_away_no_momento, a.live_time,
                   a.tick_id, t.ts AS tick_ts,
                   a.stats_h2h, a.motivo, a.pnl, a.lucro_unidades
            FROM apostas a
            LEFT JOIN ticks t ON t.id = a.tick_id
            WHERE a.bot_id = $1
            ORDER BY a.apostado_em
        """, bot_id)

    if not apostas:
        return None

    caminho = os.path.join(out_dir, f"bot_{bot_id}_{_slug(nome)}.csv")
    total_confrontos = 0
    fb_count = 0

    with open(caminho, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=CABECALHO, delimiter=";", extrasaction="ignore")
        w.writeheader()

        async with pool.acquire() as conn:
            for ap in apostas:
                ts = ap["tick_ts"] or ap["apostado_em"]
                if ap["tick_ts"] is None:
                    fb_count += 1

                eid = str(ap["event_id"]) if ap["event_id"] is not None else "__none__"
                confrontos = await conn.fetch(
                    SQL_CONFRONTOS, casa, esporte_banco,
                    ap["jogador_a"], ap["jogador_b"], ts, eid, LIMITE_CONFRONTOS,
                )
                confrontos = sorted(confrontos, key=lambda r: r["ts"], reverse=True)[:janela]

                linha_ap = _parse_linha(ap["linha"])
                stats = _parse_json(ap["stats_h2h"]) or {}
                sh_e, sa_e = ap["score_home_no_momento"], ap["score_away_no_momento"]

                base = {
                    "aposta_id": ap["id"],
                    "apostado_em": ap["apostado_em"].isoformat(sep=" ") if ap["apostado_em"] else "",
                    "status": ap["status"],
                    "resultado": ap["resultado"] or "",
                    "casa": ap["casa"], "esporte": ap["esporte"], "liga": ap["liga"],
                    "event_id": ap["event_id"],
                    "jogador_a": ap["jogador_a"], "jogador_b": ap["jogador_b"],
                    "mercado": ap["mercado"], "mercado_tipo": ap["mercado_tipo"],
                    "linha": linha_ap if linha_ap is not None else ap["linha"],
                    "selecao": ap["selecao"],
                    "odd": float(ap["odd"]) if ap["odd"] is not None else "",
                    "stake": float(ap["stake"]) if ap["stake"] is not None else "",
                    "placar_entrada": f"{sh_e}-{sa_e}" if sh_e is not None and sa_e is not None else "",
                    "live_time": ap["live_time"] or "",
                    "qtd_h2h": stats.get("qtd_h2h", ""),
                    "motivo": ap["motivo"] or "",
                    "pnl": float(ap["pnl"]) if ap["pnl"] is not None else "",
                    "lucro_unidades": float(ap["lucro_unidades"]) if ap["lucro_unidades"] is not None else "",
                }

                if not confrontos:
                    row = dict(base)
                    row.update({k: "" for k in CABECALHO[22:]})
                    row["confronto_n"] = 0
                    w.writerow(row)
                    continue

                for i, c in enumerate(confrontos, 1):
                    sh = c["score_home"] or 0
                    sa = c["score_away"] or 0
                    total = sh + sa
                    if linha_ap is None:
                        passou = ""
                    elif total > linha_ap:
                        passou = "sim"
                    elif total < linha_ap:
                        passou = "nao"
                    else:
                        passou = "push"

                    row = dict(base)
                    row.update({
                        "confronto_n": i,
                        "confronto_data": c["ts"].isoformat(sep=" ") if c["ts"] else "",
                        "confronto_event_id": c["event_id"],
                        "confronto_jogador_a": c["jogador_a"],
                        "confronto_jogador_b": c["jogador_b"],
                        "confronto_placar": f"{sh}-{sa}",
                        "confronto_total": total,
                        "passou_linha": passou,
                    })
                    w.writerow(row)
                    total_confrontos += 1

    return {"bot_id": bot_id, "nome": nome, "arquivo": caminho,
            "apostas": len(apostas), "confrontos": total_confrontos,
            "janela": janela, "ts_fallback": fb_count}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", type=int, default=None, help="So esse bot_id")
    ap.add_argument("--out", default=OUT_DIR_DEFAULT, help="Pasta de saida")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=4, command_timeout=120)
    try:
        if args.bot is not None:
            bots = await pool.fetch("SELECT id, nome, casa, esporte, filtros FROM bots WHERE id = $1", args.bot)
        else:
            bots = await pool.fetch("SELECT id, nome, casa, esporte, filtros FROM bots ORDER BY id")
        if not bots:
            print("Nenhum bot encontrado.")
            return

        print(f"Gerando CSVs em: {args.out}\n")
        gerados = 0
        for bot in bots:
            try:
                res = await gerar_csv_bot(pool, bot, args.out)
            except Exception as e:
                print(f"  [ERRO] bot {bot['id']} ({bot['nome']}): {e}")
                continue
            if res is None:
                print(f"  bot {bot['id']:>3} {bot['nome'][:30]:<30} -> sem apostas (pulado)")
                continue
            gerados += 1
            extra = f"  (ts fallback: {res['ts_fallback']})" if res["ts_fallback"] else ""
            print(f"  bot {res['bot_id']:>3} {res['nome'][:30]:<30} -> "
                  f"{res['apostas']} apostas, {res['confrontos']} confrontos (janela {res['janela']}){extra}")
            print(f"        {res['arquivo']}")
        print(f"\nOK. {gerados} CSV(s) gerado(s).")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
