# -*- coding: utf-8 -*-
r"""
workers/varredura_job.py — executa UM job de varredura de ponta a ponta.

O CICLO (cada etapa grava status no banco, entao da' pra acompanhar na tela):

  1. VALIDA a origem      job de backtest concluido, com apostas, do usuario
  2. MONTA o dado         apostas_detalhe -> planilha (mesmo formato do export)
  3. PLANO                roda --plano e grava o CONTRATO (grades, eixos, cego,
                          total estimado). Se a estimativa passar do teto e o
                          job nao estiver confirmado, PARA em 'planejado' e
                          espera o OK — evita disparar 3 dias de CPU sem querer
  4. VARRE                com pre-compromisso (--ate): a busca so ve o treino
  5. HOLDOUT              repontua as configs achadas nos dias que a busca NAO
                          viu, e mede quantas sobrevivem
  6. GATE                 validar_varredor (T1 liquidacao / T2 leitura). Se
                          falhar, o job vai pra 'erro' — numero de garimpo que
                          nao passa no carimbo nao deveria nem ser exibido
  7. SALVA                arquivos + resumo

POR QUE ELE NAO REFATORA O VARREDOR: chama `varredura.varrer()`, que so monta
o argv e executa o MESMO caminho da linha de comando ja validado no T1/T2.
A versao do painel nunca diverge da que foi carimbada.
"""
import asyncio
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("varredura_job")

# ---------------------------------------------------------------- config ----
# raiz do projeto (este arquivo mora em workers/)
RAIZ = Path(__file__).resolve().parent.parent
# onde ficam varredura.py / repontua.py / validar_varredor.py. Se voce guarda
# em outra pasta, aponte aqui (ou defina VARREDURA_DIR no ambiente).
FERRAMENTAS = Path(os.environ.get("VARREDURA_DIR", str(RAIZ / "mineracao")))
SAIDA_DIR = Path(os.environ.get("VARREDURA_OUT", str(RAIZ / "varreduras")))

# acima disto o job PARA e espera confirmacao explicita (protege contra rodada
# de dias disparada sem querer). O --plano ja imprime esse total.
TETO_CONFIGS_SEM_CONFIRMAR = 400_000_000
# T1 (liquidacao) e' FATAL: se o export nao fecha, nada ali presta.
# T2 (leitura) e' percentual — 100% so acontece com event_id no export; sem
# ele o teto da escadinha troca 1-2 apostas de degrau em algumas configs.
# Abaixo deste piso o job reprova; acima, passa com o numero registrado.
GATE_T2_MIN = 90.0
# fracao do periodo que fica de fora da busca (holdout de verdade)
FRACAO_HOLDOUT = 0.30
MIN_DIAS_HOLDOUT = 3


class VarreduraErro(Exception):
    """Erro de negocio (dado ruim, origem invalida). Vira mensagem pro usuario;
    nao e' bug."""


# ------------------------------------------------------------- utilidades ---
def _achar_ferramenta(nome):
    """Procura o script em VARREDURA_DIR, na raiz e em workers/."""
    for pasta in (FERRAMENTAS, RAIZ, RAIZ / "workers", Path.cwd()):
        alvo = pasta / nome
        if alvo.is_file():
            return alvo
    raise VarreduraErro(
        f"nao achei {nome}. Coloque varredura.py, repontua.py e "
        f"validar_varredor.py em {FERRAMENTAS} (ou defina VARREDURA_DIR).")


def _carregar_modulo(caminho, apelido):
    import importlib.util
    spec = importlib.util.spec_from_file_location(apelido, str(caminho))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rodar_cli(caminho, args):
    """Roda um script de CLI (repontua/validar) no processo, capturando a
    saida. Devolve (codigo, texto). Nunca levanta: erro vira (1, traceback)."""
    import contextlib
    import io as _io
    import traceback
    buf = _io.StringIO()
    argv_antigo = sys.argv
    sys.argv = [caminho.name] + [str(x) for x in args]
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            mod = _carregar_modulo(caminho, f"_cli_{caminho.stem}")
            try:
                mod.main()
                cod = 0
            except SystemExit as e:
                cod = int(e.code or 0)
        return cod, buf.getvalue()
    except Exception:
        return 1, buf.getvalue() + "\n" + traceback.format_exc()
    finally:
        sys.argv = argv_antigo


