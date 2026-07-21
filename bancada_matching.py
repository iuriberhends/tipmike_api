# -*- coding: utf-8 -*-
r"""
bancada_matching.py v1.2 — ETAPA 2 do plano de validação (bancada do matching)

Testa as funções REAIS do motor (workers/backtest_runner.py — as MESMAS que o
bot_executor importa) contra os casos-pegadinha levantados no censo da etapa 1:

  _matches_mercado(mercado_bot, tick_mercado, tick_mercado_tipo, casa)
  _selecao_hc_valor(selecao)   — extração do valor do HC (superbet/estrelabet/betano)
  _lado_aposta(selecao)        — over/under da seleção
  _parse_linha(linha)          — parsing da coluna linha (informativo)

Dois modos:
  --seco   roda SÓ com ticks embutidos (nomes/tipos/seleções reais copiados do
           censo_detalhe) — não precisa de banco. Serve pra validar o motor em
           qualquer máquina que tenha o projeto.
  (padrão) modo BANCO: além dos embutidos, amostra até --n ticks reais por caso
           direto da tabela `ticks` (últimos --dias dias) e roda o mesmo teste.

Onde rodar: na RAIZ do projeto tipmike_api (mesma pasta do main.py), na VPS:
    python bancada_matching.py --seco          (primeiro, sem banco)
    python bancada_matching.py                 (depois, com ticks reais)

Saídas na pasta do script: bancada_log.txt (zerado a cada execução) e
bancada_resultado.csv (1 linha por caso). Exit code 0 somente com 100% de
acerto nos casos com dado; SEM_DADO não reprova (retenção do banco é ~3 dias).
"""

import argparse
import csv
import os
import sys
import traceback
from datetime import datetime

PASTA_SCRIPT = os.path.dirname(os.path.abspath(__file__))
DIAS_DEFAULT = 3
N_DEFAULT = 40
DSN_DEFAULT = os.environ.get(
    "MIKEDB_DSN", "postgresql://postgres:mikedb0702@localhost:5432/mikedb"
)

# ----------------------------------------------------------------------------
# Import do MOTOR REAL (mesmo código do bot vivo e do backtest)
# ----------------------------------------------------------------------------
sys.path.insert(0, PASTA_SCRIPT)
try:
    from workers.backtest_runner import (  # type: ignore
        _matches_mercado,
        _periodo_do_mercado,
        _selecao_hc_valor,
        _lado_aposta,
        _parse_linha,
    )
except Exception as e:
    print("ERRO: nao consegui importar workers.backtest_runner — rode este script")
    print("na RAIZ do projeto tipmike_api (mesma pasta do main.py).")
    print(f"Detalhe: {e}")
    sys.exit(2)


# ----------------------------------------------------------------------------
# Log simples (arquivo zerado a cada execução + console)
# ----------------------------------------------------------------------------
class Log:
    def __init__(self, caminho):
        try:
            self._fh = open(caminho, "w", encoding="utf-8")
        except OSError as e:
            self._fh = None
            print(f"[AVISO] sem log em arquivo ({e})")

    def msg(self, texto=""):
        linha = f"[{datetime.now().strftime('%H:%M:%S')}] {texto}" if texto else ""
        print(linha if texto else "")
        if self._fh:
            try:
                self._fh.write((linha if texto else "") + "\n")
                self._fh.flush()
            except OSError:
                pass

    def fechar(self):
        if self._fh:
            try:
                self._fh.close()
            except OSError:
                pass


