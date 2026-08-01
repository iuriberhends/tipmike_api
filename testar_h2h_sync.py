# -*- coding: utf-8 -*-
r"""
testar_h2h_sync.py — teste de fumaca do worker h2h_sync, SEM passar pela API
(sem token, sem front). Roda a ANALISE e, opcionalmente, o preenchimento em
modo SIMULACAO (nao grava nada).

USO (na VPS, da pasta do projeto):
    cd /d C:\Users\Administrator\PyCharmMiscProject\tipmike_api
    ..\.venv\Scripts\python.exe testar_h2h_sync.py --casa superbet --dias 3
    ..\.venv\Scripts\python.exe testar_h2h_sync.py --casa superbet --dias 3 --simular
    ..\.venv\Scripts\python.exe testar_h2h_sync.py --casa superbet --dias 15 --liga "H2H - GG League Mixed"

--simular consulta a TipManager de verdade (valida credenciais, AES e o mapa
de players) mas NAO insere: diz quantos jogos ENTRARIAM. Sem --simular, so
compara ticks x banco e nao toca na TM.
"""
import argparse
import asyncio
import sys

sys.path.insert(0, ".")

import database                      # noqa: E402
from workers import h2h_sync         # noqa: E402


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--casa", default="superbet")
    ap.add_argument("--esporte", default="nba2k")
    ap.add_argument("--liga", default=None)
    ap.add_argument("--dias", type=int, default=3)
    ap.add_argument("--min-confrontos", type=int, default=20)
    ap.add_argument("--simular", action="store_true",
                    help="consulta a TM e diz quantos entrariam (nao grava)")
    ap.add_argument("--preencher", action="store_true",
                    help="PREENCHE DE VERDADE (pede confirmacao digitada)")
    ap.add_argument("--limite", type=int, default=5,
                    help="quantos pares processar no --simular/--preencher")
    args = ap.parse_args()

    await database.init_pool()
    pool = database.get_pool()
    try:
        params = {"casa": args.casa, "esporte": args.esporte, "liga": args.liga,
                  "dias": args.dias, "min_confrontos": args.min_confrontos}
        print(f"analisando {args.casa}/{args.esporte} "
              f"{'liga=' + args.liga + ' ' if args.liga else ''}"
              f"ultimos {args.dias} dias...")
        rel = await h2h_sync.analisar(pool, params)

        print("\n=== DIAGNOSTICO (regua do runner: ticks + hist) ===")
        print(f"  pares no periodo ............. {rel['pares_total']}")
        print(f"  ja completos ................. {rel['pares_ok']}")
        print(f"  precisam ..................... {rel['pares_precisam']}")
        print(f"  jogos nos ticks .............. {rel['jogos_ticks_total']}")
        print(f"  cobertos pelo hist (ficam) ... {rel['cobertos_hist_total']}")
        print(f"  so na perna dos ticks ........ {rel['so_tick_total']}  <- sem copia permanente no hist")

        piores = [p for p in rel["pares"] if p["precisa"]][:15]
        if piores:
            print("\n  piores pares (so-tick / hist total / motivo):")
            for p in piores:
                print(f"    {p['jogador_a']:<14} x {p['jogador_b']:<14} "
                      f"so-tick {p['so_tick']:>3} | hist {p['jogos_hist']:>4} "
                      f"| {p['motivo']}")

        if args.simular or args.preencher:
            if not rel["pares_precisam"]:
                print("\nnada a simular: o historico ja cobre os pares.")
                return
            try:
                h2h_sync._creds()
            except RuntimeError as e:
                print(f"\n[ERRO] {e}")
                return
            dry = not args.preencher
            if not dry:
                print(f"\n>>> PREENCHIMENTO REAL: ate {args.limite} pares serao "
                      f"gravados no h2h_historico (com dedup + limpeza de bkp_).")
                if input(">>> digite SIM pra confirmar: ").strip().upper() != "SIM":
                    print("cancelado.")
                    return
            jid = h2h_sync._novo_job("preenchimento", {"teste": True})
            print(f"\n=== {'SIMULACAO (nao grava)' if dry else 'PREENCHENDO'} — "
                  f"ate {args.limite} pares ===")
            # roda como task e MONITORA o progresso que o worker publica no
            # job (etapa/percentual) — sem isso, pares fundos com o 24x
            # deixam o terminal minutos em silencio ("ficar no escuro").
            tarefa = asyncio.create_task(
                h2h_sync.preencher(pool, rel, jid,
                                   limite=args.limite, dry_run=dry))
            ultima = None
            while not tarefa.done():
                job = h2h_sync.JOBS.get(jid, {})
                linha = f"[{job.get('progresso', 0):>3}%] {job.get('etapa', '...')}"
                if linha != ultima:
                    print(f"  {linha}", flush=True)
                    ultima = linha
                await asyncio.sleep(2)
            rel2 = await tarefa
            chave = "jogos_que_entrariam" if dry else "jogos_inseridos"
            print(f"  {chave}: {rel2.get(chave)}"
                  + ("" if dry else f" | backups removidos: {rel2.get('backups_removidos')}"))
            for d in rel2["detalhe"]:
                n = d.get("entrariam", d.get("inseridos", 0))
                print(f"    {d['par']:<32} {'entrariam' if dry else 'inseridos'} {n:>4} "
                      f"| TM trouxe {d.get('tm_trouxe', 0):>4} | {d['obs']}")
    finally:
        await database.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
