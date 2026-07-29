# -*- coding: utf-8 -*-
r"""
DIAGNOSTICO DO VAZAMENTO — roda o MESMO codigo do servico contra o MESMO banco
e mostra, jogo a jogo, o que entra na janela do Ult.10 pra aposta do KARMA x
TAAPZ das 20:03:54Z de 27/jul (a que o vivo viu 1.0 e o backtest teima 0.9).

USO (na VPS):
    cd /d C:\Users\Administrator\PyCharmMiscProject\tipmike_api
    C:\Users\Administrator\PyCharmMiscProject\.venv\Scripts\python.exe diag_leak.py

Imprime:
  - timezone da sessao asyncpg (a promocao da UNION depende disso)
  - se o modulo importado tem os fixes v12/v12.1
  - a lista de jogos do par POS-DEDUP (ts como chega, fonte, placar,
    event_id, event_id_tick herdado, fim herdado)
  - a lista POS-CUTOFF pra aposta das 20:03:54 e a janela dos 10
Nao grava nada em lugar nenhum. So le.
"""
import asyncio, os, sys, inspect, traceback
from datetime import datetime, timezone

BASE = r"C:\Users\Administrator\PyCharmMiscProject\tipmike_api"
sys.path.insert(0, BASE)
os.chdir(BASE)

DSN = "postgresql://postgres:mikedb0702@localhost:5432/mikedb"
JA, JB = "KARMA", "TAAPZ"
APOSTA = datetime(2026, 7, 27, 20, 3, 54, tzinfo=timezone.utc)


def _fmt(dt):
    try:
        return dt.isoformat(sep=' ', timespec='seconds')
    except Exception:
        return str(dt)


async def main():
    try:
        import asyncpg
        import workers.backtest_runner as r
    except Exception as e:
        print(f"ERRO importando: {e}")
        traceback.print_exc()
        return

    src = inspect.getsource(r)
    print(f"modulo: {r.__file__}")
    print(f"fixes no modulo -> v12(event_id_tick): {'event_id_tick' in src} | "
          f"v12.1(Sao_Paulo): {'America/Sao_Paulo' in src}")
    print(f"MARGEM_AO_VIVO_MIN={r.H2HCache.MARGEM_AO_VIVO_MIN} | "
          f"margem_hist(basquete)={r._margem_hist_min_por_esporte('nba2k')}")

    try:
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    except Exception as e:
        print(f"ERRO conectando no banco: {e}")
        return

    try:
        async with pool.acquire() as c:
            tz = await c.fetchval("SHOW timezone")
            print(f"timezone da sessao asyncpg: {tz}")

        achou_algum = False
        for esporte in ("nba2k", "E-Basketball", "e-basketball", "basketball"):
            try:
                cache = r.H2HCache(pool, "superbet", esporte)
                jogos = await cache._buscar(JA, JB)
            except Exception as e:
                print(f"[sport={esporte}] ERRO no _buscar: {e}")
                continue
            if not jogos:
                print(f"[sport={esporte}] 0 jogos")
                continue
            achou_algum = True
            print(f"\n===== sport={esporte!r}: {len(jogos)} jogos POS-DEDUP "
                  f"(mais recente primeiro) =====")
            for j in jogos[:18]:
                print(f"  ts={_fmt(j.get('ts')):<25} fonte={str(j.get('fonte')):<5} "
                      f"placar={j.get('score_home')}x{j.get('score_away'):<4} "
                      f"ev={str(j.get('event_id')):<12} "
                      f"ev_tick={str(j.get('event_id_tick', '-')):<12} "
                      f"fim={_fmt(j.get('ultimo_tick_ts')) if j.get('ultimo_tick_ts') else '-'}")
            if len(jogos) > 18:
                print(f"  ... (+{len(jogos)-18} mais antigos)")

            try:
                cortados = r._aplicar_cutoff_jogos(
                    jogos, APOSTA, None,
                    r.H2HCache.MARGEM_AO_VIVO_MIN,
                    r._margem_hist_min_por_esporte(esporte))
            except TypeError:
                # assinatura velha (modulo sem v12) — denuncia na hora
                print("  >>> _aplicar_cutoff_jogos NAO aceita margem_hist: "
                      "modulo carregado e ANTERIOR ao v12!")
                cortados = r._aplicar_cutoff_jogos(
                    jogos, APOSTA, None, r.H2HCache.MARGEM_AO_VIVO_MIN)
            print(f"  -> POS-CUTOFF p/ aposta {_fmt(APOSTA)}: {len(cortados)} jogos; "
                  f"JANELA 10 (o que o Ult.10 enxerga):")
            for j in cortados[:10]:
                print(f"     ts={_fmt(j.get('ts')):<25} fonte={str(j.get('fonte')):<5} "
                      f"placar={j.get('score_home')}x{j.get('score_away')} "
                      f"ev={j.get('event_id')}")
        if not achou_algum:
            print("\nNENHUM esporte retornou jogos — me diga qual valor de "
                  "'sport' o bot 54 usa que eu ajusto.")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
