# -*- coding: utf-8 -*-
r"""
criar_sentinelas.py v1.0 — ETAPA 3: cria os 9 bots sentinela via INSERT

Por que adaptativo: o CREATE TABLE de `bots` nao esta no repo (migrations
comecam na 004), entao o script LE o schema real da tabela no banco
(information_schema.columns) e monta o INSERT somente com colunas que
existem, no tipo certo (torneios/filtros: text[] vs jsonb detectado).

Regras:
  - user_id (NOT NULL, FK usuarios — migration 014): herdado do bot mais
    antigo existente; se nao houver bot, usa o admin ativo mais antigo.
  - status='ativo', ativado_em=NOW() (migration 006).
  - Idempotente: sentinela cujo nome ja existe e PULADO (nunca duplica).
  - Filtro escancarado: odd_min=1.01, sem odd_max, sem linha_min/max,
    lados=[] (ambos), evitarLinhasSeq=True, max_apostas_partida=1,
    sem chips de historico nos O/U (zero gate).
  - HC escancarado de verdade: hc_pct_min=0 e hc_min_partidas=0 SE essas
    colunas existirem; e SEMPRE um chip hist wr janela 'all' com prob
    [0,100] e minPartidas=0 nos filtros (cobre o caso de as colunas nao
    existirem — senao o motor aplicaria o default seguro 87%/20 partidas
    e o sentinela ficaria mudo). Limitacao de design do motor que fica:
    par SEM nenhum jogo de historico nao passa no HC (pct incalculavel).

Uso (na RAIZ do projeto, na VPS):
    python criar_sentinelas.py --dry     # mostra o que faria, nao insere
    python criar_sentinelas.py           # cria os 9
    python criar_sentinelas.py --desativar   # pausa todos os SENTINELA-%

Saida: criar_sentinelas_log.txt na pasta do script. Exit 0 = tudo criado
(ou ja existia). BLINDADO: transacao unica, rollback em erro, motivos claros.
"""

import argparse
import json
import os
import sys
import traceback
from datetime import datetime

PASTA_SCRIPT = os.path.dirname(os.path.abspath(__file__))
DSN_DEFAULT = os.environ.get(
    "MIKEDB_DSN", "postgresql://postgres:mikedb0702@localhost:5432/mikedb"
)

DESCRICAO = "Sentinela etapa 3 - filtro escancarado (validacao do caminho vivo)"

FILTROS_BASE = {"lados": [], "evitarLinhasSeq": True}
# chip de historico "sem corte": wr sobre TODAS as partidas, prob 0-100
# (min/max inativos), maturidade 0. So entra nos sentinelas de HC — nos O/U
# a ausencia total de chips e o escancarado maximo (nem consulta H2H).
CHIP_HC_ABERTO = {"base": "match", "janela": "all", "prob": [0, 100],
                  "tipo": "all", "versao": "all", "minPartidas": 0}

SENTINELAS = [
    dict(nome="SENTINELA-S-ADRIATIC-HC", casa="superbet", esporte="nba2k",
         mercado="ah_ft", torneios=["Adriatic Premier"]),
    dict(nome="SENTINELA-B-NBA2K-HC", casa="betano", esporte="nba2k",
         mercado="ah_ft", torneios=["NBA 2K Battle European Conference"]),
    dict(nome="SENTINELA-B-NBA2K-OU", casa="betano", esporte="nba2k",
         mercado="over_under_ft", torneios=["NBA 2K Battle European Conference"]),
    dict(nome="SENTINELA-E-WCA-HC", casa="estrelabet", esporte="fifa",
         mercado="ah_ft", torneios=["World Cup A"]),
    dict(nome="SENTINELA-E-WCA-OU", casa="estrelabet", esporte="fifa",
         mercado="over_under_ft", torneios=["World Cup A"]),
    dict(nome="SENTINELA-E-WCA-OUHT", casa="estrelabet", esporte="fifa",
         mercado="over_under_ht", torneios=["World Cup A"]),
    dict(nome="SENTINELA-S-ESTRELAS-HC", casa="superbet", esporte="fifa",
         mercado="ah_ft", torneios=["Liga das Estrelas"]),
    dict(nome="SENTINELA-S-ESTRELAS-OU", casa="superbet", esporte="fifa",
         mercado="over_under_ft", torneios=["Liga das Estrelas"]),
    # CONTROLE NEGATIVO: celula sem OU de confronto no jogo inteiro
    # (censo etapa 1) — tem que fechar a janela com ZERO apostas.
    dict(nome="SENTINELA-NEG-E-NBA2K-OU", casa="estrelabet", esporte="nba2k",
         mercado="over_under_ft", torneios=[]),
]