# ----------------------------------------------------------------------------
# CASOS DE TESTE — gabarito construído dos ticks reais do censo (etapa 1)
# Cada sintético: (mercado, mercado_tipo, selecao, linha)
# ----------------------------------------------------------------------------
CASOS = [
    # ---------------- SUPERBET / E-Basketball ----------------
    dict(id="S1", casa="superbet", bot="ah_ft", esperado=True, hc=True,
         desc="eBasket: HC jogo inteiro '(Inc. prorrogação)' CASA no ah_ft",
         sql="sport='E-Basketball' AND mercado_tipo='HANDICAP' AND mercado ILIKE '%prorrog%'",
         sinteticos=[("Handicap (Inc. prorrogação)", "HANDICAP", "Boston Celtics (Berlin) (-13.5)", "-13.5"),
                     ("Handicap (Inc. prorrogação)", "HANDICAP", "Anadolu Efes (Legolas792) (-10.5)", "-10.5")]),
    dict(id="S2", casa="superbet", bot="ah_ft", esperado=False,
         desc="eBasket: HC de 1º Tempo NAO casa no ah_ft",
         sql="sport='E-Basketball' AND mercado_tipo='HANDICAP' AND mercado ILIKE '%tempo%'",
         sinteticos=[("1º Tempo - Handicap", "HANDICAP", "Boston Celtics (Berlin) (-4.5)", "-4.5")]),
    dict(id="S3", casa="superbet", bot="ah_ft", esperado=False,
         desc="eBasket: HC de Quarto (linha com sufixo -1..-4) NAO casa no ah_ft",
         sql="sport='E-Basketball' AND mercado_tipo='HANDICAP' AND mercado ILIKE '%quarto%'",
         sinteticos=[("Quarto 2 - Handicap", "HANDICAP", "Boston Celtics (Berlin) (-1.5)", "-1.5-2"),
                     ("Quarto 4 - Handicap", "HANDICAP", "Boston Celtics (Berlin) (-2.5)", "-2.5-4")]),
    dict(id="S4", casa="superbet", bot="over_under_ft", esperado=True, ou=True,
         desc="eBasket: OU jogo inteiro '(Inc. prorrogação)' CASA no over_under_ft",
         sql="sport='E-Basketball' AND mercado_tipo='OVER_UNDER' AND mercado ILIKE '%prorrog%'",
         sinteticos=[("Total de Pontos (Inc. prorrogação)", "OVER_UNDER", "Mais de 105.5", "105.5"),
                     ("Total de Pontos (Inc. prorrogação)", "OVER_UNDER", "Menos de 101.5", "101.5")]),
    dict(id="S5", casa="superbet", bot="over_under_ft", esperado=False,
         desc="eBasket: PERIOD_TOTAL 1º Tempo NAO casa no over_under_ft",
         sql="sport='E-Basketball' AND mercado_tipo='PERIOD_TOTAL' AND mercado ILIKE '%tempo%'",
         sinteticos=[("1º Tempo - Total de Pontos", "PERIOD_TOTAL", "Mais de 38.5", "38.5")]),
    dict(id="S6", casa="superbet", bot="over_under_ht", esperado=True, ou=True,
         desc="eBasket: PERIOD_TOTAL 1º Tempo CASA no over_under_ht",
         sql="sport='E-Basketball' AND mercado_tipo='PERIOD_TOTAL' AND mercado ILIKE '1%tempo%total%'",
         sinteticos=[("1º Tempo - Total de Pontos", "PERIOD_TOTAL", "Mais de 38.5", "38.5")]),
    dict(id="S7", casa="superbet", bot="over_under_ht", esperado=False,
         desc="eBasket: PERIOD_TOTAL de Quarto (linha com prefixo n-) NAO casa no over_under_ht",
         sql="sport='E-Basketball' AND mercado_tipo='PERIOD_TOTAL' AND mercado ILIKE '%quarto%'",
         sinteticos=[("1º Quarto - Total de Pontos", "PERIOD_TOTAL", "Mais de 20.5", "1-20.5"),
                     ("3º Quarto - Total de Pontos", "PERIOD_TOTAL", "Mais de 15.5", "3-15.5")]),
    dict(id="S8", casa="superbet", bot="over_under_ft", esperado=False,
         desc="eBasket: PLAYER_TOTAL (total por jogador) NAO casa no over_under_ft",
         sql="sport='E-Basketball' AND mercado_tipo='PLAYER_TOTAL'",
         sinteticos=[("Boston Celtics (Berlin) - Total de Pontos (Inc. prorrogação)", "PLAYER_TOTAL", "Mais de 48.5", "48.5")]),
    # ---------------- SUPERBET / E-Football ----------------
    dict(id="S9", casa="superbet", bot="ah_ft", esperado=True, hc=True,
         desc="e-foot: 'Handicap Asiático' (jogo inteiro) CASA no ah_ft",
         sql="sport='E-Football' AND mercado_tipo='HANDICAP' AND mercado ILIKE 'handicap asi%'",
         sinteticos=[("Handicap Asiático", "HANDICAP", "Bayern de Munique (KINGSLAYER) (-1.5)", "-1.5")]),
    dict(id="S9b", casa="superbet", bot="ah_ft", esperado=False,
         desc="e-foot: '1º Tempo - Handicap asiático' (HT!) NAO casa no ah_ft — descoberta da rodada 20/jul",
         sql="sport='E-Football' AND mercado_tipo='HANDICAP' AND mercado ILIKE '1%tempo%asi%'",
         sinteticos=[("1º Tempo - Handicap asiático", "HANDICAP", "Bayern de Munique (KINGSLAYER) (-0.5)", "-0.5")]),
    dict(id="S10", casa="superbet", bot="ah_ft", esperado=False,
         desc="e-foot: 'Handicap 3-Way' (europeu, empate PERDE) NAO deveria casar no ah_ft",
         sql="sport='E-Football' AND mercado_tipo='HANDICAP' AND mercado ILIKE 'handicap 3%'",
         sinteticos=[("Handicap 3-Way", "HANDICAP", "1 (-1.5)", "-1.5")]),
    dict(id="S11", casa="superbet", bot="ah_ft", esperado=True, hc=True,
         desc="e-foot: 'Handicap 2-way' (linha meia, sem empate) casa no ah_ft",
         sql="sport='E-Football' AND mercado_tipo='HANDICAP' AND mercado ILIKE 'handicap 2%'",
         sinteticos=[("Handicap 2-way", "HANDICAP", "Barcelona (KINGSLAYER) (-1.5)", "-1.5")]),
    dict(id="S12", casa="superbet", bot="over_under_ft", esperado=True, ou=True,
         desc="e-foot: 'Total de Gols' CASA no over_under_ft",
         sql="sport='E-Football' AND mercado_tipo='OVER_UNDER' AND mercado = 'Total de Gols'",
         sinteticos=[("Total de Gols", "OVER_UNDER", "Mais de 4.5", "4.5")]),
    dict(id="S13", casa="superbet", bot="over_under_ft", esperado=False,
         desc="e-foot: 'Total de Gols Asiático' NAO deveria casar no over_under_ft (tem mercado_bot próprio)",
         sql="sport='E-Football' AND mercado_tipo='OVER_UNDER' AND mercado ILIKE 'total de gols asi%'",
         sinteticos=[("Total de Gols Asiático", "OVER_UNDER", "Mais de 4.75", "4.75")]),
    # ---------------- ESTRELABET ----------------
    dict(id="E1", casa="estrelabet", bot="ah_ft", esperado=True, hc=True,
         desc="e-foot: tipo 16 'Handicap' CASA no ah_ft",
         sql="mercado_tipo='16'",
         sinteticos=[("Handicap", "16", "Germany (ROONEY) (-1.5)", "-1.5")]),
    dict(id="E2", casa="estrelabet", bot="ah_ft", esperado=False,
         desc="e-foot: tipo 66 (1º tempo - handicap) NAO casa no ah_ft",
         sql="mercado_tipo='66'",
         sinteticos=[("1º tempo - handicap", "66", "Germany (ROONEY) (-0.5)", "-0.5")]),
    dict(id="E2b", casa="estrelabet", bot="ah_ht", esperado=True, hc=True,
         desc="e-foot: tipo 66 CASA no ah_ht",
         sql="mercado_tipo='66'",
         sinteticos=[("1º tempo - handicap", "66", "Germany (ROONEY) (-0.5)", "-0.5")]),
    dict(id="E3", casa="estrelabet", bot="ah_ft", esperado=False,
         desc="eBasket: tipo 303 (handicap por quarto) NAO casa no ah_ft",
         sql="mercado_tipo='303'",
         sinteticos=[("2º quarto - handicap", "303", "Denver Nuggets (Polub) (-1.5)", "-1.5")]),
    dict(id="E4", casa="estrelabet", bot="over_under_ft", esperado=True, ou=True,
         desc="e-foot: tipo 18 'Total de Gols' CASA no over_under_ft",
         sql="mercado_tipo='18'",
         sinteticos=[("Total de Gols", "18", "Mais de 4.5", "4.5")]),
    dict(id="E5", casa="estrelabet", bot="over_under_ft", esperado=False,
         desc="e-foot: tipo 68 (1º tempo - total) NAO casa no over_under_ft",
         sql="mercado_tipo='68'",
         sinteticos=[("1ª tempo - Total de gols", "68", "Mais de 2.5", "2.5")]),
    dict(id="E5b", casa="estrelabet", bot="over_under_ht", esperado=True, ou=True,
         desc="e-foot: tipo 68 CASA no over_under_ht",
         sql="mercado_tipo='68'",
         sinteticos=[("1ª tempo - Total de gols", "68", "Mais de 2.5", "2.5")]),
    dict(id="E6", casa="estrelabet", bot="over_under_ft", esperado=False,
         desc="eBasket: tipo 236 (total por quarto) NAO casa no over_under_ft",
         sql="mercado_tipo='236'",
         sinteticos=[("Terceiro quarto - total", "236", "Mais de 15.5", "15.5")]),
    dict(id="E7", casa="estrelabet", bot="over_under_ft", esperado=False,
         desc="eBasket: tipos 227/228 (PLAYER total incl. prorrogação) NAO casam no over_under_ft",
         sql="mercado_tipo IN ('227','228')",
         sinteticos=[("Denver Nuggets (Polub) Pontos Totais (incluindo Prorrogação)", "227", "Mais de 47.5", "47.5")]),
    dict(id="E8", casa="estrelabet", bot="over_under_ft", esperado=False,
         desc="e-foot: tipo 8 (Próximo gol N) NAO casa no over_under_ft",
         sql="mercado_tipo='8'",
         sinteticos=[("Segundo gol", "8", "Mais de 1.5", "1.5")]),
    # ---------------- BETANO ----------------
    dict(id="B1", casa="betano", bot="ah_ft", esperado=True, hc=True,
         desc="eBasket: tipo 156 'Handicap' CASA no ah_ft (seleção betano SEM parênteses)",
         sql="mercado_tipo='156'",
         sinteticos=[("Handicap", "156", "Partizan (tapachan) -15.5", "-15.5")]),
    dict(id="B2", casa="betano", bot="over_under_ft", esperado=True, ou=True,
         desc="eBasket: tipo 157 'Total de pontos' CASA no over_under_ft",
         sql="mercado_tipo='157'",
         sinteticos=[("Total de pontos", "157", "Mais de 155.5", "155.5")]),
    dict(id="B3", casa="betano", bot="over_under_ft", esperado=True, ou=True,
         desc="e-foot: tipo 13 'Total de Gols' CASA no over_under_ft",
         sql="mercado_tipo='13'",
         sinteticos=[("Total de Gols", "13", "Mais de 4.5", "4.5")]),
    dict(id="B4", casa="betano", bot="over_under_ft", esperado=False,
         desc="e-foot: tipo 14 (Total 1º Tempo) NAO casa no over_under_ft",
         sql="mercado_tipo='14'",
         sinteticos=[("Total de gols - 1° Tempo", "14", "Mais de 2.5", "2.5")]),
    dict(id="B4b", casa="betano", bot="over_under_ht", esperado=True, ou=True,
         desc="e-foot: tipo 14 CASA no over_under_ht",
         sql="mercado_tipo='14'",
         sinteticos=[("Total de gols - 1° Tempo", "14", "Mais de 2.5", "2.5")]),
    dict(id="B5", casa="betano", bot="over_under_ft", esperado=False,
         desc="e-foot: tipo 189 (Total Asiático — mercado_bot próprio) NAO casa no over_under_ft",
         sql="mercado_tipo='189'",
         sinteticos=[("Asiático (Mais/Menos) Total de Gols", "189", "Mais de 4.75", "4.75")]),
    dict(id="B7", casa="betano", bot="over_under_ft", esperado=False,
         desc="e-foot: tipo 11 (Próximo gol) NAO casa no over_under_ft",
         sql="mercado_tipo='11'",
         sinteticos=[("Próximo gol (Gol 2)", "11", "Equipe 1", "")]),
]


