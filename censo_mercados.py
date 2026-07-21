# -*- coding: utf-8 -*-
r"""
censo_mercados.py — ETAPA 1 do plano de validação dos bots (TipMike / MikeDB)  [v2]

Objetivo: responder, com TICKS REAIS (nunca o mapa de mercado), a pergunta:
  "nesta casa × esporte × liga existe HC FT? existe Over/Under FT?"

Duas fontes de dados (mesmas saídas):
  --fonte pg       (default) lê a tabela `ticks` do Postgres — janela QUENTE.
                   ATENÇÃO: a retenção do banco é ~3 dias (o resto vira placar),
                   então o default aqui é --dias 3. Janela maior que isso no PG
                   só devolve o que ainda não foi expurgado.
  --fonte parquet  lê o histórico consolidado em E:\historico (partições
                   bookmaker=/liga=/data=). Use p/ censo longo (--dias 30) e
                   p/ ligas que não rodaram nos últimos 3 dias.

Saídas (na pasta --out):
  1) censo_matriz.csv  — 1 linha por casa×esporte×liga com o VEREDITO:
                         hc_ft_status e ou_ft_status em
                         TEM / POUCO_DADO / SO_OUTROS_PERIODOS / NAO_TEM
  2) censo_detalhe.csv — 1 linha por mercado bruto encontrado, com exemplos
                         reais de linha e seleção (auditoria + pegadinhas da etapa 2)
  3) censo_log.txt     — log passo a passo (zerado a cada execução)

Uso:
    python censo_mercados.py                              (PG, últimos 3 dias)
    python censo_mercados.py --dias 2 --casa superbet
    python censo_mercados.py --fonte parquet --dias 30
    python censo_mercados.py --fonte parquet --hist E:\historico --de 2026-06-20 --ate 2026-07-18

Flags:
    --fonte pg|parquet  fonte dos ticks (default pg)
    --dias N            janela: últimos N dias (default: 3 no pg, 30 no parquet)
    --de / --ate        janela explícita YYYY-MM-DD (--ate inclusivo)
    --casa X            filtro parcial (contém, sem case) em bookmaker
    --sport X           filtro parcial em sport
    --liga X            filtro parcial em liga (no parquet a liga vem do path:
                        minúscula e espaço vira _, ex.: adriatic_premier)
    --min-eventos N     mínimo de event_id distintos p/ veredito TEM (default 5)
    --timeout S         statement_timeout no Postgres, em segundos (default 300)
    --dsn ...           DSN do Postgres (default: env MIKEDB_DSN ou o local da VPS)
    --hist PASTA        raiz do histórico Parquet (default E:\historico)
    --out PASTA         pasta de saída (default: <pasta_do_script>\censo_saida)

Dependências: fonte pg = psycopg2 (ou psycopg v3); fonte parquet = pyarrow
(o venv do PyCharmMiscProject, do consolidar_parquet, já tem).

A classificação de período/família é HEURÍSTICA de reporte — o veredito fino é
seu, olhando o censo_detalhe.csv (exemplos crus + flags de atenção).
"""

import argparse
import csv
import os
import re
import sys
import traceback
import unicodedata
from datetime import datetime, timedelta

# ----------------------------------------------------------------------------
# Config default (pode sobrepor por flag/env)
# ----------------------------------------------------------------------------
DSN_DEFAULT = os.environ.get(
    "MIKEDB_DSN",
    "postgresql://postgres:mikedb0702@localhost:5432/mikedb",
)
HIST_DEFAULT = r"E:\historico"
DIAS_DEFAULT_PG = 3        # retenção real da tabela ticks (~3 dias)
DIAS_DEFAULT_PARQUET = 30
MIN_EVENTOS_DEFAULT = 5
TIMEOUT_S_DEFAULT = 300
RETENCAO_PG_DIAS = 3

MAX_EX_LINHA = 6
MAX_EX_SELECAO = 8

# Saída na pasta do PRÓPRIO script (nunca no cwd — lição do coletor que,
# rodando como SYSTEM, gravava em C:\Windows\System32 por caminho relativo).
PASTA_SCRIPT = os.path.dirname(os.path.abspath(__file__))
OUT_DEFAULT = os.path.join(PASTA_SCRIPT, "censo_saida")

