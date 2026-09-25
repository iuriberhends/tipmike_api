# -*- coding: utf-8 -*-
"""
mentor_carga_v3.py — carga do Mike Mentor (schema v3, spec v3.2)

O que faz, na ordem:
  1. lê o edital_verticalizado_v2.json (12 matérias, tiers, modos, TI, próprias, cards)
  2. lê as aulas (saida/aulas/*/*.json) e junta a cada aula do edital pelo arquivo
  3. lê as questões (saida/questoes/**/*.json + coleta/saida/questoes/**/*.json),
     sem repetir id
  4. monta a árvore de etiquetas (id → pai, raiz) com o que as questões trazem
  5. classifica cada questão: soldado_ba, fora_banca, fora_edital (pelo mapa
     do edital + pais) — nada é apagado
  6. vínculo questão ↔ aula: etiqueta exata, etiqueta pai, e por semelhança de
     texto (TF-IDF) quando a etiqueta é só a raiz/genérica
  7. cards de lei seca (só o esqueleto: a frente/verso vem depois) e bizus do
     resumo de bolso (as 4 seções fixas do Gran)

Tudo numa transação: falhou, dá rollback.

USO (na VPS, na raiz do tipmike_api):
    python mentor_carga_v3.py --simular  --dados saida --edital edital_verticalizado_v2.json
    python mentor_carga_v3.py --tudo     --dados saida --edital edital_verticalizado_v2.json
    python mentor_carga_v3.py --tudo --complemento coleta/saida    (junta a coleta da VPS)
    python mentor_carga_v3.py --so-vinculo                          (refaz só o vínculo)

DSN: MIKEDB_DSN (ambiente ou .env) ou --dsn; sem nada, usa o padrão da VPS, igual ao mentor_tiers.py.
"""
import argparse
import glob
import json
import math
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
# mesma fonte dos outros scripts do repo (mentor_tiers, bancada_matching, criar_sentinelas):
# MIKEDB_DSN no ambiente ou no .env da raiz; senão o DSN padrão da VPS.
try:
    from dotenv import load_dotenv
    load_dotenv(RAIZ / ".env")
except Exception:
    pass
DSN_PADRAO = (os.environ.get("MIKEDB_DSN") or os.environ.get("MENTOR_DSN") or os.environ.get("DATABASE_URL")
              or "postgresql://postgres:mikedb0702@localhost:5432/mikedb")

BANCAS = {"FCC", "IBFC", "FGV", "VUNESP", "IDECAN", "AOCP", "SELECON", "IBADE",
          "CONSULPLAN", "UNEB", "CONSULTEC", "CEBRASPE", "CESPE"}
SECOES_BIZU = ["Pegadinhas Comuns", "Diferenças Críticas", "Fórmulas e Regras", "Checklist Final"]


# ---------------------------------------------------------------- util
def log(msg):
    print(msg, flush=True)


