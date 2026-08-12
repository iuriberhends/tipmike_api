# -*- coding: utf-8 -*-
r"""
workers/run_varredura.py — executa UMA varredura em processo PROPRIO.

Mesmo motivo do run_job.py, so que pior: o backtest sao minutos de CPU, o
garimpo sao HORAS. Se rodasse dentro da API (BackgroundTasks), o painel
simplesmente parava de responder enquanto voce garimpa — e garimpo e' coisa
de deixar rodando de madrugada.

Rodando fora:
  - a API nunca engasga (o loop dela so orquestra);
  - da' pra matar o garimpo pelo PID sem reiniciar nada;
  - se estourar memoria, morre so ele;
  - o sistema operacional consegue dar PRIORIDADE BAIXA pro processo, entao o
    executor de bots e os coletores sempre passam na frente (quem chama e' o
    varredura_daemon.py).

USO (o daemon chama assim; da' pra rodar na mao pra depurar):
    python -m workers.run_varredura 12
    ..\.venv\Scripts\python.exe -m workers.run_varredura 12

SAIDA: 0 = terminou (resultado ja gravado no banco); 1 = falhou (o motivo vai
pro log e pro campo `erro` do job).
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
logger = logging.getLogger("run_varredura")


async def _marcar_erro(job_id: int, msg: str):
    """Ultimo recurso. Job nao pode ficar 'rodando' pra sempre no banco — foi
    isso que sujou a tabela de backtest no dia 03/ago."""
    try:
        from database import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE varredura_jobs
                      SET status = 'erro', erro = $2, progresso_msg = NULL,
                          concluido_em = NOW()
                    WHERE id = $1 AND status IN ('pendente', 'planejando',
                                                 'planejado', 'rodando')""",
                job_id, msg[:1000])
    except Exception:
        logger.exception("[run_varredura] nao consegui nem marcar o erro")


async def _principal(job_id: int) -> int:
    import asyncpg
    import database
    from workers.varredura_job import executar_varredura, VarreduraErro

    # pool minusculo: este processo faz pouquissima consulta (le o job, grava
    # progresso). O gasto real e' CPU. Deixar 10 conexoes aqui seria roubar do
    # servidor, que ja divide 100 com API + coletores + backtests.
    database._pool = await asyncpg.create_pool(database.DSN, min_size=1,
                                               max_size=2)
    try:
        logger.info(f"[run_varredura] iniciando job {job_id}")
        await executar_varredura(job_id)
        logger.info(f"[run_varredura] job {job_id} finalizado")
        return 0
    except VarreduraErro as e:
        # erro de negocio: a mensagem e' pro usuario ler, sem traceback
        logger.error(f"[run_varredura] job {job_id}: {e}")
        await _marcar_erro(job_id, str(e))
        return 1
    except Exception as e:
        logger.error(f"[run_varredura] job {job_id} FALHOU: {e}")
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
        print("uso: python -m workers.run_varredura <job_id>")
        return 2
    return asyncio.run(_principal(int(sys.argv[1])))


if __name__ == "__main__":
    sys.exit(main())
