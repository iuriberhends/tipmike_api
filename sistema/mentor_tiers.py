# -*- coding: utf-8 -*-
"""
mentor_tiers.py
===============
Calcula o PERFIL DA PROVA (spec §3.7) e a SUFICIENCIA (§3.8).

  tier   = quanto esse topico cai   -> S | A | B | C | Z
  suficiencia = as questoes dao conta do ciclo inteiro?

Os dois sao CALCULADOS a partir das questoes, nunca opinados. Cada topico
recebe tambem a frase de evidencia que aparece no painel ("CAI NA PMBA -
caiu em 2023 e 2020").

USO:
    python mentor_tiers.py --simular
        Calcula a partir dos JSONs e mostra o relatorio. Nao toca no banco.

    python mentor_tiers.py --aplicar
        Grava tier e suficiencia no schema mentor do mikedb.

DEPENDE do vinculo questao->topico (assunto_ids). Onde ele falta, o topico
sai como "sem dados" em vez de Z - a diferenca importa: Z significa "medi e
nao cai", sem dados significa "nao consegui medir".
"""

import sys
import io
import os
import re
import json
import glob
import argparse
from pathlib import Path
from datetime import datetime
from collections import Counter
from urllib.parse import urlparse, parse_qs

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)

RAIZ = Path(__file__).parent
DIR_SAIDA = RAIZ / "saida"
DSN_PADRAO = os.environ.get("MIKEDB_DSN", os.environ.get(
    "MENTOR_DSN",
    # Sem a senha no codigo: ela vem do ambiente. O repo e privado, mas senha
    # em git fica no historico pra sempre - se um dia o repo abrir, ou alguem
    # ganhar acesso de leitura, ela vaza junto com tudo que ja foi commitado.
    "postgresql://postgres@localhost:5432/mikedb"))

# ---------------------------------------------------------------------------
# Limiares. Ficam aqui, nomeados, porque sao decisao - nao verdade.
# ---------------------------------------------------------------------------

# §3.7 - hierarquia de evidencia
ORGAOS_ALVO = re.compile(r'PM ?BA|CBM ?BA|Pol[ií]cia Militar da Bahia'
                         r'|Corpo de Bombeiros.*Bahia', re.I)

# O tier S da spec e "caiu na PM-BA SOLDADO", nao "caiu na PM-BA".
# Da passada por orgao, so 37% e de Soldado: o resto e Oficial (exige
# faculdade), Medico, Odontologo - provas de outro nivel e outro conteudo.
# Sem esta distincao, um topico que so apareceu na prova de Odontologia
# viraria "CAI NA PMBA" e furaria a fila de estudo com sinal falso.
CARGO_ALVO = re.compile(r'soldado|praça|pra[cç]a', re.I)
# Oficial da mesma corporacao: mesma banca e mesmo edital-base, mas nivel
# superior. Vale como evidencia, um degrau abaixo.
CARGO_MESMA_CORPORACAO = re.compile(r'oficial|tenente|cabo|sargento', re.I)
# banca de referencia: as que ja aplicaram na PM-BA (medido, nao chutado)
BANCAS_REFERENCIA = re.compile(r'IBFC|Carlos Chagas|FCC|UNEB|CONSULTEC', re.I)

TIER_A_MIN_POLICIAL = 10     # questoes em carreira militar estadual
TIER_B_MIN_TOTAL = 20
TIER_C_MIN_TOTAL = 1

# §3.8 - suficiencia
ALVO_TOTAL = 45              # 15 pos-aula + 5 reestudo + 5 R1 + 10 R7 + 10 R30
MINIMO_TOTAL = 20
MIN_PCT_REFERENCIA = 0.30
MIN_PCT_RECENTE = 0.20
ANOS_RECENTE = 5


def assuntos_da_url(url):
    if not url:
        return set()
    q = parse_qs(urlparse(url).query)
    ids = set()
    for chave in ("assunto", "a"):
        for v in q.get(chave, []):
            ids.update(int(x) for x in str(v).split(",") if x.strip().isdigit())
    return ids


