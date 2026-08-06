# -*- coding: utf-8 -*-
r"""
workers/run_job.py — executa UM backtest em processo PROPRIO.

POR QUE ISTO EXISTE (medido em 03/ago, com a VPS ja upgradada):
    O job rodava via BackgroundTasks, ou seja, DENTRO do processo da API e no
    MESMO event loop que atende as requisicoes. Como o backtest e' trabalho de
    CPU puro (milhoes de ticks em Python), o loop ficava sem folga e a API
    parava de responder: /docs levou 10,7 SEGUNDOS com um unico job ativo, e
    com 4 jobs empilhados a pagina simplesmente nao carregava.
    A maquina tem 24 nucleos — 23 assistiam.

O QUE MUDA:
    A API vira so despachante: grava o job e dispara ESTE script como
    subprocesso. Efeitos:
      - a API nunca mais engasga por causa de backtest (o loop so orquestra);
      - jobs rodam em PARALELO DE VERDADE (um processo por job, um nucleo
        cada) em vez de disputar o mesmo loop;
      - job que morre (estouro de memoria, bug) NAO derruba nem trava a API;
      - dá pra matar um job pelo PID sem reiniciar nada.

USO (a API chama assim; da pra rodar na mao pra depurar):
    python -m workers.run_job 549
    ..\.venv\Scripts\python.exe -m workers.run_job 549

SAIDA: codigo 0 = terminou (o resultado ja esta no banco, gravado pelo
runner); 1 = falhou (o motivo vai pro log e pro campo `erro` do job, quando
possivel).
"""

import asyncio
import logging
import sys
import traceback

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_job")


async def _marcar_erro(job_id: int, msg: str):
    """Ultimo recurso: se o runner morreu sem gravar, o job nao pode ficar
    'rodando' pra sempre no banco (foi o que sujou a tabela hoje)."""
    try:
        from database import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE backtest_jobs
                      SET status = 'erro', erro = $2, concluido_em = NOW()
                    WHERE id = $1 AND status = 'rodando'""",
                job_id, msg[:500])
    except Exception:
        logger.exception("[run_job] nao consegui nem marcar o erro no banco")


async def _principal(job_id: int) -> int:
    import asyncpg
    import database
    from workers.backtest_runner import executar_backtest

    # POOL ENXUTO deste processo. O init_pool() padrao abre 2-10 conexoes —
    # bom pra API (que atende muita gente), desperdicio pro job (que consulta
    # em serie). Com max_connections=100 no servidor e ate 4 jobs + API +
    # coletores + atualizar_h2h disputando, 10 por job estouraria a conta.
    # 3 e' folgado: o runner nao faz consulta concorrente.
    database._pool = await asyncpg.create_pool(database.DSN, min_size=1,
                                               max_size=3)
    try:
        logger.info(f"[run_job] iniciando job {job_id}")
        await executar_backtest(job_id)
        logger.info(f"[run_job] job {job_id} concluido")
        return 0
    except Exception as e:
        logger.error(f"[run_job] job {job_id} FALHOU: {e}")
        traceback.print_exc()
        await _marcar_erro(job_id, f"{type(e).__name__}: {e}")
        return 1
    finally:
        try:
            if database._pool:
                await database._pool.close()
        except Exception:
            pass


def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        print("uso: python -m workers.run_job <job_id>")
        return 2
    return asyncio.run(_principal(int(sys.argv[1])))


if __name__ == "__main__":
    sys.exit(main())