# Colunas que o censo usa (mesma ordem nas duas fontes)
COLS_BASE = ["ts", "bookmaker", "sport", "liga", "mercado_tipo", "mercado",
             "event_id", "linha", "selecao", "odd_status"]

# ----------------------------------------------------------------------------
# Driver do Postgres: aceita psycopg2 OU psycopg (v3) — só exigido na fonte pg
# ----------------------------------------------------------------------------
_DRIVER = None
try:
    import psycopg2 as _pg  # type: ignore
    _DRIVER = "psycopg2"
except ImportError:
    try:
        import psycopg as _pg  # type: ignore
        _DRIVER = "psycopg"
    except ImportError:
        _pg = None


# ----------------------------------------------------------------------------
# Log simples: arquivo (zerado a cada execução) + console
# ----------------------------------------------------------------------------
class Log:
    def __init__(self, caminho):
        self.caminho = caminho
        try:
            os.makedirs(os.path.dirname(caminho), exist_ok=True)
            self._fh = open(caminho, "w", encoding="utf-8")
        except OSError as e:
            self._fh = None
            print(f"[AVISO] Nao consegui abrir o log em {caminho}: {e}")

    def msg(self, texto):
        linha = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {texto}"
        print(linha)
        if self._fh:
            try:
                self._fh.write(linha + "\n")
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
# Normalização e classificação (heurística de reporte)
# ----------------------------------------------------------------------------
def _norm(s):
    """minúsculo, sem acento (NFKD: º->o, ª->a), ° tratado, espaços colapsados."""
    s = (str(s) if s is not None else "").strip().lower().replace("°", "o")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s)


# Ordem importa: do mais específico pro mais genérico. Se nada bater => FT.
_PADROES_PERIODO = [
    ("Q1", [r"(\b1o?|primeiro)\s*quarto\b", r"\bq\s*1\b", r"1st\s*quarter", r"quarto\s*1\b", r"quarter\s*1\b"]),
    ("Q2", [r"(\b2o?|segundo)\s*quarto\b", r"\bq\s*2\b", r"2nd\s*quarter", r"quarto\s*2\b", r"quarter\s*2\b"]),
    ("Q3", [r"(\b3o?|terceiro)\s*quarto\b", r"\bq\s*3\b", r"3rd\s*quarter", r"quarto\s*3\b", r"quarter\s*3\b"]),
    ("Q4", [r"(\b4o?|quarto)\s*quarto\b", r"\bq\s*4\b", r"4th\s*quarter", r"quarto\s*4\b", r"quarter\s*4\b"]),
    ("OT", [r"prorrogacao", r"overtime", r"tempo\s*extra"]),
    ("HT", [r"(\b1o?|primeiro)\s*tempo\b", r"1st\s*half", r"\bht\b", r"1a?\s*parte\b", r"half\s*time", r"intervalo"]),
    ("2T", [r"(\b2o?|segundo)\s*tempo\b", r"2nd\s*half", r"2a?\s*parte\b"]),
]
_PADROES_PERIODO = [
    (rot, [re.compile(p) for p in pats]) for rot, pats in _PADROES_PERIODO
]


def classificar_periodo(mercado_tipo, mercado):
    mt = _norm(mercado_tipo)
    m = _norm(mercado)
    texto = f"{mt} {m}"
    for rotulo, pats in _PADROES_PERIODO:
        for p in pats:
            if p.search(texto):
                return rotulo
    # Convenção do conversor BetsAPI: períodos fora do FT viram PERIOD_*
    if mt.startswith("period"):
        return "PERIODO_?"
    return "FT"