def carregar():
    """Le aulas e questoes dos JSONs e liga uma coisa na outra."""
    topicos = []
    for arq in sorted(glob.glob(str(DIR_SAIDA / "aulas" / "*" / "*.json"))):
        d = json.load(open(arq, encoding="utf-8"))
        topicos.append({
            "codigo_aula": str(d.get("codigo_aula")),
            "titulo": d.get("titulo"),
            "disciplina": d.get("disciplina"),
            "modulo": (d.get("modulo") or {}).get("titulo"),
            "ordem": d.get("ordem"),
            "assunto_ids": assuntos_da_url(d.get("caderno_questoes")),
            "questoes": [],
        })

    por_id = {}
    for arq in sorted(glob.glob(str(DIR_SAIDA / "questoes" / "*" / "*.json"))):
        for q in json.load(open(arq, encoding="utf-8")).get("questoes") or []:
            if q.get("id"):
                por_id[q["id"]] = q
    questoes = list(por_id.values())

    # --- assuntos genericos demais para servir de vinculo ---
    #
    # Muitos cadernos incluem o assunto RAIZ da disciplina (ex.: 406223 =
    # "Direito Penal" inteiro). Como quase toda questao de Penal carrega esse
    # id, ela casaria com TODAS as aulas que o listam - o vinculo vira ruido e
    # o tier sai inflado (82 topicos viraram "S" com contagens identicas).
    #
    # Em vez de listar as raizes na mao, MEDE-SE: assunto que aparece em mais
    # de um terco dos cadernos de uma disciplina nao distingue nada.
    por_disciplina = {}
    for t in topicos:
        por_disciplina.setdefault(t["disciplina"], []).append(t)

    genericos = set()
    for disc, ts in por_disciplina.items():
        freq = Counter()
        for t in ts:
            freq.update(t["assunto_ids"])
        limite = max(2, len(ts) // 3)
        genericos.update(a for a, n in freq.items() if n > limite)

    for t in topicos:
        t["assunto_ids_uteis"] = t["assunto_ids"] - genericos

    # o vinculo da spec §3.3.1: interseccao dos assuntos ESPECIFICOS
    sem_link = 0
    for q in questoes:
        ids = set(q.get("assunto_ids") or [])
        if not ids:
            sem_link += 1
            continue
        for t in topicos:
            if ids & t["assunto_ids_uteis"]:
                t["questoes"].append(q)
    return topicos, questoes, sem_link, genericos


# ---------------------------------------------------------------------------
# §3.7 - tier
# ---------------------------------------------------------------------------

def eh_da_bahia(q):
    return bool(ORGAOS_ALVO.search((q.get("orgao") or "") + " " +
                                   (q.get("orgao_sigla") or "")))


def calcular_tier(qs):
    """Devolve (tier, frase_de_evidencia). None se nao der pra medir."""
    if not qs:
        return None, "sem questoes ligadas - nao consegui medir"

    baianas = [q for q in qs if eh_da_bahia(q)]
    soldado = [q for q in baianas if CARGO_ALVO.search(q.get("cargo") or "")]
    if soldado:
        anos = sorted({q.get("ano") for q in soldado if q.get("ano")}, reverse=True)
        quais = ", ".join(str(a) for a in anos[:3])
        return "S", f"CAI NA PMBA - {len(soldado)} questao(oes) de Soldado, em {quais}"

    # Mesma corporacao, outro cargo (Oficial, Tenente): evidencia boa, mas
    # prova de nivel superior - nao e a sua. Um degrau abaixo do S.
    outros_cargos = [q for q in baianas if CARGO_MESMA_CORPORACAO.search(q.get("cargo") or "")]
    if outros_cargos:
        anos = sorted({q.get("ano") for q in outros_cargos if q.get("ano")}, reverse=True)
        return "A", (f"CAI MUITO - {len(outros_cargos)} questoes na PM-BA, mas em prova de "
                     f"Oficial/Tenente ({', '.join(str(a) for a in anos[:2])}), nao de Soldado")

    militares = [q for q in qs if q.get("militar_estadual")]
    if len(militares) >= TIER_A_MIN_POLICIAL:
        return "A", f"CAI MUITO - {len(militares)} questoes em PM/CBM de outros estados"

    if len(qs) >= TIER_B_MIN_TOTAL:
        return "B", f"CAI - {len(qs)} questoes no banco"
    if len(qs) >= TIER_C_MIN_TOTAL:
        return "C", f"RARO - so {len(qs)} questao(oes)"
    return "Z", "NAO CAI - zero questoes, pode pular"


# ---------------------------------------------------------------------------
# §3.8 - suficiencia
# ---------------------------------------------------------------------------

def calcular_suficiencia(qs):
    ano_corte = datetime.now().year - ANOS_RECENTE
    uteis = [q for q in qs
             if q.get("gabarito") and (q.get("enunciado") or "").strip()
             and not q.get("desatualizada") and not q.get("anulada")
             and q.get("so_texto", True)]
    n = len(uteis)

    faixas = Counter()
    for q in uteis:
        p = (q.get("indice_acerto") or {}).get("percentual")
        if p is None:
            continue
        faixas["dificil" if p < 50 else "media" if p <= 75 else "facil"] += 1

    n_ref = sum(1 for q in uteis if BANCAS_REFERENCIA.search(q.get("banca") or ""))
    n_recente = sum(1 for q in uteis if (q.get("ano") or 0) >= ano_corte)
    pct_ref = n_ref / n if n else 0
    pct_recente = n_recente / n if n else 0

    faltas = []
    if n < MINIMO_TOTAL:
        faltas.append(f"so {n} questoes (minimo {MINIMO_TOTAL})")
    elif n < ALVO_TOTAL:
        faltas.append(f"{n} questoes (alvo {ALVO_TOTAL})")
    if len(faixas) < 3:
        faltam = {"dificil", "media", "facil"} - set(faixas)
        faltas.append(f"sem faixa {'/'.join(sorted(faltam))}")
    if pct_ref < MIN_PCT_REFERENCIA:
        faltas.append(f"so {pct_ref*100:.0f}% da banca de referencia")
    if pct_recente < MIN_PCT_RECENTE:
        faltas.append(f"so {pct_recente*100:.0f}% recentes")

    if n < MINIMO_TOTAL:
        status = "insuficiente"
    elif faltas:
        status = "fino"
    else:
        status = "suficiente"
    return {
        "n_total": n, "n_ref": n_ref, "faixas": dict(faixas),
        "pct_ref": round(pct_ref, 3), "pct_recente": round(pct_recente, 3),
        "status": status, "motivo": "; ".join(faltas) or "tudo no alvo",
    }


# ---------------------------------------------------------------------------
# Relatorio
# ---------------------------------------------------------------------------

def relatar(topicos, questoes, sem_link, genericos):
    print("=" * 74)
    print("  PERFIL DA PROVA (3.7) e SUFICIENCIA (3.8)")
    print("=" * 74)
    print(f"  {len(topicos)} topicos · {len(questoes)} questoes")
    if genericos:
        print(f"  {len(genericos)} assuntos genericos descartados do vinculo "
              f"(apareciam em >1/3 dos cadernos)")
    media = sum(len(t["assunto_ids_uteis"]) for t in topicos) / max(1, len(topicos))
    print(f"  media de {media:.1f} assuntos especificos por topico")
    if sem_link:
        print(f"  [!] {sem_link} questoes SEM assunto_ids - nao entram em nenhum topico.")
        print("      Enquanto isso durar, o tier sai subestimado.")

    for t in topicos:
        t["tier"], t["tier_motivo"] = calcular_tier(t["questoes"])
        t["suf"] = calcular_suficiencia(t["questoes"])

    print("\n  --- TIER ---")
    cont = Counter(t["tier"] for t in topicos)
    rotulos = {"S": "CAI NA PMBA", "A": "CAI MUITO", "B": "CAI",
               "C": "RARO", "Z": "NAO CAI", None: "sem dados"}
    for k in ["S", "A", "B", "C", "Z", None]:
        if cont.get(k):
            print(f"     {str(k or '-'):3} {rotulos[k]:14} {cont[k]:>4} topicos")

    print("\n  --- SUFICIENCIA ---")
    cs = Counter(t["suf"]["status"] for t in topicos)
    for k in ["suficiente", "fino", "insuficiente"]:
        print(f"     {k:14} {cs.get(k, 0):>4}")

    criticos = [t for t in topicos
                if t["tier"] in ("S", "A") and t["suf"]["status"] == "insuficiente"]
    print(f"\n  ALERTA VERMELHO (tier S/A + insuficiente): {len(criticos)}")
    for t in criticos[:10]:
        print(f"     [{t['tier']}] {t['titulo'][:46]:46} {t['suf']['motivo'][:40]}")

    print("\n  --- OS TIER S (o que ja caiu na PMBA) ---")
    esses = [t for t in topicos if t["tier"] == "S"]
    for t in sorted(esses, key=lambda x: -x["suf"]["n_total"])[:12]:
        print(f"     {t['titulo'][:40]:40} {t['suf']['n_total']:>4}q  {t['tier_motivo'][:44]}")
    if not esses:
        print("     nenhum - provavelmente por falta do vinculo questao->topico")
    print("=" * 74)


def main():
    ap = argparse.ArgumentParser(description="Calcula tier e suficiencia por topico.")
    ap.add_argument("--simular", action="store_true", help="so calcula e mostra")
    ap.add_argument("--aplicar", action="store_true", help="grava no banco")
    ap.add_argument("--dsn", default=DSN_PADRAO)
    ap.add_argument("--saida", default=None)
    args = ap.parse_args()

    global DIR_SAIDA
    if args.saida:
        DIR_SAIDA = Path(args.saida).expanduser().resolve()
    else:
        # mesma historia do mentor_carga: no PC e 'saida', na VPS e 'dados_m'
        aqui = Path(__file__).resolve().parent
        for c in (DIR_SAIDA, aqui / "dados_m", aqui / "saida",
                  Path.cwd() / "dados_m", Path.cwd() / "saida"):
            if c and Path(c).exists() and (Path(c) / "aulas").exists():
                DIR_SAIDA = Path(c).resolve()
                break

    topicos, questoes, sem_link, genericos = carregar()
    relatar(topicos, questoes, sem_link, genericos)

    if not args.aplicar:
        print("\n  (--simular: nada foi gravado)\n")
        return

    import psycopg2
    import psycopg2.extras as ex
    conn = psycopg2.connect(args.dsn)
    cur = conn.cursor()
    try:
        for t in topicos:
            cur.execute("""
                UPDATE mentor.topico
                   SET tier = %s::mentor.tier_prova, tier_motivo = %s, tier_em = now()
                 WHERE codigo_aula = %s
            """, (t["tier"], t["tier_motivo"], t["codigo_aula"]))
            s = t["suf"]
            cur.execute("""
                INSERT INTO mentor.suficiencia
                    (topico_id, n_total, n_ref, faixas_dif, pct_recente, status,
                     motivo, calculado_em)
                VALUES ((SELECT id FROM mentor.topico WHERE codigo_aula = %s),
                        %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (topico_id) DO UPDATE SET
                    n_total = EXCLUDED.n_total, n_ref = EXCLUDED.n_ref,
                    faixas_dif = EXCLUDED.faixas_dif, pct_recente = EXCLUDED.pct_recente,
                    status = EXCLUDED.status, motivo = EXCLUDED.motivo,
                    calculado_em = now()
            """, (t["codigo_aula"], s["n_total"], s["n_ref"],
                  json.dumps(s["faixas"]), s["pct_recente"], s["status"], s["motivo"]))
        conn.commit()
        print(f"\n  gravado: {len(topicos)} topicos\n")
    except Exception:
        conn.rollback()
        print("\n  [ERRO] rollback - nada gravado.")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
