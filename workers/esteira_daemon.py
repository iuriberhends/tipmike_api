# -*- coding: utf-8 -*-
r"""
workers/esteira_daemon.py — a FILA das rodadas da esteira (passo 3).

Servico proprio (nssm), separado da API, do backtest e da fila do varredor.
Mesmo dialeto do varredura_daemon.py, com TRES coisas a mais (o desenho de
19/ago: pools INDEPENDENTES, nao fila unica — a maquina tem CPU sobrando,
o gargalo e' RAM):

  1. PISO DE RAM (RAM_MIN_LIVRE_GB, default 6): antes de subir rodada, mede a
     RAM livre da maquina. Abaixo do piso, NAO sobe — foi um processo de 49 GB
     que derrubou o Postgres por 16h em 10/ago. Cada job da esteira carrega o
     parquet (Blitz 3-5 GB, BATTLE 1-2).
  2. TETO GLOBAL DE PESADOS (PESADOS_MAX, default 4): conta os processos vivos
     das DUAS filas (varredura planejando/rodando + esteira preparando/rodando,
     pids conferidos vivos) e nao passa do teto. Com os defaults 2+2 o teto
     fecha por construcao; a contagem blinda contra alguem subir os slots.
  3. MOTIVO DE ESPERA NO progresso_msg: quando ha rodada pendente e ela NAO
     sobe, o proximo da fila ganha o porque ("aguardando: RAM livre 4.2GB <
     piso 6GB"). Fila parada calada foi bug do varredor — nunca repetir.

O resto e' o padrao ja validado: prioridade BAIXA (garimpo/esteira cedem CPU
pros coletores e bots — sinal perdido e' dinheiro, backtest atrasado e' so'
espera), log por rodada em esteiras\esteira_<id>.log, faxina de orfao checando
_pid_vivo (o filho sobrevive ao pai no Windows), cancelamento pela tela,
timeout, e pausa por arquivo (esteira_pausada.flag).

DIFERENCA DELIBERADA da faxina em relacao ao varredor: rodada orfa (daemon
caiu, processo morto) NAO vira erro — volta pra 'pendente'. A retomada da
esteira pula os itens concluidos, entao re-subir e' seguro e barato.

INSTALAR COMO SERVICO (cmd como ADMIN):
    "C:\nssm-2.24\win64\nssm.exe" install TipMikeEsteira ^
        "C:\Users\Administrator\PyCharmMiscProject\.venv\Scripts\python.exe" ^
        "-m" "workers.esteira_daemon"
    "C:\nssm-2.24\win64\nssm.exe" set TipMikeEsteira AppDirectory ^
        "C:\Users\Administrator\PyCharmMiscProject\tipmike_api"
    "C:\nssm-2.24\win64\nssm.exe" start TipMikeEsteira

RODAR NA MAO (pra ver o log):
    python -m workers.esteira_daemon

SELF-TEST (sem banco):
    python -m workers.esteira_daemon --teste
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
logger = logging.getLogger("esteira_daemon")

RAIZ = Path(__file__).resolve().parent.parent
SLOTS = int(os.environ.get("ESTEIRA_SLOTS", "2"))
POLL_S = int(os.environ.get("ESTEIRA_POLL", "5"))
# rodada que passar disso e' considerada travada e e' morta (a esteira ja tem
# watchdog de 45min POR ITEM; isto e' o teto da RODADA inteira)
TIMEOUT_H = float(os.environ.get("ESTEIRA_TIMEOUT_H", "8"))
# criar este arquivo pausa a fila (nao mata o que ja roda)
PAUSA_ARQ = RAIZ / "esteira_pausada.flag"
# --- os dois freios do desenho de 19/ago ---
RAM_MIN_LIVRE_GB = float(os.environ.get("RAM_MIN_LIVRE_GB", "6"))
PESADOS_MAX = int(os.environ.get("PESADOS_MAX", "4"))

# Windows: prioridade abaixo do normal + sem abrir janela de console
_BELOW_NORMAL = 0x00004000
_NO_WINDOW = 0x08000000


def _flags_prioridade():
    if os.name == "nt":
        return {"creationflags": _BELOW_NORMAL | _NO_WINDOW}
    # Linux/Mac: nice 10 tem o mesmo efeito pratico
    return {"preexec_fn": lambda: os.nice(10)}


def _ram_livre_gb():
    """RAM fisica LIVRE da maquina, em GB. Blindado e FAIL-OPEN: se a medicao
    falhar, devolve None e a fila segue (nao medir nao pode travar tudo —
    mas fica avisado no log)."""
    try:
        import psutil
        return psutil.virtual_memory().available / (1024 ** 3)
    except ImportError:
        pass
    except Exception:
        logger.exception("[fila] psutil falhou medindo RAM")
    if os.name == "nt":
        try:
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = _MemStatus()
            st.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return st.ullAvailPhys / (1024 ** 3)
        except Exception:
            logger.exception("[fila] GlobalMemoryStatusEx falhou")
        return None
    try:
        with open("/proc/meminfo", encoding="ascii", errors="ignore") as f:
            for ln in f:
                if ln.startswith("MemAvailable:"):
                    return int(ln.split()[1]) / (1024 ** 2)
    except Exception:
        pass
    return None


def _pid_vivo(pid):
    """O processo daquele job ainda esta de pe? Sem isso, reiniciar o servico
    matava NO BANCO uma rodada que continuava rodando — o filho sobrevive ao
    pai no Windows."""
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


def _subir(job_id: int) -> subprocess.Popen:
    """Sobe o processo da rodada. Saida em UM LOG POR RODADA — DEVNULL ja
    escondeu bug por horas no varredor."""
    cmd = [sys.executable, "-m", "workers.run_esteira", str(job_id)]
    pasta = RAIZ / "esteiras"
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        saida = open(pasta / f"esteira_{job_id}.log", "a", encoding="utf-8",
                     errors="replace")
    except Exception:
        saida = subprocess.DEVNULL
    return subprocess.Popen(cmd, cwd=str(RAIZ), stdout=saida,
                            stderr=subprocess.STDOUT, **_flags_prioridade())


async def _pesados_no_ar(pool, vivos: dict) -> int:
    """Quantos processos PESADOS (varredura + esteira) estao vivos agora.
    Conta por PID unico: banco das duas filas + Popen locais (janela em que
    o worker ainda nao gravou o proprio pid). varredura_jobs ausente
    (ambiente de teste) nao derruba a contagem."""
    pids = set()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                """SELECT pid FROM varredura_jobs
                    WHERE status IN ('planejando', 'rodando')
                      AND pid IS NOT NULL""")
            pids.update(int(r["pid"]) for r in rows)
        except Exception:
            pass  # tabela pode nao existir fora da VPS
        try:
            rows = await conn.fetch(
                """SELECT pid FROM esteira_jobs
                    WHERE status IN ('preparando', 'rodando')
                      AND pid IS NOT NULL""")
            pids.update(int(r["pid"]) for r in rows)
        except Exception:
            logger.exception("[fila] falha lendo pids da esteira")
    vivos_banco = sum(1 for p in pids if _pid_vivo(p))
    # Popen locais ainda sem pid no banco (recem-subidos)
    locais = sum(1 for j, p in vivos.items()
                 if p.poll() is None and p.pid not in pids)
    return vivos_banco + locais


async def _avisar_espera(pool, motivo: str, cache: dict):
    """Escreve o motivo de nao subir no progresso_msg do PROXIMO da fila.
    So escreve quando o texto muda (nada de UPDATE por poll)."""
    async with pool.acquire() as conn:
        jid = await conn.fetchval(
            """SELECT id FROM esteira_jobs WHERE status = 'pendente'
                ORDER BY criado_em LIMIT 1""")
        if jid is None:
            return
        if cache.get(jid) == motivo:
            return
        await conn.execute(
            "UPDATE esteira_jobs SET progresso_msg = $2 WHERE id = $1",
            jid, motivo)
        cache[jid] = motivo
    logger.info(f"[fila] rodada {jid} aguardando: {motivo}")


async def _faxina_inicial(pool):
    """Na subida, rodada 'preparando'/'rodando' com processo MORTO e' orfa.
    Diferente do varredor: volta pra PENDENTE (a retomada pula o que ja
    concluiu), em vez de virar erro."""
    async with pool.acquire() as conn:
        abertos = await conn.fetch(
            """SELECT id, pid FROM esteira_jobs
                WHERE status IN ('preparando', 'rodando')""")
        orfaos, sobreviventes = [], []
        for r in abertos:
            (sobreviventes if _pid_vivo(r["pid"]) else orfaos).append(r["id"])
        if orfaos:
            await conn.execute(
                """UPDATE esteira_jobs
                      SET status = 'pendente', pid = NULL,
                          progresso_msg = 'fila caiu no meio; de volta a fila '
                                          '(retomada pula itens concluidos)'
                    WHERE id = ANY($1::int[])""", orfaos)
            logger.warning(f"[fila] {len(orfaos)} orfa(s) de volta a fila: "
                           f"{orfaos}")
        if sobreviventes:
            logger.info(f"[fila] {len(sobreviventes)} rodada(s) seguem vivas "
                        f"fora do meu controle: {sobreviventes} "
                        f"(contam no teto de pesados)")


async def _cancelar_pedidos(pool, vivos: dict):
    """A tela marca status='cancelado'; aqui o processo e' morto de fato.
    (O worker tambem checa entre itens, mas o kill garante o imediato.)"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM esteira_jobs WHERE status = 'cancelado'")
    for r in rows:
        p = vivos.pop(r["id"], None)
        if p and p.poll() is None:
            try:
                p.kill()
                logger.info(f"[fila] rodada {r['id']} cancelada "
                            f"(processo morto)")
            except Exception:
                logger.exception(f"[fila] falha ao matar rodada {r['id']}")