def classificar_familia(mercado_tipo, mercado, exemplos_selecao):
    mt = _norm(mercado_tipo)
    m = _norm(mercado)
    sels = [_norm(s) for s in (exemplos_selecao or [])]

    # HC primeiro (inclui 'spread' — nomenclatura bet365/BetsAPI)
    if "handicap" in mt or "handicap" in m or "spread" in m or mt == "ah_ft" or re.search(r"\bah\b", m):
        return "HC"
    # Over/Under: por tipo, por nome ou pelas seleções ("Mais de"/"Menos de")
    if "over_under" in mt or "over/under" in m or "total" in m or "pontos" in m or re.search(r"\bgols?\b", m) or "golos" in m:
        return "OU"
    if any(("mais de" in s) or ("menos de" in s) or s.startswith("over") or s.startswith("under") for s in sels):
        return "OU"
    return "OUTRO"


_RE_SUFIXO_LINHA = re.compile(r"^[+-]?\d+(?:[.,]\d+)?-[1-4]$")


def pct_linhas_com_sufixo(exemplos_linha):
    """% dos EXEMPLOS de linha com sufixo -1/-2/-3/-4 (período/versão) — flag de atenção."""
    ex = [e for e in (exemplos_linha or []) if e]
    if not ex:
        return 0.0
    n = sum(1 for e in ex if _RE_SUFIXO_LINHA.match(str(e).strip()))
    return 100.0 * n / len(ex)


def fmt_pct(v):
    return f"{v:.1f}".replace(".", ",")


# ----------------------------------------------------------------------------
# FONTE PG — agregação no próprio Postgres
# ----------------------------------------------------------------------------
SQL_CENSO = """
SELECT
    lower(COALESCE(bookmaker, ''))              AS casa,
    COALESCE(sport, '')                         AS sport,
    COALESCE(liga, '')                          AS liga,
    COALESCE(mercado_tipo, '')                  AS mercado_tipo,
    COALESCE(mercado, '')                       AS mercado,
    COUNT(*)                                    AS ticks,
    COUNT(DISTINCT event_id)                    AS eventos,
    MIN(ts)                                     AS primeiro_tick,
    MAX(ts)                                     AS ultimo_tick,
    COUNT(*) FILTER (WHERE odd_status = 1)      AS ticks_suspensos,
    (array_agg(DISTINCT COALESCE(linha, '')))[1:{max_lin}]   AS exemplos_linha,
    (array_agg(DISTINCT COALESCE(selecao, '')))[1:{max_sel}] AS exemplos_selecao
FROM ticks
WHERE ts >= %s AND ts < %s
{filtros}
GROUP BY 1, 2, 3, 4, 5
ORDER BY 1, 2, 3, 4, 5;
"""