class Log:
    def __init__(self, caminho):
        try:
            self._fh = open(caminho, "w", encoding="utf-8")
        except OSError as e:
            self._fh = None
            print(f"[AVISO] sem log em arquivo ({e})")

    def msg(self, texto=""):
        linha = f"[{datetime.now().strftime('%H:%M:%S')}] {texto}" if texto else ""
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


def _adaptar(valor, data_type, udt_name):
    """Converte o valor Python pro formato da coluna real.
    - jsonb/json: json.dumps (com cast no SQL)
    - ARRAY: lista Python (psycopg2 adapta pra text[])
    - resto: valor cru."""
    dt = (data_type or "").lower()
    if dt in ("json", "jsonb"):
        return json.dumps(valor, ensure_ascii=False)
    if dt == "array":
        return list(valor) if isinstance(valor, (list, tuple)) else [valor]
    return valor


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Cria os bots sentinela (etapa 3)")
    ap.add_argument("--dry", action="store_true", help="mostra sem inserir")
    ap.add_argument("--desativar", action="store_true",
                    help="pausa todos os bots SENTINELA-%% e sai")
    ap.add_argument("--dsn", type=str, default=DSN_DEFAULT)
    args = ap.parse_args()

    log = Log(os.path.join(PASTA_SCRIPT, "criar_sentinelas_log.txt"))
    log.msg(f"=== criar_sentinelas.py v1.0 | {'DRY-RUN' if args.dry else ('DESATIVAR' if args.desativar else 'CRIAR')} ===")

    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        log.msg("ERRO: psycopg2 ausente (pip install psycopg2-binary)")
        sys.exit(2)

    conn = None
    try:
        conn = psycopg2.connect(args.dsn)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if args.desativar:
            cur.execute("UPDATE bots SET status='pausado' "
                        "WHERE nome ILIKE 'SENTINELA-%' RETURNING nome")
            nomes = [r["nome"] for r in cur.fetchall()]
            conn.commit()
            log.msg(f"Pausados {len(nomes)}: {', '.join(nomes) if nomes else '(nenhum)'}")
            sys.exit(0)

        # ---- 1) schema real da tabela bots ----
        cur.execute("""
            SELECT column_name, data_type, udt_name, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='bots'
        """)
        cols = {r["column_name"]: r for r in cur.fetchall()}
        if not cols:
            log.msg("ERRO: tabela 'bots' nao encontrada no schema public.")
            sys.exit(3)
        log.msg(f"Tabela bots: {len(cols)} colunas — "
                + ", ".join(sorted(cols.keys())))

        tem_hc_cols = "hc_pct_min" in cols and "hc_min_partidas" in cols
        if not tem_hc_cols:
            log.msg("[NOTA] colunas hc_pct_min/hc_min_partidas nao existem — "
                    "HC escancarado via chip de historico aberto (fallback ja previsto).")

        # ---- 2) user_id dono ----
        user_id = None
        if "user_id" in cols:
            cur.execute("SELECT user_id FROM bots WHERE user_id IS NOT NULL "
                        "ORDER BY id LIMIT 1")
            r = cur.fetchone()
            if r:
                user_id = r["user_id"]
                log.msg(f"user_id herdado do bot mais antigo: {user_id}")
            else:
                cur.execute("SELECT id FROM usuarios WHERE role='admin' AND ativo "
                            "ORDER BY id LIMIT 1")
                r = cur.fetchone()
                if r:
                    user_id = r["id"]
                    log.msg(f"user_id do admin ativo mais antigo: {user_id}")
                else:
                    log.msg("ERRO: bots.user_id e obrigatorio e nao achei nem bot "
                            "existente nem admin ativo pra herdar. Aborto.")
                    sys.exit(3)

        # ---- 3) ja existentes (idempotencia) ----
        cur.execute("SELECT nome FROM bots WHERE nome ILIKE 'SENTINELA-%'")
        existentes = {r["nome"] for r in cur.fetchall()}
        if existentes:
            log.msg(f"Ja existem (serao pulados): {', '.join(sorted(existentes))}")

        # ---- 4) monta e executa os INSERTs ----
        criados, pulados = [], []
        for s in SENTINELAS:
            if s["nome"] in existentes:
                pulados.append(s["nome"])
                continue

            eh_hc = s["mercado"].startswith("ah_")
            filtros = dict(FILTROS_BASE)
            if eh_hc:
                filtros["filtrosHistAdicionados"] = [dict(CHIP_HC_ABERTO)]

            desejado = {
                "nome": s["nome"],
                "descricao": DESCRICAO,
                "status": "ativo",
                "casa": s["casa"],
                "esporte": s["esporte"],
                "mercado": s["mercado"],
                "filtros": filtros,
                "linha_min": None, "linha_max": None,
                "odd_min": 1.01, "odd_max": None,
                "max_apostas_dia": None,
                "max_apostas_partida": 1,
                "blacklist_jogadores": None, "whitelist_jogadores": None,
                "torneios": s["torneios"] if s["torneios"] else None,
                "torneios_excluir": None,
                "user_id": user_id,
            }
            if tem_hc_cols and eh_hc:
                desejado["hc_pct_min"] = 0
                desejado["hc_min_partidas"] = 0
            if "ativado_em" in cols:
                desejado["ativado_em"] = datetime.now()
            if "criado_em" in cols and not cols["criado_em"]["column_default"]:
                desejado["criado_em"] = datetime.now()

            campos, valores, casts = [], [], []
            for c, v in desejado.items():
                if c not in cols:
                    continue  # coluna nao existe nesta instalacao — ignora
                meta = cols[c]
                campos.append(c)
                valores.append(_adaptar(v, meta["data_type"], meta["udt_name"]))
                casts.append("%s::jsonb" if (meta["data_type"] or "").lower()
                             in ("json", "jsonb") else "%s")

            # colunas NOT NULL sem default que ficaram de fora -> aborta claro
            faltando = [c for c, m in cols.items()
                        if m["is_nullable"] == "NO" and m["column_default"] is None
                        and c not in campos and c != "id"]
            if faltando:
                log.msg(f"ERRO: colunas obrigatorias sem valor pro INSERT: "
                        f"{faltando} — me mande o schema dessas colunas.")
                conn.rollback()
                sys.exit(3)

            sql = (f"INSERT INTO bots ({', '.join(campos)}) "
                   f"VALUES ({', '.join(casts)}) RETURNING id")
            if args.dry:
                log.msg(f"[DRY] {s['nome']}: INSERT {len(campos)} colunas "
                        f"({', '.join(campos)})")
                continue
            cur.execute(sql, valores)
            novo_id = cur.fetchone()["id"]
            criados.append((novo_id, s["nome"]))
            log.msg(f"[CRIADO] id={novo_id} {s['nome']} — {s['casa']}/{s['esporte']}"
                    f"/{s['mercado']} torneios={s['torneios'] or '(sem)'}")

        if args.dry:
            conn.rollback()
            log.msg("DRY-RUN: nada foi gravado.")
        else:
            conn.commit()
            log.msg("")
            log.msg(f"RESULTADO: {len(criados)} criados, {len(pulados)} ja existiam.")
            if criados:
                log.msg("Confira no painel: os 9 devem aparecer como ATIVO. "
                        "Janela de ~24h comeca agora; depois rode "
                        "'python auditoria_sentinelas.py --horas 24'.")
        sys.exit(0)

    except SystemExit:
        raise
    except Exception:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        log.msg("ERRO INESPERADO:\n" + traceback.format_exc())
        sys.exit(4)
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass
        log.fechar()


if __name__ == "__main__":
    main()