async def _matar_travadas(pool, vivos: dict):
    limite = datetime.now() - timedelta(hours=TIMEOUT_H)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id FROM esteira_jobs
                WHERE status = 'rodando' AND iniciado_em < $1""", limite)
        for r in rows:
            p = vivos.pop(r["id"], None)
            if p and p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass
            await conn.execute(
                """UPDATE esteira_jobs
                      SET status = 'erro', finalizado_em = NOW(), erro = $2
                    WHERE id = $1""",
                r["id"],
                f"rodada passou de {TIMEOUT_H:g}h e foi interrompida. O que "
                f"concluiu esta gravado; pra retomar do ponto: UPDATE "
                f"esteira_jobs SET status='pendente', erro=NULL WHERE "
                f"id={r['id']};")
            logger.warning(f"[fila] rodada {r['id']} morta por timeout")


async def _laco():
    import asyncpg
    import database
    database._pool = await asyncpg.create_pool(database.DSN, min_size=1,
                                               max_size=3)
    pool = database._pool
    await _faxina_inicial(pool)
    logger.info(f"[fila] no ar | slots={SLOTS} | poll={POLL_S}s | "
                f"timeout={TIMEOUT_H:g}h | ram_min={RAM_MIN_LIVRE_GB:g}GB | "
                f"pesados_max={PESADOS_MAX} | raiz={RAIZ}")

    vivos: dict = {}          # job_id -> Popen
    aviso_cache: dict = {}    # job_id -> ultimo motivo escrito
    while True:
        try:
            # 1) recolhe os que terminaram
            for jid in [j for j, p in vivos.items() if p.poll() is not None]:
                cod = vivos.pop(jid).returncode
                aviso_cache.pop(jid, None)
                logger.info(f"[fila] rodada {jid} saiu com codigo {cod}")

            await _cancelar_pedidos(pool, vivos)
            await _matar_travadas(pool, vivos)

            # 2) fila pausada? nao pega rodada nova (deixa terminar o que roda)
            if PAUSA_ARQ.exists():
                await asyncio.sleep(POLL_S)
                continue

            # 3) freios ANTES de olhar a fila: RAM e teto global
            livres = SLOTS - len(vivos)
            if livres > 0:
                ram = _ram_livre_gb()
                if ram is not None and ram < RAM_MIN_LIVRE_GB:
                    await _avisar_espera(
                        pool, f"aguardando: RAM livre {ram:.1f}GB < piso "
                              f"{RAM_MIN_LIVRE_GB:g}GB (protecao do banco)",
                        aviso_cache)
                    await asyncio.sleep(POLL_S)
                    continue
                pesados = await _pesados_no_ar(pool, vivos)
                if pesados >= PESADOS_MAX:
                    await _avisar_espera(
                        pool, f"aguardando: {pesados} processos pesados no ar "
                              f"(teto {PESADOS_MAX} entre varredura+esteira)",
                        aviso_cache)
                    await asyncio.sleep(POLL_S)
                    continue
                # nunca subir mais do que o teto global permite neste poll
                livres = min(livres, PESADOS_MAX - pesados)

            # 4) sobe ate encher os slots
            if livres > 0:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        # SKIP LOCKED: 2 daemons nunca pegam a mesma rodada
                        rows = await conn.fetch(
                            """SELECT id FROM esteira_jobs
                                WHERE status = 'pendente'
                                ORDER BY criado_em
                                LIMIT $1 FOR UPDATE SKIP LOCKED""", livres)
                        for r in rows:
                            await conn.execute(
                                """UPDATE esteira_jobs
                                      SET status = 'preparando',
                                          iniciado_em = NOW(),
                                          progresso_msg = 'reservada pela '
                                                          'fila; subindo'
                                    WHERE id = $1""", r["id"])
                for r in rows:
                    try:
                        vivos[r["id"]] = _subir(r["id"])
                        aviso_cache.pop(r["id"], None)
                        logger.info(f"[fila] rodada {r['id']} iniciada "
                                    f"({len(vivos)}/{SLOTS} slots)")
                    except Exception as e:
                        logger.exception(f"[fila] nao subi a rodada "
                                         f"{r['id']}")
                        async with pool.acquire() as conn:
                            await conn.execute(
                                """UPDATE esteira_jobs SET status='erro',
                                          erro=$2, finalizado_em=NOW()
                                    WHERE id=$1""",
                                r["id"],
                                f"falha ao iniciar o processo: {e}")
        except Exception:
            # o daemon NUNCA morre por causa de uma rodada ou hiccup do banco
            logger.exception("[fila] erro no laco (seguindo)")
        await asyncio.sleep(POLL_S)


def _teste():
    ok = [0]

    def check(cond, nome):
        if not cond:
            print(f"FALHOU: {nome}")
            sys.exit(1)
        ok[0] += 1
        print(f"ok {ok[0]}: {nome}")

    check(_pid_vivo(os.getpid()), "pid_vivo: enxerga o proprio processo")
    check(not _pid_vivo(999999999), "pid_vivo: pid inexistente = morto")
    check(not _pid_vivo(None), "pid_vivo: None = morto")
    ram = _ram_livre_gb()
    check(ram is None or (isinstance(ram, float) and 0 < ram < 4096),
          f"ram_livre: valor sao ({ram if ram is None else round(ram, 1)}GB)")
    fl = _flags_prioridade()
    check(("creationflags" in fl) if os.name == "nt" else
          ("preexec_fn" in fl), "prioridade baixa: flags do SO")
    print(f"\n=== SELF-TEST DAEMON: {ok[0]}/{ok[0]} PASSARAM ===")


def main():
    sys.path.insert(0, str(RAIZ))
    if "--teste" in sys.argv:
        _teste()
        return
    try:
        asyncio.run(_laco())
    except KeyboardInterrupt:
        logger.info("[fila] encerrando")


if __name__ == "__main__":
    main()