def rodar_censo_pg(args, log):
    if _pg is None:
        log.msg("ERRO: nenhum driver Postgres encontrado (psycopg2 ou psycopg).")
        log.msg("Instale no venv do MIKEDB:  pip install psycopg2-binary")
        return None

    janela_dias = (args.dt_ate - args.dt_de).days
    if janela_dias > RETENCAO_PG_DIAS + 1:
        log.msg(f"[AVISO] janela de {janela_dias}d na fonte pg, mas a retencao da tabela ticks e ~{RETENCAO_PG_DIAS}d")
        log.msg("[AVISO] o banco so devolve o que ainda nao foi expurgado — p/ janela longa use --fonte parquet")

    filtros_sql = []
    params = [args.dt_de, args.dt_ate]
    if args.casa:
        filtros_sql.append("AND bookmaker ILIKE %s")
        params.append(f"%{args.casa}%")
    if args.sport:
        filtros_sql.append("AND sport ILIKE %s")
        params.append(f"%{args.sport}%")
    if args.liga:
        filtros_sql.append("AND liga ILIKE %s")
        params.append(f"%{args.liga}%")

    sql = SQL_CENSO.format(filtros="\n".join(filtros_sql),
                           max_lin=MAX_EX_LINHA, max_sel=MAX_EX_SELECAO)

    log.msg(f"Driver: {_DRIVER} | conectando no Postgres...")
    conn = None
    try:
        conn = _pg.connect(args.dsn)
    except Exception as e:
        log.msg(f"ERRO ao conectar no Postgres: {e}")
        log.msg("Checar: servico do PostgreSQL rodando? DSN/senha corretos? (--dsn ou env MIKEDB_DSN)")
        return None

    try:
        cur = conn.cursor()
        try:
            cur.execute(f"SET statement_timeout = {int(args.timeout * 1000)}")
        except Exception as e:
            log.msg(f"[AVISO] nao consegui setar statement_timeout: {e}")

        log.msg("Rodando a agregacao no Postgres...")
        try:
            cur.execute(sql, params)
        except Exception as e:
            log.msg(f"ERRO na query de censo: {e}")
            log.msg(
                "Se foi timeout: reduza --dias, filtre --casa, aumente --timeout, "
                "ou crie indice em ts:  CREATE INDEX IF NOT EXISTS idx_ticks_ts ON ticks (ts);"
            )
            return None

        linhas = cur.fetchall()
        log.msg(f"Agregacao ok: {len(linhas)} grupos (casa x esporte x liga x mercado).")
        return linhas
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# FONTE PARQUET — varre E:\historico (bookmaker=/liga=/data=) em batches
# ----------------------------------------------------------------------------
def rodar_censo_parquet(args, log):
    try:
        import pyarrow.dataset as pads  # type: ignore
    except ImportError:
        log.msg("ERRO: pyarrow nao encontrado neste venv.")
        log.msg("Rode no venv do PyCharmMiscProject (o do consolidar_parquet ja tem) ou:  pip install pyarrow")
        return None

    if not os.path.isdir(args.hist):
        log.msg(f"ERRO: pasta do historico nao existe: {args.hist} (use --hist)")
        return None

    try:
        dataset = pads.dataset(args.hist, format="parquet", partitioning="hive")
    except Exception as e:
        log.msg(f"ERRO ao abrir o dataset parquet em {args.hist}: {e}")
        return None

    nomes = set(dataset.schema.names)
    cols = [c for c in COLS_BASE if c in nomes]
    faltando = [c for c in COLS_BASE if c not in nomes]
    if faltando:
        log.msg(f"[AVISO] colunas ausentes no historico (seguem vazias no censo): {', '.join(faltando)}")
    if not any(c in nomes for c in ("mercado", "mercado_tipo")):
        log.msg("ERRO: historico sem coluna mercado/mercado_tipo — nao da pra censar mercados aqui.")
        return None

    # Filtro grosso por particao de data (string YYYY-MM-DD); fino em Python
    de_str = args.dt_de.strftime("%Y-%m-%d")
    ate_str = (args.dt_ate - timedelta(seconds=1)).strftime("%Y-%m-%d")
    filtro = None
    if "data" in nomes:
        f = pads.field("data")
        filtro = (f >= de_str) & (f <= ate_str)
        log.msg(f"Filtro de particao: data entre {de_str} e {ate_str}")
    else:
        log.msg("[AVISO] particao 'data' nao encontrada — vou varrer tudo e filtrar por ts em Python (mais lento)")

    casa_f = _norm(args.casa) if args.casa else None
    sport_f = _norm(args.sport) if args.sport else None
    liga_f = _norm(args.liga) if args.liga else None

    grupos = {}
    total_linhas = 0
    batches_lidos = 0
    log.msg("Varrendo o historico em batches (RAM constante)...")
    try:
        for batch in dataset.to_batches(columns=cols, filter=filtro, batch_size=65536):
            batches_lidos += 1
            registros = batch.to_pylist()
            for r in registros:
                casa = _norm(r.get("bookmaker"))
                sport = str(r.get("sport") or "")
                liga = str(r.get("liga") or "")
                if casa_f and casa_f not in casa:
                    continue
                if sport_f and sport_f not in _norm(sport):
                    continue
                if liga_f and liga_f not in _norm(liga):
                    continue

                ts = r.get("ts")
                if "data" not in nomes and ts is not None:
                    ts_txt = str(ts)[:19]
                    if not (de_str <= ts_txt[:10] <= ate_str):
                        continue

                total_linhas += 1
                mtipo = str(r.get("mercado_tipo") or "")
                merc = str(r.get("mercado") or "")
                chave = (casa, sport, liga, mtipo, merc)
                g = grupos.get(chave)
                if g is None:
                    g = grupos[chave] = {
                        "ticks": 0, "eventos": set(), "pri": None, "ult": None,
                        "susp": 0, "ex_linha": set(), "ex_sel": set(),
                    }
                g["ticks"] += 1
                ev = r.get("event_id")
                if ev is not None and ev != "":
                    g["eventos"].add(str(ev))
                if ts is not None:
                    ts_cmp = str(ts)
                    if g["pri"] is None or ts_cmp < g["pri"]:
                        g["pri"] = ts_cmp
                    if g["ult"] is None or ts_cmp > g["ult"]:
                        g["ult"] = ts_cmp
                try:
                    if int(r.get("odd_status") or 0) == 1:
                        g["susp"] += 1
                except (TypeError, ValueError):
                    pass
                lin = str(r.get("linha") or "").strip()
                if lin and len(g["ex_linha"]) < MAX_EX_LINHA:
                    g["ex_linha"].add(lin)
                sel = str(r.get("selecao") or "").strip()
                if sel and len(g["ex_sel"]) < MAX_EX_SELECAO:
                    g["ex_sel"].add(sel)

            if batches_lidos % 50 == 0:
                log.msg(f"... {batches_lidos} batches, {total_linhas} ticks no filtro, {len(grupos)} grupos")
    except KeyboardInterrupt:
        raise
    except Exception as e:
        log.msg(f"ERRO varrendo o historico: {e}")
        log.msg(traceback.format_exc())
        return None

    log.msg(f"Varredura ok: {total_linhas} ticks no filtro | {len(grupos)} grupos.")
    if not grupos:
        return []

    linhas = []
    for (casa, sport, liga, mtipo, merc) in sorted(grupos.keys()):
        g = grupos[(casa, sport, liga, mtipo, merc)]
        linhas.append((
            casa, sport, liga, mtipo, merc,
            g["ticks"], len(g["eventos"]), g["pri"], g["ult"], g["susp"],
            sorted(g["ex_linha"]), sorted(g["ex_sel"]),
        ))
    return linhas