def sem_acento(t):
    t = unicodedata.normalize("NFKD", str(t or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def lista(v):
    """Campos que às vezes vêm como string de lista (coleta antiga)."""
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.startswith("["):
        try:
            import ast
            return ast.literal_eval(v)
        except Exception:
            return []
    return []


def dicionario(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.startswith("{"):
        try:
            import ast
            return ast.literal_eval(v)
        except Exception:
            return {}
    return {}


def banca_ok(sigla, nome):
    s = sem_acento(f"{sigla or ''} {nome or ''}").upper()
    if "AOCP" in s or "CONSULPLAN" in s or "CESPE" in s or "CEBRASPE" in s:
        return True
    return any(b in s.replace("/", " ").split() for b in BANCAS)


# ---------------------------------------------------------------- leitura
def ler_edital(caminho):
    d = json.loads(Path(caminho).read_text(encoding="utf-8"))
    if not d.get("materias"):
        raise SystemExit("edital sem matérias")
    log(f"  edital v{d.get('versao')} aprovado em {d.get('aprovado_em')}: {len(d['materias'])} matérias")
    return d


def ler_aulas(pasta):
    """arquivo (relativo) -> dict da aula, mais índice por codigo_aula e por título."""
    aulas = {}
    for f in sorted(glob.glob(str(Path(pasta) / "aulas" / "*" / "*.json"))):
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception as e:
            log(f"  [!] aula ilegível {f}: {e}")
            continue
        cad = d.get("caderno_questoes") or ""
        m = re.search(r"[?&](?:a|assunto)=([^&]+)", cad)
        d["_etiquetas"] = [int(x) for x in re.split(r"%2C|,", m.group(1)) if x.isdigit()] if m else []
        d["_arquivo"] = str(Path(f).relative_to(pasta)).replace("\\", "/")
        dur = d.get("duracao")
        if isinstance(dur, str):
            p = [int(x) for x in re.findall(r"\d+", dur)]
            dur = (p[0] * 3600 + p[1] * 60 + (p[2] if len(p) > 2 else 0)) if len(p) >= 2 else None
        d["_duracao_s"] = dur if isinstance(dur, (int, float)) else None
        aulas[d["_arquivo"]] = d
    log(f"  aulas lidas: {len(aulas)}")
    return aulas


def ler_questoes(pastas):
    """id -> dict compacto da questão. Primeiro arquivo vence; os outros só completam campos vazios."""
    qs = {}
    arqs = []
    for p in pastas:
        arqs += sorted(glob.glob(str(Path(p) / "questoes" / "*" / "*.json")))
    for f in arqs:
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception as e:
            log(f"  [!] questões ilegíveis {f}: {e}")
            continue
        origem = Path(f).parent.name
        n_new = 0
        CAMPOS = ("id", "enunciado", "alternativas", "gabarito", "banca", "banca_id", "banca_sigla", "ano", "prova_ano",
                  "orgao", "orgao_sigla", "orgao_uf", "cargo", "prova", "assuntos", "assunto_ids", "indice_acerto", "dificuldade",
                  "anulada", "desatualizada", "tem_imagem", "so_texto", "tem_comentario_professor", "comentario_professor")
        for q0 in d.get("questoes") or []:
            qid = q0.get("id")
            if qid is None:
                continue
            q = {k: q0.get(k) for k in CAMPOS if q0.get(k) not in (None, "", [], {})}
            q["id"] = qid
            q["_origem"] = origem
            if qid in qs:
                base = qs[qid]
                for k, v in q.items():
                    if base.get(k) in (None, "", [], {}) and v not in (None, "", [], {}):
                        base[k] = v
            else:
                qs[qid] = q
                n_new += 1
        log(f"    {origem[:40]:40s} {len(d.get('questoes') or []):7d} ({n_new} novas)")
        del d
    log(f"  questões únicas: {len(qs)}")
    return qs


# ---------------------------------------------------------------- árvore
def arvore(qs):
    pai, raiz, nome = {}, {}, {}
    for q in qs.values():
        for a in lista(q.get("assuntos")):
            if not isinstance(a, dict) or a.get("id") in (None, "None", ""):
                continue
            i = int(a["id"])
            nome.setdefault(i, a.get("nome") or a.get("titulo"))
            p, r = a.get("pai"), a.get("raiz")
            if p not in (None, "None", "") and i not in pai:
                pai[i] = int(p)
            if r not in (None, "None", "") and i not in raiz:
                raiz[i] = int(r)
    return pai, raiz, nome


def ancestrais(i, pai, limite=30):
    out, seen, a = [], set(), i
    for _ in range(limite):
        p = pai.get(a)
        if p is None or p in seen or p == a:
            break
        out.append(p)
        seen.add(p)
        a = p
    return out


# ---------------------------------------------------------------- semelhança (TF-IDF simples)
TOKEN = re.compile(r"[a-z0-9]{3,}")
STOP = set("""que nao com para uma por dos das mais como seu sua sao esta este essa esse
sobre entre pode ser foi tem seus suas aos nas nos qual quais quando onde apenas assim
mesmo tambem ainda sendo pela pelo pelos pelas art arts inciso paragrafo lei
questao alternativa correta incorreta assinale considere acerca segundo conforme
codigo penal brasileiro texto acima abaixo item itens julgue afirmativa afirmativas
verdadeira falsa verdadeiro falso caso casos""".split())


def tokens(t):
    return [w for w in TOKEN.findall(sem_acento(t)) if w not in STOP]


def vetor(texto, idf):
    c = Counter(tokens(texto))
    v = {w: (1 + math.log(n)) * idf.get(w, 0) for w, n in c.items() if w in idf}
    norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
    return {w: x / norm for w, x in v.items()}


def cosseno(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(w, 0) for w, x in a.items())


# ---------------------------------------------------------------- carga
def carregar(dsn, edital, aulas, qs, complemento_ok, simular, so_vinculo, limiar):
    import psycopg2
    import psycopg2.extras as ex
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='mentor' AND table_name='questao_aula'")
        if not cur.fetchone():
            raise SystemExit("schema v3 não encontrado: rode a migration 032_mentor_v3.sql antes")

        pai, raiz, nome_et = arvore(qs)
        log(f"  etiquetas conhecidas: {len(nome_et)} ({len(pai)} com pai)")

        # ---- mapa aprovado (edital → etiquetas) e raízes
        aprovado = set()
        raizes_mat = {}
        for m in edital["materias"]:
            raizes_mat[m["nome"]] = {int(x) for x in m.get("etiquetas_raiz") or []}
            for mo in m["modulos"]:
                for a in mo["assuntos"]:
                    for e in a.get("etiquetas") or []:
                        aprovado.add(int(e))
                    for au in a.get("aulas") or []:
                        for e in au.get("etiquetas") or []:
                            aprovado.add(int(e))
        raizes = set().union(*raizes_mat.values())
        aprovado -= raizes

        if not so_vinculo:
            # ---- limpa e regrava a árvore
            cur.execute("TRUNCATE mentor.questao_aula, mentor.bizu, mentor.card, mentor.aula, mentor.assunto, mentor.modulo, mentor.materia RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE mentor.questao, mentor.prova_real, mentor.etiqueta RESTART IDENTITY CASCADE")

            ex.execute_values(cur, "INSERT INTO mentor.etiqueta (id, nome, pai, raiz) VALUES %s",
                              [(i, nome_et.get(i), pai.get(i), raiz.get(i)) for i in nome_et])

            # ---- matérias, módulos, assuntos, aulas
            n_aulas = 0
            aula_id_por_arquivo = {}
            aulas_por_codigo = {str(a.get("codigo_aula")): k for k, a in aulas.items() if a.get("codigo_aula")}
            for m in edital["materias"]:
                cur.execute("INSERT INTO mentor.materia (id, nome, peso_prova, gran_curso_id, gran_disciplina_curso_id, etiquetas_raiz, obs) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                            (m["id"], m["nome"], m.get("peso_prova") or 5, str(m.get("gran_curso_id") or "") or None, str(m.get("gran_disciplina_curso_id") or "") or None,
                             sorted(raizes_mat[m["nome"]]), m.get("obs_v2") or m.get("obs")))
                for mo in m["modulos"]:
                    cur.execute("INSERT INTO mentor.modulo (materia_id, ordem, nome, gran_topico_id, tipo) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                                (m["id"], mo["ordem"], mo["nome"], str(mo.get("gran_topico_id") or "") or None, mo.get("tipo") or "conteudo"))
                    modulo_id = cur.fetchone()[0]
                    for a in mo["assuntos"]:
                        tier = a.get("tier") or ("S" if a.get("base") else "A")
                        if mo.get("tipo") == "revisao":
                            tier = "revisao"
                        if tier == "conteudo_S_aulas_3_a_6":
                            tier = "S"
                        ets = sorted({int(e) for au in a.get("aulas") or [] for e in au.get("etiquetas") or []} |
                                     {int(e) for e in a.get("etiquetas") or []})
                        cur.execute("""INSERT INTO mentor.assunto (materia_id, modulo_id, ordem, nome, tier, modo, base, origem, descricao,
                                       itens_edital, etiquetas, soldado, estoque_me) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                                    (m["id"], modulo_id, a["ordem"], a["nome"], tier, a.get("modo") or "video_1.5x", bool(a.get("base")),
                                     a.get("origem") or ("propria" if mo.get("tipo") == "propria" else "gran"), a.get("descricao"),
                                     a.get("itens_edital") or [], [e for e in ets if e not in raizes], a.get("soldado"), a.get("estoque_me")))
                        assunto_id = cur.fetchone()[0]
                        if mo.get("tipo") == "propria":
                            cur.execute("INSERT INTO mentor.aula (assunto_id, ordem, tipo, titulo, etiquetas, lei) VALUES (%s,1,'propria',%s,%s,%s)",
                                        (assunto_id, a["nome"], [int(e) for e in a.get("etiquetas") or []], a.get("descricao")))
                            n_aulas += 1
                        for au in a.get("aulas") or []:
                            cod = str(au.get("codigo_aula") or "")
                            fonte = aulas.get(aulas_por_codigo.get(cod, ""), {})
                            if not fonte and au.get("origem") == "propria":
                                cur.execute("INSERT INTO mentor.aula (assunto_id, ordem, tipo, titulo, codigo_aula, duracao_s, etiquetas, lei) VALUES (%s,%s,'propria',%s,%s,%s,%s,%s)",
                                            (assunto_id, au["ordem"], au["titulo"], cod or None, au.get("duracao_s"), [int(e) for e in au.get("etiquetas") or []], au.get("lei")))
                                n_aulas += 1
                                continue
                            tipo = au.get("tipo") or "conteudo"
                            if tipo not in ("conteudo", "exercicios", "revisao", "fora", "propria"):
                                tipo = "conteudo"
                            ets_aula = [e for e in (fonte.get("_etiquetas") or [int(x) for x in au.get("etiquetas") or []]) if e not in raizes]
                            cur.execute("""INSERT INTO mentor.aula (assunto_id, ordem, tipo, titulo, codigo_aula, video_id, url, professor, duracao_s,
                                           etiquetas, caderno_url, transcricao, resumo, resumo_bolso, mapa_mental, flashcards, questoes_fixacao, pdf_path, arquivo_origem)
                                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                                        (assunto_id, au["ordem"], tipo, au["titulo"], cod or None, au.get("video_id") or fonte.get("video_id"),
                                         fonte.get("url_aula"), fonte.get("professor"), au.get("duracao_s") or fonte.get("_duracao_s"), ets_aula,
                                         fonte.get("caderno_questoes"), fonte.get("transcricao") if isinstance(fonte.get("transcricao"), str) else json.dumps(fonte.get("transcricao"), ensure_ascii=False) if fonte.get("transcricao") else None,
                                         fonte.get("resumo"), fonte.get("resumo_bolso"), fonte.get("mapa_mental"),
                                         ex.Json(fonte.get("flashcards")) if fonte.get("flashcards") else None,
                                         ex.Json(fonte.get("questoes_fixacao")) if fonte.get("questoes_fixacao") else None,
                                         fonte.get("pdf_degravacao"), fonte.get("_arquivo")))
                            aula_id = cur.fetchone()[0]
                            n_aulas += 1
                            if fonte.get("_arquivo"):
                                aula_id_por_arquivo[fonte["_arquivo"]] = aula_id
                            # bizus das seções fixas do resumo de bolso
                            rb = fonte.get("resumo_bolso") or ""
                            if rb:
                                for sec in SECOES_BIZU:
                                    mm = re.search(r"^#{2,3}[^\n]*" + re.escape(sec) + r"[^\n]*\n(.*?)(?=^#{2,3}\s|\Z)", rb, re.S | re.M)
                                    if not mm:
                                        continue
                                    for linha in mm.group(1).splitlines():
                                        linha = linha.strip().lstrip("-*•").strip()
                                        if 15 < len(linha) < 400 and not linha.startswith("#"):
                                            cur.execute("INSERT INTO mentor.bizu (aula_id, assunto_id, fonte, secao, texto) VALUES (%s,%s,'resumo_bolso',%s,%s)",
                                                        (aula_id, assunto_id, sec, linha))
                # cards de lei seca (esqueleto: frente = nome, verso = "pendente")
                for c in m.get("cards_lei_seca") or []:
                    cur.execute("INSERT INTO mentor.card (assunto_id, tipo, frente, verso, fonte, ativo) VALUES (NULL,'lei_seca',%s,%s,%s,FALSE)",
                                (c["nome"], "PENDENTE: texto da lei a montar", json.dumps(c.get("etiquetas"))))
            log(f"  árvore gravada: {n_aulas} aulas")

            # ---- provas reais
            provas = Counter()
            exemplos = {}
            for q in qs.values():
                if q.get("prova"):
                    nome = str(q["prova"])[:200]
                    provas[nome] += 1
                    exemplos.setdefault(nome, q)
            prova_id = {}
            log(f"  provas encontradas: {len(provas)} (gravando...)")
            for nome, n in provas.items():
                exemplo = exemplos[nome]
                sold = "soldado" in str(exemplo.get("cargo") or "").lower() and str(exemplo.get("orgao_sigla")) in ("PM BA", "CBM BA")
                cur.execute("INSERT INTO mentor.prova_real (nome, banca, orgao_sigla, cargo, ano, soldado_ba, n_questoes) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                            (nome, exemplo.get("banca_sigla") or exemplo.get("banca"), exemplo.get("orgao_sigla"), exemplo.get("cargo"),
                             exemplo.get("prova_ano") or exemplo.get("ano"), sold, n))
                prova_id[nome] = cur.fetchone()[0]
            log(f"  provas: {len(prova_id)} ({sum(1 for n in provas if 'Soldado' in n and ('PM BA' in n or 'CBM BA' in n))} de Soldado da Bahia)")

            # ---- questões
            log("  classificando e gravando as questões...")
            linhas = []
            cont = Counter()
            for qid, q in qs.items():
                alts = lista(q.get("alternativas"))
                alts_j = [{"letra": (a[0] if isinstance(a, (list, tuple)) else a.get("letra")), "texto": (a[1] if isinstance(a, (list, tuple)) else a.get("texto"))}
                          for a in alts] if alts and isinstance(alts[0], (list, tuple, dict)) else alts
                n_alt = len(alts)
                tipo = "CE" if n_alt == 2 else ("ME" if n_alt >= 4 else "outro")
                ets = sorted({int(a["id"]) for a in lista(q.get("assuntos")) if isinstance(a, dict) and str(a.get("id")).isdigit()} |
                             {int(x) for x in lista(q.get("assunto_ids")) if str(x).isdigit()})
                ia = dicionario(q.get("indice_acerto"))
                perc = ia.get("percentual") if ia else (q.get("indice_acerto") if isinstance(q.get("indice_acerto"), (int, float)) else None)
                try:
                    perc = float(perc) if perc is not None else None
                except (TypeError, ValueError):
                    perc = None
                sold = "soldado" in str(q.get("cargo") or "").lower() and str(q.get("orgao_sigla")) in ("PM BA", "CBM BA")
                fb = not banca_ok(q.get("banca_sigla"), q.get("banca")) and not sold
                # fora_edital: alguma etiqueta (ou pai) aprovada → False; todas com caminho até a raiz e nenhuma aprovada → True; senão NULL
                dentro = any(e in aprovado or any(c in aprovado for c in ancestrais(e, pai)) for e in ets)
                if dentro or sold:
                    fe = False
                elif ets and all((raiz.get(e) is not None and (e == raiz[e] or (ancestrais(e, pai) and ancestrais(e, pai)[-1] == raiz[e]))) for e in ets):
                    fe = True
                else:
                    fe = None
                cont["soldado" if sold else "-"] += 1
                cont["fora_banca"] += fb
                cont[{True: "fora_edital", False: "dentro", None: "indeterminado"}[fe]] += 1
                enun = q.get("enunciado")
                def txt(v):
                    if v is None or isinstance(v, (str, int, float, bool)):
                        return v
                    return json.dumps(v, ensure_ascii=False)
                linhas.append((qid, txt(enun), json.dumps(alts_j, ensure_ascii=False), txt(q.get("gabarito")), n_alt, tipo,
                               txt(q.get("banca")), q.get("banca_id") if str(q.get("banca_id")).isdigit() else None, q.get("banca_sigla"),
                               (q.get("ano") or q.get("prova_ano")) if str(q.get("ano") or q.get("prova_ano") or "").isdigit() else None, txt(q.get("orgao")), txt(q.get("orgao_sigla")), txt(q.get("orgao_uf")), txt(q.get("cargo")),
                               str(q.get("prova") or "")[:200] or None, prova_id.get(str(q.get("prova") or "")[:200]), ets,
                               perc, txt(ia.get("dificuldade") if ia else q.get("dificuldade")),
                               bool(q.get("anulada")), bool(q.get("desatualizada")), bool(q.get("tem_imagem")),
                               (q.get("so_texto") if q.get("so_texto") is not None else bool(enun and not q.get("tem_imagem"))),
                               (bool(q.get("tem_comentario_professor")) if q.get("tem_comentario_professor") is not None else None), txt(q.get("comentario_professor")) or None, sold, fe, fb, q.get("_origem")))
            ex.execute_values(cur, """INSERT INTO mentor.questao (id, enunciado, alternativas, gabarito, n_alt, tipo, banca, banca_id, banca_sigla, ano, orgao,
                              orgao_sigla, orgao_uf, cargo, prova, prova_real_id, etiquetas, indice_acerto, dificuldade, anulada, desatualizada, tem_imagem,
                              so_texto, tem_comentario_professor, comentario_professor, soldado_ba, fora_edital, fora_banca, arquivo_origem) VALUES %s""",
                              linhas, page_size=2000)
            log(f"  questões gravadas: {len(linhas)} | Soldado BA {cont['soldado']} | fora_banca {cont['fora_banca']} | dentro {cont['dentro']} · fora_edital {cont['fora_edital']} · indeterminado {cont['indeterminado']}")

        # ---- vínculo questão ↔ aula
        log("  montando o vínculo questão ↔ aula...")
        cur.execute("DELETE FROM mentor.questao_aula")
        cur.execute("SELECT a.id, a.assunto_id, a.etiquetas, s.materia_id, a.titulo, a.resumo_bolso, a.resumo FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id WHERE a.tipo <> 'fora'")
        aulas_db = cur.fetchall()
        aulas_por_et = defaultdict(list)          # etiqueta -> [(aula_id, assunto_id)]
        mat_da_aula = {}
        for aid, sid, ets, mid, tit, rb, res in aulas_db:
            mat_da_aula[aid] = mid
            for e in ets or []:
                aulas_por_et[e].append((aid, sid))
        # descendentes: etiqueta pai -> aulas cujas etiquetas estão abaixo
        desc = defaultdict(set)
        for e, lst in aulas_por_et.items():
            for c in ancestrais(e, pai):
                if c not in raizes:
                    desc[c].update(lst)
        cur.execute("SELECT id, etiquetas FROM mentor.questao WHERE usavel AND fora_banca = FALSE")
        vinc = []
        sem_vinculo = []
        n_ex = n_pai = 0
        for qid, ets in cur.fetchall():
            pares = {}
            for e in ets or []:
                if e in aulas_por_et:
                    for aid, sid in aulas_por_et[e]:
                        pares[aid] = (sid, "etiqueta", 1.0)
                else:
                    achou = False
                    for c in ancestrais(e, pai):
                        if c in raizes:
                            break
                        if c in aulas_por_et:
                            for aid, sid in aulas_por_et[c]:
                                pares.setdefault(aid, (sid, "pai", 0.8))
                            achou = True
                            break
                    if not achou and e in desc:
                        for aid, sid in desc[e]:
                            pares.setdefault(aid, (sid, "pai", 0.6))
            if pares:
                for aid, (sid, via, f) in pares.items():
                    vinc.append((qid, aid, sid, via, f))
                    n_ex += via == "etiqueta"
                    n_pai += via == "pai"
            else:
                sem_vinculo.append((qid, ets))
        ex.execute_values(cur, "INSERT INTO mentor.questao_aula (questao_id, aula_id, assunto_id, via, forca) VALUES %s", vinc, page_size=5000)
        log(f"  vínculo por etiqueta: {len(vinc)} pares (exata {n_ex}, pai {n_pai}); questões sem vínculo: {len(sem_vinculo)}")

        # ---- semelhança de texto pras que ficaram sem vínculo (só dentro da matéria da raiz)
        if sem_vinculo and limiar > 0:
            log(f"  semelhança de texto pras {len(sem_vinculo)} sem vínculo...")
            cur.execute("SELECT id, nome, etiquetas_raiz FROM mentor.materia")
            mat_por_raiz = {}
            for mid, nome, rz in cur.fetchall():
                for r in rz or []:
                    mat_por_raiz[r] = mid
            # documentos por aula: título + resumo de bolso + resumo (o suficiente, sem a transcrição)
            docs = {}
            for aid, sid, ets, mid, tit, rb, res in aulas_db:
                docs[aid] = (mid, sid, f"{tit} {rb or ''} {(res or '')[:4000]}")
            df = Counter()
            toks = {}
            for aid, (mid, sid, txt) in docs.items():
                t = set(tokens(txt))
                toks[aid] = t
                df.update(t)
            N = len(docs)
            idf = {w: math.log((N + 1) / (n + 1)) + 1 for w, n in df.items()}
            vet = {aid: vetor(docs[aid][2], idf) for aid in docs}
            cur.execute("SELECT id, enunciado, etiquetas FROM mentor.questao WHERE id = ANY(%s)", ([q for q, _ in sem_vinculo],))
            por_mat = defaultdict(list)
            for aid, (mid, sid, _) in docs.items():
                por_mat[mid].append(aid)
            novos = []
            for qid, enun, ets in cur.fetchall():
                mats = {mat_por_raiz.get(raiz.get(e)) for e in (ets or [])} - {None}
                if not mats or not enun:
                    continue
                vq = vetor(enun, idf)
                melhor = (0, None)
                for mid in mats:
                    for aid in por_mat.get(mid, []):
                        s = cosseno(vq, vet[aid])
                        if s > melhor[0]:
                            melhor = (s, aid)
                if melhor[1] and melhor[0] >= limiar:
                    aid = melhor[1]
                    novos.append((qid, aid, docs[aid][1], "semelhanca", round(melhor[0], 3)))
            ex.execute_values(cur, "INSERT INTO mentor.questao_aula (questao_id, aula_id, assunto_id, via, forca) VALUES %s ON CONFLICT DO NOTHING", novos, page_size=5000)
            log(f"  vínculo por semelhança (limiar {limiar}): {len(novos)} questões")

        # ---- resumo
        cur.execute("SELECT s.materia_id, count(DISTINCT qa.questao_id) FROM mentor.questao_aula qa JOIN mentor.assunto s ON s.id=qa.assunto_id GROUP BY 1 ORDER BY 1")
        log("  questões ligadas por matéria: " + ", ".join(f"{m}:{n}" for m, n in cur.fetchall()))
        cur.execute("SELECT count(*) FROM mentor.assunto s WHERE s.tier IN ('S','A') AND NOT EXISTS (SELECT 1 FROM mentor.questao_aula qa WHERE qa.assunto_id=s.id)")
        log(f"  assuntos S/A sem nenhuma questão ligada: {cur.fetchone()[0]}")
        cur.execute("SELECT count(*) FROM mentor.bizu")
        log(f"  bizus do resumo de bolso: {cur.fetchone()[0]}")

        if simular:
            conn.rollback()
            log("  --simular: rollback, nada gravado")
        else:
            conn.commit()
            log("  commit")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrar(dsn, caminho):
    """Aplica o .sql pelo psycopg2 (a VPS é Windows e não tem psql no PATH)."""
    import psycopg2
    sql = Path(caminho).read_text(encoding="utf-8")
    conn = psycopg2.connect(dsn)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='mentor' ORDER BY 1")
                tabs = [r[0] for r in cur.fetchall()]
        log(f"  migration {Path(caminho).name} aplicada: {len(tabs)} tabelas no schema mentor")
        log("  " + ", ".join(tabs))
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="carga do Mike Mentor (schema v3)")
    ap.add_argument("--migrar", default=None, metavar="ARQUIVO_SQL", help="aplica a migration (DERRUBA o schema mentor)")
    ap.add_argument("--dsn", default=DSN_PADRAO)
    ap.add_argument("--dados", default="saida", help="pasta com aulas/ e questoes/")
    ap.add_argument("--complemento", default=None, help="segunda pasta de questões (coleta da VPS)")
    ap.add_argument("--edital", default="edital_verticalizado_v2.json")
    ap.add_argument("--tudo", action="store_true")
    ap.add_argument("--simular", action="store_true", help="faz tudo e dá rollback")
    ap.add_argument("--so-vinculo", action="store_true", dest="so_vinculo", help="refaz só o vínculo questão↔aula")
    ap.add_argument("--limiar", type=float, default=0.25, help="cosseno mínimo da semelhança (0 desliga)")
    args = ap.parse_args()
    if not args.dsn:
        ap.error("DSN vazio: defina MIKEDB_DSN ou passe --dsn")
    log(f"  DSN: ...@{args.dsn.split('@')[-1]}")
    if args.migrar:
        resp = input("  A migration DERRUBA o schema mentor inteiro (estudo gravado se perde). Continuar? [s/N] ")
        if resp.strip().lower() != "s":
            raise SystemExit("  ok, não migrei")
        migrar(args.dsn, args.migrar)
        if not (args.tudo or args.simular):
            return
    if not (args.tudo or args.simular or args.so_vinculo):
        ap.error("use --migrar, --tudo, --simular ou --so-vinculo")
    edital = ler_edital(args.edital)
    aulas = ler_aulas(args.dados)
    pastas = [args.dados] + ([args.complemento] if args.complemento else [])
    qs = ler_questoes(pastas)
    if not aulas or not qs:
        raise SystemExit("sem aulas ou sem questões: confira --dados")
    carregar(args.dsn, edital, aulas, qs, bool(args.complemento), args.simular, args.so_vinculo, args.limiar)


if __name__ == "__main__":
    main()
