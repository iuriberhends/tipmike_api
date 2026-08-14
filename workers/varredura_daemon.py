# -*- coding: utf-8 -*-
r"""
workers/varredura_daemon.py — a FILA das varreduras.

Servico proprio (nssm), separado da API e do backtest. Faz tres coisas:

  1. pega job 'pendente' e sobe ate SLOTS processos (default 2);
  2. sobe com PRIORIDADE BAIXA — o garimpo roda por horas, entao ele tem que
     ser o primeiro a ceder CPU quando o executor de bots ou os coletores
     precisarem. Sinal perdido e' dinheiro; garimpo atrasado e' so' espera;
  3. faz a faxina: job orfao (daemon/maquina caiu no meio), cancelamento
     pedido pela tela, e job que passou do tempo limite.

POR QUE FILA E NAO A API DISPARANDO DIRETO:
  - reiniciar a API nao mata garimpo em andamento;
  - da' pra pausar a fila num dia de jogo pesado (PAUSA_ARQ);
  - impede 5 garimpos simultaneos comendo 5 nucleos e 5 copias do dado.

INSTALAR COMO SERVICO:
    nssm install TipMikeVarredura "C:\...\.venv\Scripts\python.exe" ^
        "-m" "workers.varredura_daemon"
    nssm set TipMikeVarredura AppDirectory "C:\...\tipmike_api"
    nssm start TipMikeVarredura

RODAR NA MAO (pra ver o log):
    python -m workers.varredura_daemon
"""
import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("varredura_daemon")

RAIZ = Path(__file__).resolve().parent.parent
SLOTS = int(os.environ.get("VARREDURA_SLOTS", "2"))
POLL_S = int(os.environ.get("VARREDURA_POLL", "5"))
# garimpo que passar disso e' considerado travado e e' morto
TIMEOUT_H = float(os.environ.get("VARREDURA_TIMEOUT_H", "12"))
# criar este arquivo pausa a fila (nao mata o que ja roda)
PAUSA_ARQ = RAIZ / "varredura_pausada.flag"

# Windows: prioridade abaixo do normal + sem abrir janela de console
_BELOW_NORMAL = 0x00004000
_NO_WINDOW = 0x08000000


def _flags_prioridade():
    if os.name == "nt":
        return {"creationflags": _BELOW_NORMAL | _NO_WINDOW}
    # Linux/Mac: nice 10 tem o mesmo efeito pratico
    return {"preexec_fn": lambda: os.nice(10)}


def _subir(job_id: int) -> subprocess.Popen:
    """Sobe o processo do job. A saida vai pra UM LOG POR JOB — mandar pro
    DEVNULL escondeu um bug por horas: o worker recusava o job e avisava no
    stdout, que ninguem via. Log de processo filho e' barato; depurar as cegas
    nao e'."""
    cmd = [sys.executable, "-m", "workers.run_varredura", str(job_id)]
    pasta = RAIZ / "varreduras"
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        saida = open(pasta / f"varredura_{job_id}.log", "a", encoding="utf-8",
                     errors="replace")
    except Exception:
        saida = subprocess.DEVNULL
    return subprocess.Popen(cmd, cwd=str(RAIZ), stdout=saida,
                            stderr=subprocess.STDOUT, **_flags_prioridade())


def _pid_vivo(pid):
    """O processo daquele job ainda esta de pe? Sem isso, reiniciar o servico
    (pra ligar um log, por exemplo) MATAVA no banco um garimpo que continuava
    rodando feliz — o filho sobrevive ao pai no Windows."""
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except ImportError:
        pass
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        cod = ctypes.c_ulong()
        k32.GetExitCodeProcess(h, ctypes.byref(cod))
        k32.CloseHandle(h)
        return cod.value == STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


async def _faxina_inicial(pool):
    """Na subida, qualquer job 'rodando'/'planejando' e' ORFAO: o daemon e' o
    unico que sobe processo, entao se ele estava fora, ninguem estava cuidando.
    Deixar em 'rodando' pra sempre e' pior que marcar erro — o usuario fica
    olhando uma barra que nunca anda."""
    async with pool.acquire() as conn:
        abertos = await conn.fetch(
            """SELECT id, pid FROM varredura_jobs
                WHERE status IN ('rodando', 'planejando')""")
        orfaos, sobreviventes = [], []
        for r in abertos:
            (sobreviventes if _pid_vivo(r["pid"]) else orfaos).append(r["id"])
        if orfaos:
            await conn.execute(
                """UPDATE varredura_jobs
                      SET status = 'erro', concluido_em = NOW(),
                          erro = 'o servico da fila caiu no meio da rodada e o '
                                 'processo morreu junto — rode de novo (nada '
                                 'foi corrompido)'
                    WHERE id = ANY($1::int[])""", orfaos)
            logger.warning(f"[fila] {len(orfaos)} orfao(s) marcados: {orfaos}")
        if sobreviventes:
            # o processo continua vivo: NAO mexer. Ele termina e grava o
            # resultado sozinho; o daemon so nao o contabiliza nos slots.
            logger.info(f"[fila] {len(sobreviventes)} job(s) seguem rodando "
                        f"fora do meu controle: {sobreviventes} "
                        f"(nao ocupam slot ate terminarem)")