# ----------------------------------------------------------------------------
# Processamento + saída (comum às duas fontes)
# ----------------------------------------------------------------------------
def processar(linhas, args, log):
    detalhe = []
    celulas = {}  # (casa, sport, liga) -> acumulador

    for row in linhas:
        (casa, sport, liga, mtipo, merc, ticks, eventos,
         pri, ult, suspensos, ex_linha, ex_sel) = row

        ticks = int(ticks or 0)
        eventos = int(eventos or 0)
        suspensos = int(suspensos or 0)
        ex_linha = [str(x) for x in (ex_linha or []) if str(x).strip()]
        ex_sel = [str(x) for x in (ex_sel or []) if str(x).strip()]

        fam = classificar_familia(mtipo, merc, ex_sel)
        per = classificar_periodo(mtipo, merc)
        pct_susp = (100.0 * suspensos / ticks) if ticks else 0.0
        pct_suf = pct_linhas_com_sufixo(ex_linha)

        detalhe.append({
            "casa": casa, "sport": sport, "liga": liga,
            "mercado_tipo": mtipo, "mercado": merc,
            "familia": fam, "periodo": per,
            "ticks": ticks, "eventos": eventos,
            "pct_suspensos": fmt_pct(pct_susp),
            "pct_linhas_sufixo": fmt_pct(pct_suf),
            "exemplos_linha": " | ".join(ex_linha),
            "exemplos_selecao": " | ".join(ex_sel),
            "primeiro_tick": str(pri)[:19] if pri is not None else "",
            "ultimo_tick": str(ult)[:19] if ult is not None else "",
        })

        chave = (casa, sport, liga)
        c = celulas.setdefault(chave, {
            "total_ticks": 0,
            "primeiro": None, "ultimo": None,
            "hc_ft_ticks": 0, "hc_ft_eventos": 0, "hc_outros": set(),
            "ou_ft_ticks": 0, "ou_ft_eventos": 0, "ou_outros": set(),
            "flags": set(),
        })
        c["total_ticks"] += ticks
        pri_c = str(pri)[:19] if pri is not None else None
        ult_c = str(ult)[:19] if ult is not None else None
        if pri_c is not None:
            c["primeiro"] = pri_c if c["primeiro"] is None else min(c["primeiro"], pri_c)
        if ult_c is not None:
            c["ultimo"] = ult_c if c["ultimo"] is None else max(c["ultimo"], ult_c)

        # eventos por familia = MAIOR contagem de um unico mercado (conservador:
        # nao superconta o mesmo jogo que aparece em varios nomes de mercado;
        # se subcontar, cai em POUCO_DADO e voce decide olhando o detalhe)
        if fam == "HC":
            if per == "FT":
                c["hc_ft_ticks"] += ticks
                c["hc_ft_eventos"] = max(c["hc_ft_eventos"], eventos)
                if pct_suf > 10.0:
                    c["flags"].add("HC_FT_SUFIXO_LINHA")
                if pct_susp > 50.0:
                    c["flags"].add("HC_FT_MUITO_SUSPENSO")
            else:
                c["hc_outros"].add(per)
        elif fam == "OU":
            if per == "FT":
                c["ou_ft_ticks"] += ticks
                c["ou_ft_eventos"] = max(c["ou_ft_eventos"], eventos)
                if pct_susp > 50.0:
                    c["flags"].add("OU_FT_MUITO_SUSPENSO")
            else:
                c["ou_outros"].add(per)

    def veredito(t, e, outros):
        if e >= args.min_eventos:
            return "TEM"
        if t > 0:
            return "POUCO_DADO"
        if outros:
            return "SO_OUTROS_PERIODOS"
        return "NAO_TEM"

    matriz = []
    for (casa, sport, liga), c in sorted(celulas.items()):
        matriz.append({
            "casa": casa, "sport": sport, "liga": liga,
            "total_ticks": c["total_ticks"],
            "primeiro_tick": c["primeiro"] or "",
            "ultimo_tick": c["ultimo"] or "",
            "hc_ft_status": veredito(c["hc_ft_ticks"], c["hc_ft_eventos"], c["hc_outros"]),
            "hc_ft_ticks": c["hc_ft_ticks"],
            "hc_ft_eventos": c["hc_ft_eventos"],
            "hc_outros_periodos": ",".join(sorted(c["hc_outros"])) or "-",
            "ou_ft_status": veredito(c["ou_ft_ticks"], c["ou_ft_eventos"], c["ou_outros"]),
            "ou_ft_ticks": c["ou_ft_ticks"],
            "ou_ft_eventos": c["ou_ft_eventos"],
            "ou_outros_periodos": ",".join(sorted(c["ou_outros"])) or "-",
            "obs": ",".join(sorted(c["flags"])) or "-",
        })

    log.msg(f"Celulas casa x esporte x liga: {len(matriz)}")
    tem_hc = sum(1 for m in matriz if m["hc_ft_status"] == "TEM")
    tem_ou = sum(1 for m in matriz if m["ou_ft_status"] == "TEM")
    log.msg(f"Com HC FT (>= {args.min_eventos} eventos): {tem_hc} | Com OU FT: {tem_ou}")
    return matriz, detalhe