def testar_ticks(caso, ticks, log):
    """Roda o motor real nos ticks; devolve (ok, fail, exemplos_fail, notas_parsing)."""
    ok = fail = 0
    exemplos = []
    hc_none = ou_none = linha_none = 0
    casados = 0
    for (mercado, mtipo, selecao, linha) in ticks:
        try:
            casou = _matches_mercado(caso["bot"], mercado, mtipo, caso["casa"])
        except Exception as e:
            casou = f"EXCECAO:{e}"
        certo = (casou is caso["esperado"]) if isinstance(casou, bool) else False
        if certo:
            ok += 1
        else:
            fail += 1
            if len(exemplos) < 5:
                exemplos.append(f"mercado='{mercado}' tipo='{mtipo}' -> {casou} (esperado {caso['esperado']})")
        # sub-testes de parsing só nos ticks que DEVEM casar
        if caso["esperado"] and isinstance(casou, bool) and casou:
            casados += 1
            if caso.get("hc") and _selecao_hc_valor(selecao) is None:
                hc_none += 1
                if len(exemplos) < 5:
                    exemplos.append(f"_selecao_hc_valor('{selecao}') = None")
            if caso.get("ou") and _lado_aposta(selecao) not in ("over", "under"):
                ou_none += 1
                if len(exemplos) < 5:
                    exemplos.append(f"_lado_aposta('{selecao}') = None")
            if _parse_linha(linha) is None and str(linha or "").strip() != "":
                linha_none += 1
    parsing_fail = hc_none + ou_none  # linha_none é só informativo
    notas = []
    if casados:
        if caso.get("hc"):
            notas.append(f"hc_valor None: {hc_none}/{casados}")
        if caso.get("ou"):
            notas.append(f"lado None: {ou_none}/{casados}")
    if linha_none:
        notas.append(f"parse_linha None: {linha_none} (informativo)")
    return ok, fail, parsing_fail, exemplos, "; ".join(notas)