async def _cancelar_pedidos(pool, vivos: dict):
    """A tela marca status='cancelado'; aqui o processo e' morto de fato."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM varredura_jobs WHERE status = 'cancelado'")
    for r in rows:
        p = vivos.pop(r["id"], None)
        if p and p.poll() is None:
            try:
                p.kill()
                logger.info(f"[fila] job {r['id']} cancelado (processo morto)")
            except Exception:
                logger.exception(f"[fila] falha ao matar job {r['id']}")


async def _matar_travados(pool, vivos: dict):
    limite = datetime.now() - timedelta(hours=TIMEOUT_H)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id FROM varredura_jobs
                WHERE status = 'rodando' AND iniciado_em < $1""", limite)
        for r in rows:
            p = vivos.pop(r["id"], None)
            if p and p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass
            await conn.execute(
                """UPDATE varredura_jobs
                      SET status = 'erro', concluido_em = NOW(),
                          erro = $2
                    WHERE id = $1""",
                r["id"], f"passou de {TIMEOUT_H:g}h e foi interrompido. "
                         f"Reduza o escopo (--modo completo, menos janelas, "
                         f"min_apostas maior) e rode de novo.")
            logger.warning(f"[fila] job {r['id']} morto por timeout")


async def _laco():
    import asyncpg
    import database
    database._pool = await asyncpg.create_pool(database.DSN, min_size=1,
                                               max_size=3)
    pool = database._pool
    await _faxina_inicial(pool)
    logger.info(f"[fila] no ar | slots={SLOTS} | poll={POLL_S}s | "
                f"timeout={TIMEOUT_H:g}h | raiz={RAIZ}")

    vivos: dict = {}          # job_id -> Popen
    while True:
        try:
            # 1) recolhe os que terminaram
            for jid in [j for j, p in vivos.items() if p.poll() is not None]:
                cod = vivos.pop(jid).returncode
                logger.info(f"[fila] job {jid} saiu com codigo {cod}")

            await _cancelar_pedidos(pool, vivos)
            await _matar_travados(pool, vivos)

            # 2) fila pausada? nao pega job novo (mas deixa terminar o que roda)
            if PAUSA_ARQ.exists():
                await asyncio.sleep(POLL_S)
                continue

            # 3) sobe ate encher os slots
            livres = SLOTS - len(vivos)
            if livres > 0:
                async with pool.acquire() as conn:
                    # SKIP LOCKED: se um dia rodar 2 daemons, nao pegam o mesmo
                    rows = await conn.fetch(
                        """SELECT id FROM varredura_jobs
                            WHERE status = 'pendente'
                            ORDER BY criado_em
                            LIMIT $1 FOR UPDATE SKIP LOCKED""", livres)
                    for r in rows:
                        await conn.execute(
                            """UPDATE varredura_jobs
                                  SET status = 'planejando', iniciado_em = NOW()
                                WHERE id = $1""", r["id"])
                for r in rows:
                    try:
                        vivos[r["id"]] = _subir(r["id"])
                        logger.info(f"[fila] job {r['id']} iniciado "
                                    f"({len(vivos)}/{SLOTS} slots)")
                    except Exception as e:
                        logger.exception(f"[fila] nao subi o job {r['id']}")
                        async with pool.acquire() as conn:
                            await conn.execute(
                                """UPDATE varredura_jobs SET status='erro',
                                          erro=$2, concluido_em=NOW()
                                    WHERE id=$1""",
                                r["id"], f"falha ao iniciar o processo: {e}")
        except Exception:
            # o daemon NUNCA pode morrer por causa de um job ou de um hiccup do
            # banco — loga e continua
            logger.exception("[fila] erro no laco (seguindo)")
        await asyncio.sleep(POLL_S)


def main():
    sys.path.insert(0, str(RAIZ))
    try:
        asyncio.run(_laco())
    except KeyboardInterrupt:
        logger.info("[fila] encerrando")


if __name__ == "__main__":
    main()