def escrever_csv(caminho, linhas, campos, log):
    try:
        with open(caminho, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=campos, delimiter=";")
            w.writeheader()
            for l in linhas:
                w.writerow(l)
        log.msg(f"Gravado: {caminho} ({len(linhas)} linhas)")
        return True
    except OSError as e:
        log.msg(f"ERRO ao gravar {caminho}: {e}")
        return False


def parse_data(txt, nome):
    try:
        return datetime.strptime(txt, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(f"--{nome} invalido: '{txt}' (use YYYY-MM-DD)")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Censo de mercados por casa x esporte x liga (ticks reais)")
    ap.add_argument("--fonte", choices=["pg", "parquet"], default="pg")
    ap.add_argument("--dias", type=int, default=None,
                    help=f"default: {DIAS_DEFAULT_PG} no pg (retencao ~{RETENCAO_PG_DIAS}d), {DIAS_DEFAULT_PARQUET} no parquet")
    ap.add_argument("--de", type=str, default=None)
    ap.add_argument("--ate", type=str, default=None)
    ap.add_argument("--casa", type=str, default=None)
    ap.add_argument("--sport", type=str, default=None)
    ap.add_argument("--liga", type=str, default=None)
    ap.add_argument("--min-eventos", dest="min_eventos", type=int, default=MIN_EVENTOS_DEFAULT)
    ap.add_argument("--timeout", type=int, default=TIMEOUT_S_DEFAULT)
    ap.add_argument("--dsn", type=str, default=DSN_DEFAULT)
    ap.add_argument("--hist", type=str, default=HIST_DEFAULT)
    ap.add_argument("--out", type=str, default=OUT_DEFAULT)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    log = Log(os.path.join(args.out, "censo_log.txt"))
    log.msg(f"=== censo_mercados.py v2 — Etapa 1 (censo por ticks reais) | fonte={args.fonte} ===")

    try:
        if args.de or args.ate:
            if not (args.de and args.ate):
                raise SystemExit("Use --de e --ate juntos (YYYY-MM-DD).")
            args.dt_de = parse_data(args.de, "de")
            args.dt_ate = parse_data(args.ate, "ate") + timedelta(days=1)
        else:
            dias = args.dias if args.dias else (DIAS_DEFAULT_PG if args.fonte == "pg" else DIAS_DEFAULT_PARQUET)
            args.dt_ate = datetime.now()
            args.dt_de = args.dt_ate - timedelta(days=max(1, dias))

        log.msg(
            f"Janela: {args.dt_de.strftime('%Y-%m-%d %H:%M')} ate {args.dt_ate.strftime('%Y-%m-%d %H:%M')} | "
            f"filtros: casa={args.casa or '-'} sport={args.sport or '-'} liga={args.liga or '-'}"
        )

        if args.fonte == "pg":
            linhas = rodar_censo_pg(args, log)
        else:
            linhas = rodar_censo_parquet(args, log)

        if linhas is None:
            log.msg("Encerrado com ERRO (ver mensagens acima).")
            sys.exit(2)
        if not linhas:
            log.msg("ZERO grupos retornados — janela sem ticks ou filtros nao bateram.")
            if args.fonte == "pg":
                log.msg(f"Lembrete: retencao do banco ~{RETENCAO_PG_DIAS}d. Pra janela maior use --fonte parquet.")
            log.msg("Nada foi gravado.")
            sys.exit(1)

        matriz, detalhe = processar(linhas, args, log)

        ok1 = escrever_csv(
            os.path.join(args.out, "censo_matriz.csv"), matriz,
            ["casa", "sport", "liga", "total_ticks", "primeiro_tick", "ultimo_tick",
             "hc_ft_status", "hc_ft_ticks", "hc_ft_eventos", "hc_outros_periodos",
             "ou_ft_status", "ou_ft_ticks", "ou_ft_eventos", "ou_outros_periodos", "obs"],
            log,
        )
        ok2 = escrever_csv(
            os.path.join(args.out, "censo_detalhe.csv"), detalhe,
            ["casa", "sport", "liga", "mercado_tipo", "mercado", "familia", "periodo",
             "ticks", "eventos", "pct_suspensos", "pct_linhas_sufixo",
             "exemplos_linha", "exemplos_selecao", "primeiro_tick", "ultimo_tick"],
            log,
        )

        log.msg("--- RESUMO (celulas com mercado-alvo) ---")
        for m in matriz:
            if m["hc_ft_status"] == "TEM" or m["ou_ft_status"] == "TEM":
                log.msg(
                    f"{m['casa']} | {m['sport']} | {m['liga']} -> "
                    f"HC_FT={m['hc_ft_status']}({m['hc_ft_eventos']} ev) "
                    f"OU_FT={m['ou_ft_status']}({m['ou_ft_eventos']} ev) obs={m['obs']}"
                )
        log.msg("Concluido." if (ok1 and ok2) else "Concluido COM ERRO de gravacao.")
        sys.exit(0 if (ok1 and ok2) else 3)

    except KeyboardInterrupt:
        log.msg("Interrompido pelo usuario (Ctrl+C).")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception:
        log.msg("ERRO INESPERADO:\n" + traceback.format_exc())
        sys.exit(4)
    finally:
        log.fechar()


if __name__ == "__main__":
    main()
