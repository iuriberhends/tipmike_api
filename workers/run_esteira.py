# -*- coding: utf-8 -*-
r"""
workers/run_esteira.py — executa UMA rodada da esteira em processo PROPRIO.

Mesmo motivo do run_varredura.py: cada item da esteira roda o motor real
(minutos de CPU + parquet na RAM). Dentro da API isso paralisa o painel;
em processo proprio, a API so orquestra, da' pra matar pelo PID e o SO
pode dar prioridade baixa (quem chama com prioridade e' o daemon, passo 3).

USO:
    # rodar uma rodada ja criada (daemon/router chamam assim):
    python -m workers.run_esteira 3

    # criar E rodar na hora, sem router (pra testar o passo 2 hoje):
    python -m workers.run_esteira --novo --planilha estrategias.xlsx ^
        --fonte C:\...\MikeBacktest\acervo_betsapi_H2H.csv --dias 30 ^
        --nome "rodada blitz 30d"

    # outras origens:
    python -m workers.run_esteira --novo --planilha x.xlsx --upload-id C:\...\arq.parquet
    python -m workers.run_esteira --novo --planilha x.xlsx --banco superbet ^
        --de 2026-07-20 --ate 2026-08-18

SAIDA: 0 = concluida (resultado no banco + esteiras\esteira_<id>.xlsx);
       1 = falhou (motivo no campo `erro` da rodada e no log);
       3 = cancelada pela tela.
"""
import argparse
import asyncio
import json
import logging
import sys
import traceback

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_esteira")


async def _marcar_erro(job_id: int, msg: str):
    """Ultimo recurso: rodada nao pode ficar 'rodando' pra sempre no banco."""
    try:
        from database import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE esteira_jobs
                      SET status = 'erro', erro = $2,
                          progresso_msg = NULL, finalizado_em = NOW()
                    WHERE id = $1
                      AND status IN ('pendente', 'preparando', 'rodando')""",
                job_id, msg[:1000])
    except Exception:
        logger.exception("[run_esteira] nao consegui nem marcar o erro")


async def _criar(a) -> int:
    """--novo: INSERT da rodada com params montados da CLI."""
    from database import get_pool
    pool = get_pool()
    params = {}
    if a.upload_id:
        params["upload_id"] = a.upload_id
    elif a.banco:
        params["fonte"] = "banco"
        params["casa"] = a.banco
        if not (a.de and a.ate):
            print("ERRO: --banco exige --de e --ate (YYYY-MM-DD)")
            sys.exit(2)
        params["data_inicio"], params["data_fim"] = a.de, a.ate
    elif a.fonte:
        params["fonte_arquivo"] = a.fonte
        if a.dias and str(a.dias).lower() not in ("tudo", "all", "0"):
            params["dias"] = int(a.dias)
    else:
        print("ERRO: --novo exige --fonte, --upload-id ou --banco")
        sys.exit(2)
    params["planilha"] = a.planilha
    if a.sem_sentinela:
        params["sem_sentinela"] = True
    if a.max_zerados is not None:
        params["max_zerados"] = int(a.max_zerados)
    async with pool.acquire() as conn:
        # user_id de algum dono existente (mesmo truque dos bots por SQL)
        uid = await conn.fetchval(
            "SELECT user_id FROM bots WHERE user_id IS NOT NULL LIMIT 1")
        jid = await conn.fetchval(
            """INSERT INTO esteira_jobs (user_id, nome, origem, origem_ref,
                                         params, status)
               VALUES ($1, $2, 'planilha', $3, $4::jsonb, 'pendente')
               RETURNING id""",
            uid, a.nome or f"rodada {a.planilha}", a.planilha,
            json.dumps(params, ensure_ascii=False))
    logger.info(f"[run_esteira] rodada {jid} criada (params: {params})")
    return int(jid)


async def _principal(a) -> int:
    import asyncpg
    import database
    from workers.esteira_job import (executar_esteira, EsteiraErro,
                                     EsteiraCancelada)

    # pool minusculo: este processo consulta pouco (le o job, grava progresso
    # e resultados); o gasto real e' o motor. Nao roubar conexao do servidor.
    database._pool = await asyncpg.create_pool(database.DSN, min_size=1,
                                               max_size=2)
    job_id = None
    try:
        job_id = await _criar(a) if a.novo else int(a.job_id)
        logger.info(f"[run_esteira] iniciando rodada {job_id}")
        await executar_esteira(job_id)
        logger.info(f"[run_esteira] rodada {job_id} concluida")
        return 0
    except EsteiraCancelada:
        logger.info(f"[run_esteira] rodada {job_id} cancelada pela tela")
        return 3
    except EsteiraErro as e:
        logger.error(f"[run_esteira] rodada {job_id}: {e}")
        if job_id:
            await _marcar_erro(job_id, str(e))
        return 1
    except Exception as e:
        logger.error(f"[run_esteira] rodada {job_id} FALHOU: {e}")
        traceback.print_exc()
        if job_id:
            await _marcar_erro(job_id, f"{type(e).__name__}: {e}")
        return 1
    finally:
        try:
            if database._pool:
                await database._pool.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="run_esteira",
        description="Roda uma rodada da esteira (motor real, fora da API).")
    ap.add_argument("job_id", nargs="?", help="id da rodada ja criada")
    ap.add_argument("--novo", action="store_true",
                    help="cria a rodada agora (exige --planilha + origem)")
    ap.add_argument("--planilha", "-p", default="estrategias.xlsx")
    ap.add_argument("--fonte", "-f", default=None,
                    help="csv/parquet de ticks (recorta com --dias)")
    ap.add_argument("--dias", "-d", default=None,
                    help="ultimos N dias da fonte, ou 'tudo'")
    ap.add_argument("--upload-id", default=None,
                    help="parquet JA preparado no UPLOAD_DIR (usa direto)")
    ap.add_argument("--banco", default=None,
                    help="fonte banco: nome da casa (exige --de/--ate)")
    ap.add_argument("--de", default=None, help="YYYY-MM-DD (fonte banco)")
    ap.add_argument("--ate", default=None, help="YYYY-MM-DD (fonte banco)")
    ap.add_argument("--nome", default=None, help="rotulo da rodada")
    ap.add_argument("--sem-sentinela", action="store_true",
                    help="desliga a calibracao obrigatoria (explicito e feio "
                         "de proposito)")
    ap.add_argument("--max-zerados", default=None,
                    help="aborta apos N itens seguidos com 0 apostas "
                         "(0 desliga; default 3)")
    a = ap.parse_args()
    if not a.novo and not (a.job_id and str(a.job_id).isdigit()):
        ap.print_help()
        return 2
    return asyncio.run(_principal(a))


if __name__ == "__main__":
    sys.exit(main())