def buscar_banco(caso, args, log):
    try:
        import psycopg2  # type: ignore
    except ImportError:
        log.msg("[AVISO] psycopg2 ausente — modo banco indisponível (pip install psycopg2-binary)")
        return None
    # FIX v1.1: o psycopg2 trata '%' literal na query como INICIO de
    # placeholder (formatacao estilo printf). Os casos com ILIKE '%...%'
    # quebravam com "tuple index out of range" (log VPS 20/jul: S1-S7, S9,
    # S10, S11, S13 sem amostra do banco). Escapa '%'->'%%' SOMENTE no
    # fragmento do caso; o placeholder real %s do bookmaker fica intacto.
    sql_caso = caso['sql'].replace('%', '%%')
    sql = f"""
        SELECT DISTINCT ON (mercado, selecao)
               COALESCE(mercado,''), COALESCE(mercado_tipo::text,''),
               COALESCE(selecao,''), COALESCE(linha,'')
        FROM ticks
        WHERE bookmaker ILIKE %s AND ts >= NOW() - INTERVAL '{int(args.dias)} days'
          AND ({sql_caso})
        ORDER BY mercado, selecao
        LIMIT {int(args.n)}
    """
    conn = None
    try:
        conn = psycopg2.connect(args.dsn)
        cur = conn.cursor()
        cur.execute(f"SET statement_timeout = {int(args.timeout * 1000)}")
        cur.execute(sql, (f"%{caso['casa']}%",))
        return cur.fetchall()
    except Exception as e:
        log.msg(f"[AVISO] {caso['id']}: consulta falhou ({e}) — segue só com sintéticos")
        return None
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Bancada do matching (etapa 2)")
    ap.add_argument("--seco", action="store_true", help="só ticks embutidos, sem banco")
    ap.add_argument("--dias", type=int, default=DIAS_DEFAULT)
    ap.add_argument("--n", type=int, default=N_DEFAULT)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--dsn", type=str, default=DSN_DEFAULT)
    args = ap.parse_args()

    log = Log(os.path.join(PASTA_SCRIPT, "bancada_log.txt"))
    log.msg(f"=== bancada_matching.py — etapa 2 | modo={'SECO' if args.seco else 'BANCO+SECO'} ===")

    resultados = []
    total_fail = 0
    try:
        for caso in CASOS:
            ticks = list(caso["sinteticos"])
            origem = f"{len(ticks)} sintéticos"
            if not args.seco:
                reais = buscar_banco(caso, args, log)
                if reais:
                    ticks += [tuple(r) for r in reais]
                    origem += f" + {len(reais)} do banco"
                elif reais is not None:
                    origem += " + 0 do banco (SEM_DADO na janela)"
            ok, fail, pfail, exemplos, notas = testar_ticks(caso, ticks, log)
            status = "OK" if (fail == 0 and pfail == 0) else "FAIL"
            if status == "FAIL":
                total_fail += 1
            log.msg(f"[{status}] {caso['id']} ({caso['casa']}/{caso['bot']}, esperado={caso['esperado']}) — {caso['desc']}")
            log.msg(f"       {origem} | acertos {ok}/{ok+fail}" + (f" | {notas}" if notas else ""))
            for ex in exemplos:
                log.msg(f"       FALHA: {ex}")
            resultados.append({
                "caso": caso["id"], "casa": caso["casa"], "mercado_bot": caso["bot"],
                "esperado": caso["esperado"], "descricao": caso["desc"],
                "ticks_testados": ok + fail, "acertos": ok, "falhas": fail,
                "falhas_parsing": pfail, "status": status,
                "exemplos_falha": " || ".join(exemplos), "notas": notas,
            })

        caminho_csv = os.path.join(PASTA_SCRIPT, "bancada_resultado.csv")
        with open(caminho_csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(resultados[0].keys()), delimiter=";")
            w.writeheader()
            for r in resultados:
                w.writerow(r)

        log.msg("")
        log.msg(f"RESULTADO: {len(CASOS) - total_fail}/{len(CASOS)} casos OK | CSV: {caminho_csv}")
        log.msg("GATE etapa 2: 100% — qualquer FAIL acima precisa de correção no motor antes da etapa 3.")
        sys.exit(0 if total_fail == 0 else 1)
    except SystemExit:
        raise
    except Exception:
        log.msg("ERRO INESPERADO:\n" + traceback.format_exc())
        sys.exit(4)
    finally:
        log.fechar()


if __name__ == "__main__":
    main()