def _ler_gate(texto):
    """Le a linha `GATE t1=OK t2=98.5` do validador. Sem ela (versao antiga do
    script), devolve (None, 100) pra nao reprovar por falta de informacao."""
    t1, pct = None, 100.0
    for l in texto.split("\n"):
        s = l.strip()
        if "GATE " in s and "t1=" in s:
            for parte in s.split():
                if parte.startswith("t1="):
                    t1 = parte[3:]
                elif parte.startswith("t2="):
                    try:
                        pct = float(parte[3:])
                    except ValueError:
                        pass
    return t1, pct


def _num(txt):
    try:
        return int(str(txt).replace(",", "").replace(".", "").strip())
    except Exception:
        return None


def _ler_contrato(texto):
    """Extrai do --plano o que importa pra decisao e pro registro."""
    c = {"texto": texto[-8000:], "total_estimado": None, "janelas": None,
         "complementares": None, "cego": None, "baseline": None,
         "apostas": None, "jogos": None, "dias": None,
         "pisos_linha": None, "tetos_linha": None}
    for l in texto.split("\n"):
        s = l.strip()
        if s.startswith("TOTAL") and " ate " in s:
            c["total_estimado"] = _num(s.split(" ate ")[-1])
        elif s.startswith("janelas em uso"):
            # prioridade sobre "janelas de WR": esta e' a lista efetiva quando
            # o --janelas foi usado. startswith (nao `in`) porque o rodape do
            # --plano tambem cita a frase e ja roubou o campo uma vez.
            c["janelas"] = s.split("...")[-1].strip(" .")
        elif s.startswith("janelas de WR") and not c.get("janelas"):
            c["janelas"] = s.split("...")[-1].strip(" .")
        elif s.startswith("complementares"):
            c["complementares"] = s.split("...")[-1].strip(" .")
        elif s.startswith("teste cego"):
            c["cego"] = s.split("...")[-1].strip(" .")
        elif s.startswith("baseline"):
            c["baseline"] = s.split("...")[-1].strip(" .")
        elif s.startswith("pisos (>=X)"):
            c["pisos_linha"] = s
        elif s.startswith("tetos (<=X)"):
            c["tetos_linha"] = s
        elif " apostas | " in s and " jogos | " in s:
            partes = [p.strip() for p in s.split("|")]
            c["apostas"] = _num(partes[0].split()[0])
            c["jogos"] = _num(partes[1].split()[0])
            c["dias"] = _num(partes[2].split()[0])
    return c


def _args_do_params(p, entrada, saida, ate=None, plano=False):
    """params (json do job) -> lista de flags do varredor. Whitelist: so o que
    esta aqui vira flag, entao params torto nao vira injecao de linha de
    comando."""
    a = ["--xlsx", str(entrada), "--out", str(saida),
         "--modo", str(p.get("modo") or "completo")]
    if p.get("janelas"):
        a += ["--janelas", str(p["janelas"])]
    if p.get("min_apostas"):
        a += ["--min-apostas", str(int(p["min_apostas"]))]
    if p.get("guardar"):
        a += ["--guardar", str(int(p["guardar"]))]
    if p.get("nlmax"):
        a += ["--nlmax", str(int(p["nlmax"]))]
    if p.get("nlin"):
        a += ["--nlin", str(int(p["nlin"]))]
    if p.get("placebo"):
        a += ["--placebo", str(int(p["placebo"]))]
    if p.get("sem_odd"):
        a += ["--sem-odd"]
    if ate:
        a += ["--ate", str(ate)]
    if plano:
        a += ["--plano"]
    return a


# ------------------------------------------------------------------ banco ---
_COLS_JSONB = {"contrato", "resumo", "params"}


async def _set(conn, job_id, **campos):
    """UPDATE parcial. Coluna jsonb PRECISA de cast explicito — o asyncpg manda
    str como text e o Postgres nao converte sozinho."""
    if not campos:
        return
    cols = ", ".join(
        f"{k} = ${i + 2}" + ("::jsonb" if k in _COLS_JSONB else "")
        for i, k in enumerate(campos))
    await conn.execute(f"UPDATE varredura_jobs SET {cols} WHERE id = $1",
                       job_id, *campos.values())


# ------------------------------------------------------------------ ciclo ---
async def executar_varredura(job_id: int):
    from database import get_pool
    pool = get_pool()
    t_ini = time.time()

    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            "SELECT * FROM varredura_jobs WHERE id = $1", job_id)
        if job is None:
            raise VarreduraErro(f"job {job_id} nao existe")
        # 'planejando' entra na lista: e' o status que o DAEMON grava ao
        # reservar o slot, antes de subir este processo. Sem ele aqui, o worker
        # desistia calado e o job ficava parado pra sempre — o job 1 so' passou
        # porque foi rodado na mao, com status 'pendente'.
        if job["status"] not in ("pendente", "planejado", "planejando"):
            logger.warning(f"[varredura] job {job_id} em '{job['status']}' — "
                           "nao vou rodar de novo")
            return
        params = job["params"]
        if isinstance(params, str):
            params = json.loads(params or "{}")
        params = params or {}
        # 'confirmado' vem dos PARAMS (o endpoint /confirmar grava la). Nao
        # deduzir do status: ele muda pra 'pendente'/'planejando' no caminho
        # e a confirmacao se perderia — o job voltaria pra 'planejado' pra
        # sempre. O status 'planejado' aqui e' so' um segundo caminho valido
        # (execucao manual do run_varredura sobre um job ja planejado).
        confirmado = bool(params.get("confirmado")) or job["status"] == "planejado"

        await _set(conn, job_id, status="planejando", iniciado_em=datetime.now(),
                   pid=os.getpid(), erro=None, progresso=0,
                   progresso_msg="carregando as apostas do backtest de origem")

        # ---- 1) origem ---------------------------------------------------
        org = await conn.fetchrow(
            """SELECT id, status, apostas_detalhe, bot_snapshot, user_id,
                      total_apostas
                 FROM backtest_jobs WHERE id = $1""", job["job_backtest_id"])

    if org is None:
        raise VarreduraErro(f"backtest {job['job_backtest_id']} nao existe")
    if org["status"] != "concluido":
        raise VarreduraErro(
            f"o backtest {org['id']} esta '{org['status']}' — so da' pra "
            "garimpar em cima de job concluido")

    detalhe = org["apostas_detalhe"]
    if isinstance(detalhe, str):
        detalhe = json.loads(detalhe or "[]")
    detalhe = detalhe or []
    if len(detalhe) < 500:
        raise VarreduraErro(
            f"o backtest {org['id']} tem so {len(detalhe)} apostas — pouco pra "
            "garimpar (o teto de sorte come qualquer achado nesse tamanho)")

    # ---- 2) planilha ------------------------------------------------------
    sys.path.insert(0, str(RAIZ))
    try:
        from workers.apostas_export import df_apostas, COLUNAS_MINIMAS
    except ImportError:
        from apostas_export import df_apostas, COLUNAS_MINIMAS
    df = df_apostas(detalhe)
    if df.empty:
        raise VarreduraErro("nao consegui montar nenhuma linha do apostas_detalhe")
    faltando = [c for c in COLUNAS_MINIMAS if c not in df.columns]
    if faltando:
        raise VarreduraErro(
            f"o export do job {org['id']} nao tem as colunas {faltando} — "
            "rode o backtest de origem com os chips anotando")

    # AVISO IMPORTANTE (nao bloqueia, mas fica registrado): se o job de origem
    # ja tinha filtro, o garimpo so procura DENTRO da estrategia dele e nunca
    # fora. A fonte certa e' o ESCANCARADO (sem filtro, chips 0-100).
    snap = org["bot_snapshot"]
    if isinstance(snap, str):
        try:
            snap = json.loads(snap or "{}")
        except Exception:
            snap = {}
    filtros_origem = (snap or {}).get("filtros") or {}
    origem_filtrada = bool(
        filtros_origem.get("filtrosHistAdicionados")
        or filtros_origem.get("folgaAtivo")
        or (snap or {}).get("linha_min") or (snap or {}).get("linha_max")
        or (snap or {}).get("max_apostas_partida"))

    SAIDA_DIR.mkdir(parents=True, exist_ok=True)
    base = SAIDA_DIR / f"varredura_{job_id}"
    # CSV, nao XLSX: este arquivo e' lido 3-4x depois (varredura, repontua,
    # validador) e o openpyxl custa ~18s POR LEITURA num export de 42k linhas,
    # contra 0,4s do csv. So a troca de formato tira ~1min30 da rodada.
    # O varredor/repontua/validador detectam a extensao e leem csv igual.
    entrada = base.with_name(base.name + "_entrada.csv")
    saida = base.with_name(base.name + ".xlsx")
    df.to_csv(entrada, index=False, encoding="utf-8")

    # ---- 3) holdout: data de corte ---------------------------------------
    import pandas as pd
    ts = pd.to_datetime(df["Data"].astype(str) + " " + df["Hora"].astype(str),
                        dayfirst=True, errors="coerce")
    d0, d1 = ts.min(), ts.max()
    if pd.isna(d0) or pd.isna(d1):
        raise VarreduraErro("nao consegui ler as datas do export")
    total_dias = (d1 - d0).days + 1
    corte_cfg = job["data_corte"]
    if corte_cfg:
        corte = pd.Timestamp(corte_cfg)
    else:
        corte = d1 - timedelta(days=max(MIN_DIAS_HOLDOUT,
                                        round(total_dias * FRACAO_HOLDOUT)))
    usar_holdout = total_dias >= (MIN_DIAS_HOLDOUT * 2) and d0 < corte < d1
    ate = (corte - timedelta(days=1)).date() if usar_holdout else None
    de_holdout = corte.date() if usar_holdout else None

    varredura_py = _achar_ferramenta("varredura.py")
    mod_var = _carregar_modulo(varredura_py, "_varredura_mod")

    # ---- 4) PLANO ---------------------------------------------------------
    import contextlib
    import io as _io
    # O --plano recarrega e reprepara o dado inteiro so pra estimar. Isso so
    # vale a pena quando a estimativa pode PARAR o job (modo total sem
    # confirmacao). Nos outros casos o contrato sai do cabecalho da propria
    # rodada, que imprime exatamente as mesmas linhas — e economiza uma
    # preparacao completa.
    precisa_plano_antes = (str(params.get("modo") or "").lower() == "total"
                           and not confirmado)
    contrato = {}
    if precisa_plano_antes:
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod_var.varrer(_args_do_params(params, entrada, saida, ate, plano=True))
        contrato = _ler_contrato(buf.getvalue())
    _extra_contrato = {
        "origem_job": org["id"], "origem_filtrada": origem_filtrada,
        "periodo": f"{d0:%d/%m/%Y} a {d1:%d/%m/%Y}", "dias_total": total_dias,
        "holdout": (f"{de_holdout:%d/%m/%Y} em diante" if usar_holdout
                    else "SEM holdout (periodo curto demais)"),
        "treino_ate": str(ate) if ate else None,
    }
    if contrato:
        contrato.update(_extra_contrato)

    async with pool.acquire() as conn:
        if contrato:
            await _set(conn, job_id, contrato=json.dumps(contrato, default=str))
        est = contrato.get("total_estimado") or 0
        if est > TETO_CONFIGS_SEM_CONFIRMAR and not confirmado:
            await _set(conn, job_id, status="planejado", progresso=0,
                       progresso_msg=(
                           f"{est:,} configuracoes estimadas — confirme para "
                           f"rodar (pode levar horas)").replace(",", "."))
            logger.info(f"[varredura] job {job_id} aguardando confirmacao "
                        f"({est:,} configs)")
            return
        await _set(conn, job_id, status="rodando", progresso=1,
                   progresso_msg="varrendo...")

    # ---- 5) VARRE (CPU) ---------------------------------------------------
    # o miolo e' sincrono e pesado -> vai pra thread, e o progresso volta pro
    # loop por run_coroutine_threadsafe. Se o UPDATE falhar, a varredura NAO
    # para por causa disso (o callback do varredor ja engole excecao).
    loop = asyncio.get_running_loop()
    ultimo = [0.0]

    def _progresso(d):
        agora = time.time()
        if agora - ultimo[0] < 3:          # no maximo 1 UPDATE a cada 3s
            return
        ultimo[0] = agora
        pct = max(1, min(95, int(d.get("pct") or 1)))
        msg = (f"{d.get('testadas', 0):,} testadas · "
               f"{d.get('guardadas', 0):,} guardadas · "
               f"ETA {d.get('eta_min', 0)}min").replace(",", ".")

        async def _upd():
            try:
                async with pool.acquire() as c2:
                    await _set(c2, job_id, progresso=pct, progresso_msg=msg)
            except Exception:
                pass
        asyncio.run_coroutine_threadsafe(_upd(), loop)

    def _rodar():
        b = _io.StringIO()
        with contextlib.redirect_stdout(b):
            rc = mod_var.varrer(
                _args_do_params(params, entrada, saida, ate), on_progress=_progresso)
        return rc, b.getvalue()

    rc, log_varredura = await asyncio.to_thread(_rodar)
    if not contrato:
        # o cabecalho da rodada traz as mesmas linhas do --plano
        contrato = _ler_contrato(log_varredura)
        contrato.update(_extra_contrato)
        async with pool.acquire() as conn:
            await _set(conn, job_id, contrato=json.dumps(contrato, default=str))
    if rc != 0 or not saida.is_file():
        raise VarreduraErro(
            f"a varredura terminou com codigo {rc} e sem arquivo de saida. "
            f"Fim do log: ...{log_varredura[-600:]}")

    tudo_csv = saida.with_suffix("").as_posix() + ".tudo.csv"
    resumo = {"segundos": round(time.time() - t_ini),
              "linhas_saida": None, "holdout": None, "gate": None}
    try:
        resumo["linhas_saida"] = sum(1 for _ in open(tudo_csv, encoding="utf-8")) - 1
    except Exception:
        pass

    # ---- 6) HOLDOUT -------------------------------------------------------
    arq_hold = None
    if usar_holdout and os.path.isfile(tudo_csv):
        async with pool.acquire() as conn:
            await _set(conn, job_id, progresso=96,
                       progresso_msg="medindo no holdout (dias que a busca nao viu)")
        try:
            rep_py = _achar_ferramenta("repontua.py")
            arq_hold = str(base.with_name(base.name + "_holdout.xlsx"))
            cod, txt = _rodar_cli(rep_py, [
                "--garimpo", tudo_csv, "--apostas", str(entrada),
                "--de", str(de_holdout), "--out", arq_hold])
            resumo["holdout"] = {"codigo": cod, "log": txt[-1500:],
                                 "de": str(de_holdout)}
            if cod != 0:
                arq_hold = None
        except VarreduraErro as e:
            resumo["holdout"] = {"erro": str(e)}
        except Exception as e:
            resumo["holdout"] = {"erro": f"{type(e).__name__}: {e}"}

    # ---- 7) GATE ----------------------------------------------------------
    async with pool.acquire() as conn:
        await _set(conn, job_id, progresso=98, progresso_msg="carimbando (T1/T2)")
    gate_ok = None
    try:
        val_py = _achar_ferramenta("validar_varredor.py")
        _args_gate = ["--export", str(entrada), "--garimpo", tudo_csv,
                      "--amostra", "400"]
        # CRITICO: a busca rodou com --ate (so viu o treino). Conferir contra o
        # export inteiro compara janelas diferentes e o T2 reprova por
        # construcao — as contagens batem exatamente na fracao do treino.
        if ate:
            _args_gate += ["--ate", str(ate)]
        cod, txt = _rodar_cli(val_py, _args_gate)
        t1, t2_pct = _ler_gate(txt)
        gate_ok = (t1 != "FALHOU") and (t2_pct >= GATE_T2_MIN)
        resumo["gate"] = {"passou": gate_ok, "t1": t1, "t2_pct": t2_pct,
                          "log": txt[-2500:]}
    except VarreduraErro as e:
        resumo["gate"] = {"passou": None, "aviso": str(e)}
    except Exception as e:
        resumo["gate"] = {"passou": None, "aviso": f"{type(e).__name__}: {e}"}

    # ---- 8) fecha ---------------------------------------------------------
    async with pool.acquire() as conn:
        if gate_ok is False:
            _g = resumo.get("gate") or {}
            if _g.get("t1") == "FALHOU":
                _msg = ("o EXPORT nao fecha: o T1 recalculou green/red pelo "
                        "placar final e achou divergencia. O problema esta no "
                        "backtest de origem, nao no garimpo.")
            else:
                _msg = (f"o T2 (leitura) bateu em so {_g.get('t2_pct', 0):.1f}% "
                        f"das configs conferidas (minimo {GATE_T2_MIN}%). O "
                        "garimpo pode estar filtrando diferente do motor — veja "
                        "o log do gate no resumo.")
            await _set(
                conn, job_id, status="erro", progresso=100, erro=_msg,
                arquivo_saida=str(saida), arquivo_tudo=tudo_csv,
                arquivo_holdout=arq_hold,
                resumo=json.dumps(resumo, default=str),
                concluido_em=datetime.now())
            logger.error(f"[varredura] job {job_id} REPROVADO no gate")
            return
        await _set(conn, job_id, status="concluido", progresso=100,
                   progresso_msg="pronto",
                   arquivo_saida=str(saida), arquivo_tudo=tudo_csv,
                   arquivo_holdout=arq_hold,
                   resumo=json.dumps(resumo, default=str),
                   concluido_em=datetime.now())
    logger.info(f"[varredura] job {job_id} concluido em "
                f"{resumo['segundos']}s ({resumo.get('linhas_saida')} configs)")
