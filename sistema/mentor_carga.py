# -*- coding: utf-8 -*-
"""
mentor_carga.py
===============
Carrega os JSONs do gran_extrator.py no schema "mentor" do mikedb.

IDEMPOTENTE: rodar duas vezes nao duplica nada. Tudo e ON CONFLICT DO UPDATE,
com chave natural do Gran (codigo da aula, id da questao). Pode rodar de novo
depois de cada coleta nova.

USO:
    python mentor_carga.py --simular
        Le tudo, valida e mostra o que SERIA carregado. Nao toca no banco.
        Serve pra conferir antes de rodar na VPS.

    python mentor_carga.py --tudo
        Carrega aulas + questoes. Usa MIKEDB_DSN ou --dsn.

    python mentor_carga.py --aulas
    python mentor_carga.py --questoes
    python mentor_carga.py --vincular
        Liga questao -> topico pelos assunto_ids (spec §3.3.1).

ANTES: aplicar a migration 020_mentor_schema.sql no mikedb.
"""

import sys
import io
import os
import re
import json
import glob
import argparse
from pathlib import Path
from collections import Counter
from urllib.parse import urlparse, parse_qs

# line_buffering=True: sem ele o wrapper vira buffer de bloco e a saida
# fica presa ate encher ~8 KB (ver comentario igual no gran_extrator.py)
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)

RAIZ = Path(__file__).parent
DIR_SAIDA = RAIZ / "saida"
DSN_PADRAO = os.environ.get("MIKEDB_DSN", os.environ.get(
    "MENTOR_DSN",
    # Sem a senha no codigo: ela vem do ambiente. O repo e privado, mas senha
    # em git fica no historico pra sempre - se um dia o repo abrir, ou alguem
    # ganhar acesso de leitura, ela vaza junto com tudo que ja foi commitado.
    "postgresql://postgres@localhost:5432/mikedb"))

# Peso de cada disciplina na prova (spec §1). Nao vem do Gran.
PESO_PROVA = {
    "Língua Portuguesa": 10,
    "Matemática": 8,
    "História do Brasil": 8,
    "Geografia do Brasil": 8,
    "Atualidades": 8,
    "Informática": 8,
    "Direito Constitucional": 5,
    "Direitos Humanos": 5,
    "Direito Administrativo": 5,
    "Direito Penal": 5,
    "Igualdade Racial e de Gênero": 5,
    "Direito Penal Militar": 5,
}


def assuntos_genericos(topicos):
    """
    Os assuntos que NAO distinguem topico, medidos em vez de listados.

    O caderno de toda aula inclui o assunto raiz da disciplina, e toda questao
    dela carrega o mesmo id - entao ele casa com tudo e o vinculo vira ruido.
    Criterio: aparecer em mais de um terco dos cadernos da disciplina.
    Mesmo criterio do mentor_tiers.py e do SQL da carga.
    """
    por_disc = {}
    for t in topicos:
        por_disc.setdefault(t.get("disciplina_id") or t.get("disciplina"),
                            []).append(t)
    genericos = set()
    for _, ts in por_disc.items():
        freq = Counter()
        for t in ts:
            freq.update(set(t["assunto_ids"]))
        limite = max(2, len(ts) // 3)
        genericos.update(a for a, n in freq.items() if n > limite)
    return genericos


def achar_pasta_de_dados(padrao):
    """
    Acha sozinho a pasta com aulas/ e questoes/.

    No PC ela se chama 'saida'; na VPS eu montei como 'dados_m' e deixei o
    padrao apontando pra 'saida' - resultado: o comando do LEIA-ME sem
    --saida morre com "pasta nao existe". Procurar os dois nomes custa nada
    e evita o unico erro que essa carga ja deu na mao de quem so quer rodar.
    """
    aqui = Path(__file__).resolve().parent
    candidatos = [padrao, aqui / "dados_m", aqui / "saida",
                  Path.cwd() / "dados_m", Path.cwd() / "saida"]
    for c in candidatos:
        if c and Path(c).exists() and (Path(c) / "aulas").exists():
            return Path(c).resolve()
    return padrao


def assuntos_da_url(url):
    """Tira os ids de assunto da URL do caderno de questoes."""
    if not url:
        return []
    q = parse_qs(urlparse(url).query)
    ids = []
    for chave in ("assunto", "a"):
        for v in q.get(chave, []):
            ids.extend(int(x) for x in str(v).split(",") if x.strip().isdigit())
    return sorted(set(ids))


# Nivel 3 de evidencia do §3.7. Separo os dois porque nao sao a mesma coisa:
# DEPEN e PCDF sao carreira policial, mas soldado de PM estadual e outro bicho
# (conteudo, banca e estilo de prova diferentes). Jogar tudo no mesmo balde
# inflaria o tier A com questao que nao representa a PMBA.
RE_MILITAR_ESTADUAL = re.compile(r'pol[ií]cia militar|corpo de bombeiros|\bPM\s?[A-Z]{2}\b'
                                 r'|\bCBM\s?[A-Z]{2}\b|\bPMDF\b|\bCBMDF\b', re.I)
RE_POLICIAL = re.compile(r'pol[ií]cia|bombeir|\bPM\b|\bPC\b|\bCBM\b|\bPRF\b|\bPF\b'
                         r'|penitenci|DEPEN|agente de seguran|guarda municipal', re.I)


def classificar_carreira(orgao, sigla):
    """Devolve (carreira_policial, militar_estadual)."""
    txt = f"{orgao or ''} {sigla or ''}"
    return bool(RE_POLICIAL.search(txt)), bool(RE_MILITAR_ESTADUAL.search(txt))


def grupo_campos(g):
    """
    Achata o objeto 'grupoQuestao' em colunas.

    Aceita as duas formas: o objeto inteiro (como veio da coleta) ou so o id
    (se algum dia a API mudar ou outro coletor gravar assim).
    """
    if isinstance(g, dict):
        texto = (g.get("enunciado_clean") or g.get("enunciado") or "").strip()
        return {
            "grupo_questao": g.get("id"),
            "grupo_descricao": (g.get("descricao") or "").strip() or None,
            "grupo_enunciado": texto or None,
        }
    return {"grupo_questao": g if isinstance(g, int) else None,
            "grupo_descricao": None, "grupo_enunciado": None}


def peso_da_disciplina(nome):
    n = (nome or "").strip()
    if n in PESO_PROVA:
        return PESO_PROVA[n]
    # "Matemática / Raciocínio Lógico" e afins
    for k, v in PESO_PROVA.items():
        if k.lower() in n.lower() or n.lower() in k.lower():
            return v
    return None


# ---------------------------------------------------------------------------
# Leitura e transformacao (sem banco - da pra validar tudo offline)
# ---------------------------------------------------------------------------

def ler_aulas():
    """Le os JSONs de aula e devolve as linhas de cada tabela."""
    disciplinas, modulos, topicos, aulas, cards, fixacao = {}, {}, [], [], [], []
    problemas = []

    for arq in sorted(glob.glob(str(DIR_SAIDA / "aulas" / "*" / "*.json"))):
        try:
            d = json.load(open(arq, encoding="utf-8"))
        except Exception as e:
            problemas.append(f"{Path(arq).name}: nao consegui ler ({e})")
            continue

        disc_id = d.get("disciplina_id")
        codigo = d.get("codigo_aula")
        if not disc_id or not codigo:
            problemas.append(f"{Path(arq).name}: sem disciplina_id ou codigo_aula")
            continue
        disc_id = int(disc_id)

        disciplinas[disc_id] = {
            "id": disc_id,
            "nome": d.get("disciplina"),
            "peso_prova": peso_da_disciplina(d.get("disciplina")),
            "caderno_questoes_url": None,
        }

        mod = d.get("modulo") or {}
        mod_gran = mod.get("id")
        if mod_gran is not None:
            modulos[(disc_id, int(mod_gran))] = {
                "disciplina_id": disc_id,
                "id_gran": int(mod_gran),
                "ordem": int(mod_gran),
                "titulo": (mod.get("titulo") or "").strip() or f"Modulo {mod_gran}",
            }

        caderno = d.get("caderno_questoes")
        topicos.append({
            "codigo_aula": str(codigo),
            "disciplina_id": disc_id,
            "modulo_gran": int(mod_gran) if mod_gran is not None else None,
            "ordem": d.get("ordem"),
            "titulo": d.get("titulo"),
            "caderno_questoes_url": caderno,
            "assunto_ids": assuntos_da_url(caderno),
        })

        aulas.append({
            "codigo_aula": str(codigo),
            "titulo": d.get("titulo"),
            "ordem_curso": d.get("ordem"),
            "ordem_gran": d.get("ordem_no_curso"),
            "professor": d.get("professor"),
            "duracao": d.get("duracao"),
            "video_id": d.get("video_id"),
            "url_aula": d.get("url_aula"),
            "transcricao": d.get("transcricao"),
            "resumo": d.get("resumo"),
            "resumo_bolso": d.get("resumo_bolso"),
            "mapa_mental": d.get("mapa_mental"),
            # Caminho RELATIVO a saida/, com barra normal. O JSON e gerado no
            # Windows mas a carga roda na VPS (Linux), em outro diretorio:
            # guardar "saida\aulas\..." quebraria dos dois lados. A API
            # prefixa a propria base na hora de servir o arquivo.
            "pdf_path": (Path(arq).with_suffix(".pdf").relative_to(DIR_SAIDA).as_posix()
                         if d.get("pdf_degravacao") else None),
            "coletado_em": d.get("coletado_em"),
        })

        # flashcards vem agrupados em decks; a tabela e plana, com o deck
        # guardado como campo (spec §3.2)
        for deck in (d.get("flashcards") or []):
            for c in (deck.get("cards") or []):
                if not c.get("front") or not c.get("back"):
                    continue
                cards.append({
                    "codigo_aula": str(codigo),
                    "deck": deck.get("title"),
                    "frente": c.get("front"),
                    "verso": c.get("back"),
                    "dica": c.get("hint"),
                    "id_gran": c.get("id"),
                })

        for q in (d.get("questoes_fixacao") or []):
            alts = q.get("alternatives") or []
            certa = next((a for a in alts if a.get("correct")), None)
            fixacao.append({
                "codigo_aula": str(codigo),
                "id_gran": q.get("id"),
                "enunciado": q.get("question"),
                "alternativas": [{"letra": (a.get("id") or "").upper(),
                                  "texto": a.get("text"),
                                  "correta": bool(a.get("correct"))} for a in alts],
                "gabarito": (certa.get("id") or "").upper() if certa else None,
                "explicacao_por_alternativa": {
                    (a.get("id") or "").upper(): a.get("explanation") for a in alts
                    if a.get("explanation")},
            })

    return {
        "disciplinas": list(disciplinas.values()),
        "modulos": list(modulos.values()),
        "topicos": topicos,
        "aulas": aulas,
        "cards": cards,
        "fixacao": fixacao,
        "problemas": problemas,
    }


def ler_questoes():
    """Le os JSONs de questoes. Aguenta o formato antigo e o novo."""
    por_id, problemas = {}, []
    for arq in sorted(glob.glob(str(DIR_SAIDA / "questoes" / "*" / "*.json"))):
        try:
            d = json.load(open(arq, encoding="utf-8"))
        except Exception as e:
            problemas.append(f"{Path(arq).name}: nao consegui ler ({e})")
            continue

        for q in (d.get("questoes") or []):
            qid = q.get("id")
            if not qid:
                continue

            # 'assuntos' mudou de formato no meio do caminho:
            #   antigo: ["Direito Penal", "Crimes Contra o Patrimonio"]
            #   novo:   [{"id":406245,"nome":"...","raiz":...,"pai":...}]
            brutos = q.get("assuntos") or []
            if brutos and isinstance(brutos[0], str):
                assuntos = [{"id": None, "nome": a} for a in brutos]
            else:
                assuntos = brutos
            ids = [a["id"] for a in assuntos if isinstance(a, dict) and a.get("id")]
            if not ids:
                ids = q.get("assunto_ids") or []

            enun = (q.get("enunciado") or "").strip()
            ia = q.get("indice_acerto") or {}
            com = q.get("comentario_professor") or {}

            por_id[qid] = {
                "id": qid,
                "enunciado": enun or None,
                "alternativas": q.get("alternativas") or [],
                "gabarito": q.get("gabarito"),
                "comentario": com.get("texto"),
                "banca": q.get("banca"),
                "banca_id": q.get("banca_id"),
                "banca_sigla": q.get("banca_sigla"),
                "ano": q.get("ano"),
                "orgao": q.get("orgao"),
                "orgao_id": q.get("orgao_id"),
                "orgao_sigla": q.get("orgao_sigla"),
                "orgao_uf": q.get("orgao_uf"),
                "prova": q.get("prova"),
                "cargo": q.get("cargo"),
                "cargo_id": q.get("cargo_id"),
                "carreira_policial": classificar_carreira(
                    q.get("orgao"), q.get("orgao_sigla"))[0],
                "militar_estadual": classificar_carreira(
                    q.get("orgao"), q.get("orgao_sigla"))[1],
                "indice_acerto_gran": ia.get("percentual"),
                "acertos_gran": ia.get("acertos"),
                "total_gran": ia.get("total"),
                "dificuldade_gran": q.get("dificuldade"),
                "desatualizada": bool(q.get("desatualizada")),
                "anulada": bool(q.get("anulada")),
                "assuntos": assuntos,
                "assunto_ids": sorted(set(ids)),
                # nos arquivos antigos esses campos nao existem: derivo do
                # que da pra saber, que e ter enunciado ou nao
                "so_texto": bool(q.get("so_texto", bool(enun))),
                "tem_imagem": q.get("tem_imagem"),
                # 'grupoQuestao' da API e um OBJETO, nao um id - foi o que
                # derrubou a carga com "can't adapt type 'dict'". E dentro
                # dele vem o enunciado COMPARTILHADO das questoes do grupo,
                # que sem isso se perdia.
                **grupo_campos(q.get("grupo_questao")),
                "coletado_em": d.get("coletado_em"),
            }

    return {"questoes": list(por_id.values()), "problemas": problemas}


# ---------------------------------------------------------------------------
# Relatorio (o modo --simular)
# ---------------------------------------------------------------------------

def relatar(a, q):
    print("=" * 70)
    print("  O QUE SERIA CARREGADO")
    print("=" * 70)
    print(f"  disciplina        : {len(a['disciplinas'])}")
    for d in a["disciplinas"]:
        print(f"      {d['id']:>6}  {d['nome']}  (peso {d['peso_prova']})")
    print(f"  modulo            : {len(a['modulos'])}")
    print(f"  topico (= aula)   : {len(a['topicos'])}")
    print(f"  aula (conteudo)   : {len(a['aulas'])}")
    print(f"  card              : {len(a['cards'])}")
    print(f"  questao_fixacao   : {len(a['fixacao'])}")
    print(f"  questao           : {len(q['questoes'])}")

    print("\n  --- qualidade ---")
    sem_pdf = sum(1 for x in a["aulas"] if not x["pdf_path"])
    sem_tr = sum(1 for x in a["aulas"] if not x["transcricao"])
    print(f"  aulas sem PDF        : {sem_pdf}")
    print(f"  aulas sem transcricao: {sem_tr}")

    qs = q["questoes"]
    sem_enun = [x["id"] for x in qs if not x["enunciado"]]
    sem_gab = [x["id"] for x in qs if not x["gabarito"]]
    com_assunto = sum(1 for x in qs if x["assunto_ids"])
    usaveis = sum(1 for x in qs
                  if x["so_texto"] and x["enunciado"] and x["gabarito"]
                  and not x["desatualizada"] and not x["anulada"])
    print(f"  questoes sem enunciado: {len(sem_enun)} {sem_enun[:6]}")
    print(f"  questoes sem gabarito : {len(sem_gab)}")
    print(f"  questoes COM assunto_ids: {com_assunto}/{len(qs)}")
    print(f"  questoes usaveis (v_questao_usavel): {usaveis}/{len(qs)}")

    print("\n  --- vinculo questao -> topico (spec 3.3.1) ---")
    # Tem que usar a MESMA regra do vincular_no_banco, senao o simulado promete
    # um numero e a carga grava outro - foi assim que o bug do assunto raiz
    # apareceu (simulado 6.894, banco 6.261) e quase passou batido.
    genericos = assuntos_genericos(a["topicos"])
    if genericos:
        print(f"  assuntos genericos fora do vinculo: {sorted(genericos)}")
    mapa = {}
    for t in a["topicos"]:
        for aid in set(t["assunto_ids"]) - genericos:
            mapa.setdefault(aid, []).append(t["codigo_aula"])
    print(f"  assuntos que distinguem topico: {len(mapa)}")
    if com_assunto:
        casam = vinculos = 0
        for x in qs:
            ts = {c for i in x["assunto_ids"] for c in mapa.get(i, [])}
            if ts:
                casam += 1
                vinculos += len(ts)
        print(f"  questoes que casam com algum topico: {casam}/{len(qs)}")
        print(f"  vinculos no total: {vinculos} "
              f"(media {vinculos / max(1, casam):.1f} topicos por questao)")
    else:
        print("  [!] NENHUMA questao tem assunto_ids - o vinculo fica NULO.")
        print("      Nao impede a carga; e um UPDATE depois. Ver MIKE_MENTOR_STATUS.md")

    for p in (a["problemas"] + q["problemas"]):
        print(f"  [!] {p}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Carga no Postgres
# ---------------------------------------------------------------------------

def carregar(dsn, a, q, fazer_aulas, fazer_questoes, fazer_vinculo):
    import psycopg2.extras as ex

    conn = conectar(dsn)
    conn.autocommit = False
    cur = conn.cursor()
    try:
        cur.execute("SET search_path TO mentor, public")
        conferir_tabelas(cur, fazer_vinculo)

        if fazer_aulas:
            print("  disciplinas...")
            ex.execute_batch(cur, """
                INSERT INTO mentor.disciplina (id, nome, peso_prova)
                VALUES (%(id)s, %(nome)s, %(peso_prova)s)
                ON CONFLICT (id) DO UPDATE SET
                    nome = EXCLUDED.nome,
                    peso_prova = COALESCE(EXCLUDED.peso_prova, mentor.disciplina.peso_prova)
            """, a["disciplinas"])

            print("  modulos...")
            ex.execute_batch(cur, """
                INSERT INTO mentor.modulo (disciplina_id, id_gran, ordem, titulo)
                VALUES (%(disciplina_id)s, %(id_gran)s, %(ordem)s, %(titulo)s)
                ON CONFLICT (disciplina_id, id_gran) DO UPDATE SET
                    titulo = EXCLUDED.titulo, ordem = EXCLUDED.ordem
            """, a["modulos"])

            print("  topicos...")
            for t in a["topicos"]:
                cur.execute("""
                    INSERT INTO mentor.topico
                        (disciplina_id, modulo_id, ordem, titulo,
                         caderno_questoes_url, assunto_ids, codigo_aula)
                    VALUES (%(disciplina_id)s,
                            (SELECT id FROM mentor.modulo
                              WHERE disciplina_id = %(disciplina_id)s
                                AND id_gran = %(modulo_gran)s),
                            %(ordem)s, %(titulo)s, %(caderno_questoes_url)s,
                            %(assunto_ids)s, %(codigo_aula)s)
                    ON CONFLICT (codigo_aula) DO UPDATE SET
                        modulo_id = EXCLUDED.modulo_id,
                        ordem = EXCLUDED.ordem,
                        titulo = EXCLUDED.titulo,
                        caderno_questoes_url = EXCLUDED.caderno_questoes_url,
                        assunto_ids = EXCLUDED.assunto_ids
                """, t)

            print("  aulas...")
            for x in a["aulas"]:
                cur.execute("""
                    INSERT INTO mentor.aula
                        (topico_id, titulo, ordem_curso, ordem_gran, professor,
                         duracao, video_id, url_aula, transcricao, resumo,
                         resumo_bolso, mapa_mental, pdf_path, coletado_em)
                    VALUES ((SELECT id FROM mentor.topico WHERE codigo_aula = %(codigo_aula)s),
                            %(titulo)s, %(ordem_curso)s, %(ordem_gran)s, %(professor)s,
                            %(duracao)s, %(video_id)s, %(url_aula)s, %(transcricao)s,
                            %(resumo)s, %(resumo_bolso)s, %(mapa_mental)s, %(pdf_path)s,
                            %(coletado_em)s)
                    ON CONFLICT (topico_id) DO UPDATE SET
                        titulo = EXCLUDED.titulo,
                        transcricao = EXCLUDED.transcricao,
                        resumo = EXCLUDED.resumo,
                        resumo_bolso = EXCLUDED.resumo_bolso,
                        mapa_mental = EXCLUDED.mapa_mental,
                        pdf_path = EXCLUDED.pdf_path,
                        professor = EXCLUDED.professor,
                        duracao = EXCLUDED.duracao,
                        coletado_em = EXCLUDED.coletado_em,
                        atualizado_em = now()
                """, x)

            print("  cards...")
            for c in a["cards"]:
                cur.execute("""
                    INSERT INTO mentor.card (topico_id, deck, frente, verso, dica, id_gran)
                    VALUES ((SELECT id FROM mentor.topico WHERE codigo_aula = %(codigo_aula)s),
                            %(deck)s, %(frente)s, %(verso)s, %(dica)s, %(id_gran)s)
                    ON CONFLICT (topico_id, id_gran) DO UPDATE SET
                        deck = EXCLUDED.deck, frente = EXCLUDED.frente,
                        verso = EXCLUDED.verso, dica = EXCLUDED.dica
                """, c)

            print("  questoes de fixacao...")
            for f in a["fixacao"]:
                cur.execute("""
                    INSERT INTO mentor.questao_fixacao
                        (topico_id, id_gran, enunciado, alternativas, gabarito,
                         explicacao_por_alternativa)
                    VALUES ((SELECT id FROM mentor.topico WHERE codigo_aula = %(codigo_aula)s),
                            %(id_gran)s, %(enunciado)s, %(alternativas)s, %(gabarito)s,
                            %(explicacao)s)
                    ON CONFLICT (topico_id, id_gran) DO UPDATE SET
                        enunciado = EXCLUDED.enunciado,
                        alternativas = EXCLUDED.alternativas,
                        gabarito = EXCLUDED.gabarito,
                        explicacao_por_alternativa = EXCLUDED.explicacao_por_alternativa
                """, {**f,
                      "alternativas": json.dumps(f["alternativas"], ensure_ascii=False),
                      "explicacao": json.dumps(f["explicacao_por_alternativa"],
                                               ensure_ascii=False)})

        if fazer_questoes:
            print(f"  questoes ({len(q['questoes'])})...")
            ex.execute_batch(cur, """
                INSERT INTO mentor.questao
                    (id, enunciado, alternativas, gabarito, comentario, banca, banca_id,
                     banca_sigla, ano, orgao, orgao_id, orgao_sigla, orgao_uf, prova,
                     cargo, cargo_id, carreira_policial, militar_estadual,
                     indice_acerto_gran, acertos_gran, total_gran,
                     dificuldade_gran, desatualizada, anulada, assuntos, assunto_ids,
                     so_texto, tem_imagem, grupo_questao, grupo_descricao,
                     grupo_enunciado, coletado_em)
                VALUES
                    (%(id)s, %(enunciado)s, %(alternativas)s, %(gabarito)s, %(comentario)s,
                     %(banca)s, %(banca_id)s, %(banca_sigla)s, %(ano)s, %(orgao)s,
                     %(orgao_id)s, %(orgao_sigla)s, %(orgao_uf)s, %(prova)s,
                     %(cargo)s, %(cargo_id)s, %(carreira_policial)s, %(militar_estadual)s,
                     %(indice_acerto_gran)s, %(acertos_gran)s, %(total_gran)s,
                     %(dificuldade_gran)s, %(desatualizada)s, %(anulada)s, %(assuntos)s,
                     %(assunto_ids)s, %(so_texto)s, %(tem_imagem)s, %(grupo_questao)s,
                     %(grupo_descricao)s, %(grupo_enunciado)s, %(coletado_em)s)
                ON CONFLICT (id) DO UPDATE SET
                    enunciado = EXCLUDED.enunciado,
                    alternativas = EXCLUDED.alternativas,
                    gabarito = EXCLUDED.gabarito,
                    -- comentario e buscado sob demanda (§3.3.1b): so sobrescreve
                    -- se veio um novo, nunca apaga o que ja foi guardado
                    comentario = COALESCE(EXCLUDED.comentario, mentor.questao.comentario),
                    indice_acerto_gran = EXCLUDED.indice_acerto_gran,
                    acertos_gran = EXCLUDED.acertos_gran,
                    total_gran = EXCLUDED.total_gran,
                    assuntos = COALESCE(EXCLUDED.assuntos, mentor.questao.assuntos),
                    assunto_ids = CASE
                        WHEN COALESCE(array_length(EXCLUDED.assunto_ids, 1), 0) > 0
                        THEN EXCLUDED.assunto_ids ELSE mentor.questao.assunto_ids END,
                    so_texto = EXCLUDED.so_texto,
                    grupo_questao = COALESCE(EXCLUDED.grupo_questao, mentor.questao.grupo_questao),
                    grupo_descricao = COALESCE(EXCLUDED.grupo_descricao, mentor.questao.grupo_descricao),
                    grupo_enunciado = COALESCE(EXCLUDED.grupo_enunciado, mentor.questao.grupo_enunciado),
                    orgao_id = COALESCE(EXCLUDED.orgao_id, mentor.questao.orgao_id),
                    banca_id = COALESCE(EXCLUDED.banca_id, mentor.questao.banca_id),
                    carreira_policial = EXCLUDED.carreira_policial,
                    militar_estadual = EXCLUDED.militar_estadual,
                    atualizado_em = now()
            """, [{**x,
                   "alternativas": json.dumps(x["alternativas"], ensure_ascii=False),
                   "assuntos": json.dumps(x["assuntos"], ensure_ascii=False)}
                  for x in q["questoes"]])

        if fazer_vinculo:
            vincular_no_banco(cur)

        conn.commit()
        print("\n  commit feito.")

        for tabela in ["disciplina", "modulo", "topico", "aula", "card",
                       "questao_fixacao", "questao"]:
            cur.execute(f"SELECT count(*) FROM mentor.{tabela}")
            print(f"     mentor.{tabela:18} {cur.fetchone()[0]}")
    except FaltaMigration as e:
        # nao e falha de dado nem bug: e passo que falta. Traceback aqui so
        # esconde a instrucao que resolve.
        conn.rollback()
        print("\n" + "!" * 70)
        print(f"  PAREI: {e}")
        print("!" * 70 + "\n")
        sys.exit(2)
    except Exception:
        conn.rollback()
        print("\n  [ERRO] rollback - nada foi gravado.")
        raise
    finally:
        cur.close()
        conn.close()


class FaltaMigration(Exception):
    """O banco esta atras das migrations. Nao e erro de dado."""


def conferir_tabelas(cur, fazer_vinculo):
    """
    Confere ANTES de trabalhar se o banco tem as tabelas que a carga usa.

    Sem isto, esquecer o --migrar custava 8.890 questoes processadas, um
    rollback e um traceback de psycopg2 no fim - trabalho jogado fora e uma
    mensagem que nao diz o que fazer. A conferencia e uma consulta so.
    """
    precisa = ["disciplina", "modulo", "topico", "aula", "card",
               "questao_fixacao", "questao"]
    if fazer_vinculo:
        precisa += ["questao_topico", "assunto_generico"]
    cur.execute("""
        SELECT table_name FROM information_schema.tables
         WHERE table_schema = 'mentor' AND table_name = ANY(%s)
    """, (precisa,))
    tem = {r[0] for r in cur.fetchall()}
    faltam = [t for t in precisa if t not in tem]
    if not faltam:
        return
    raise FaltaMigration(
        "o banco esta sem " + ", ".join(f"mentor.{t}" for t in faltam) + ".\n"
        "     Rode as migrations primeiro - nada foi gravado:\n\n"
        "         python mentor_carga.py --migrar\n"
        "         python mentor_carga.py --tudo")


def vincular_no_banco(cur):
    """
    Liga questao a topico pelos assuntos ESPECIFICOS, gravando todos os
    vinculos e elegendo um principal por regra.

    A versao antiga era um UPDATE com `q.assunto_ids && t.assunto_ids`. Parecia
    certa e nao era, por dois motivos que so aparecem quando se olha o dado:

      1. O caderno de toda aula lista tambem o assunto RAIZ da disciplina, e
         toda questao dela carrega o mesmo id - entao o && dava verdadeiro pra
         quase todo par. 78% das questoes "casavam" com varios topicos, uma
         com 53. As 6.894 ligadas eram ruido; com a raiz fora sao 3.017.
      2. Mesmo sem a raiz, 17% pertencem de verdade a mais de um topico (a
         mesma questao de furto serve as 4 aulas de Furto). Uma coluna so nao
         cabe isso, e o banco escolhia UM ao acaso - a contagem de cada aula
         virava sorteio, e sorteio que muda a cada carga.

    O criterio de generico e medido, nao listado na mao: assunto presente em
    mais de um terco dos cadernos da disciplina nao distingue nada. Mesmo
    criterio do mentor_tiers.py, agora num lugar so.
    """
    print("  marcando assuntos genericos (nao distinguem topico)...")
    cur.execute("""
        WITH cad AS (
            SELECT disciplina_id, id, unnest(assunto_ids) AS assunto_id
              FROM mentor.topico
             WHERE assunto_ids IS NOT NULL
        ),
        tot AS (
            SELECT disciplina_id, count(DISTINCT id) AS n_cadernos
              FROM cad GROUP BY disciplina_id
        ),
        freq AS (
            SELECT c.disciplina_id, c.assunto_id,
                   count(DISTINCT c.id) AS n, t.n_cadernos
              FROM cad c JOIN tot t USING (disciplina_id)
             GROUP BY c.disciplina_id, c.assunto_id, t.n_cadernos
        )
        INSERT INTO mentor.assunto_generico
               (assunto_id, disciplina_id, cadernos, cadernos_total)
        SELECT assunto_id, disciplina_id, n, n_cadernos
          FROM freq
         WHERE n > GREATEST(2, n_cadernos / 3)
        ON CONFLICT (assunto_id) DO UPDATE
           SET cadernos = EXCLUDED.cadernos,
               cadernos_total = EXCLUDED.cadernos_total
    """)
    cur.execute("SELECT count(*) FROM mentor.assunto_generico")
    print(f"     {cur.fetchone()[0]} assunto(s) generico(s) fora do vinculo")

    print("  vinculando questao -> topico (todos os vinculos)...")
    # Recomeca do zero: o vinculo e derivado, nao conquistado. Manter o antigo
    # so guardaria o erro de uma carga passada.
    cur.execute("TRUNCATE mentor.questao_topico")
    cur.execute("""
        INSERT INTO mentor.questao_topico (questao_id, topico_id, forca)
        SELECT q.id, t.id, cardinality(ARRAY(
                   SELECT unnest(q.assunto_ids)
                   INTERSECT SELECT unnest(t.assunto_ids)
                   EXCEPT SELECT assunto_id FROM mentor.assunto_generico))
          FROM mentor.questao q
          JOIN mentor.topico  t
            ON q.assunto_ids && t.assunto_ids
         WHERE EXISTS (
                   SELECT 1 FROM unnest(q.assunto_ids) a
                    WHERE a = ANY(t.assunto_ids)
                      AND a NOT IN (SELECT assunto_id FROM mentor.assunto_generico))
        ON CONFLICT DO NOTHING
    """)
    print(f"     {cur.rowcount} vinculo(s) gravado(s)")

    # O principal: maior forca; empate decide pelo topico de menor ordem, que
    # e o primeiro da trilha do curso. Deterministico - duas cargas dao o
    # mesmo resultado, que era o que faltava.
    print("  elegendo o topico principal de cada questao...")
    cur.execute("""
        WITH escolhido AS (
            SELECT DISTINCT ON (qt.questao_id) qt.questao_id, qt.topico_id
              FROM mentor.questao_topico qt
              JOIN mentor.topico t ON t.id = qt.topico_id
             ORDER BY qt.questao_id, qt.forca DESC,
                      COALESCE(t.ordem, 999999), t.id
        )
        UPDATE mentor.questao_topico qt
           SET principal = TRUE
          FROM escolhido e
         WHERE qt.questao_id = e.questao_id AND qt.topico_id = e.topico_id
    """)
    cur.execute("""
        UPDATE mentor.questao q
           SET topico_id = qt.topico_id, confianca_classif = 1.0
          FROM mentor.questao_topico qt
         WHERE qt.questao_id = q.id AND qt.principal
    """)
    print(f"     {cur.rowcount} questao(oes) com topico principal")

    # o vinculo antigo (pelo assunto raiz) continuaria apontando pra um topico
    # sorteado; limpar e melhor que deixar dado errado parecendo certo
    cur.execute("""
        UPDATE mentor.questao q SET topico_id = NULL, confianca_classif = NULL
         WHERE q.topico_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM mentor.questao_topico qt
                            WHERE qt.questao_id = q.id AND qt.principal)
    """)
    if cur.rowcount:
        print(f"     {cur.rowcount} vinculo(s) antigo(s) invalidado(s) e limpo(s)")


def conectar(dsn):
    """Conecta, traduzindo o erro cru do driver pra algo acionavel."""
    try:
        import psycopg2
    except ImportError:
        print("\n  [ERRO] falta o driver do Postgres. Rode:")
        print("         pip install psycopg2-binary\n")
        sys.exit(1)
    try:
        return psycopg2.connect(dsn)
    except Exception as e:
        alvo = dsn.split("@")[-1]
        msg = str(e).lower()
        print(f"\n  [ERRO] nao consegui conectar em {alvo}")
        if "refused" in msg or "could not connect" in msg:
            print("         O Postgres nao respondeu. Ele esta rodando nesta maquina?")
            print("         Se estiver em outro host, passe --dsn com o endereco certo.")
        elif "authentication" in msg or "password" in msg:
            print("         Usuario ou senha errados no DSN.")
        elif "does not exist" in msg:
            print("         O banco nao existe com esse nome.")
        else:
            print(f"         {str(e).strip().splitlines()[0]}")
        print("\n         O DSN vem de MIKEDB_DSN ou de --dsn.")
        print('         Ex: --dsn "postgresql://postgres:SENHA@localhost:5432/mikedb"\n')
        sys.exit(1)


def aplicar_migrations(dsn, caminho):
    """
    Aplica TODAS as migrations do mentor, em ordem de nome.

    Antes isto aplicava um arquivo so, com o nome no padrao do argumento.
    Quando entrou a 021 o padrao continuou apontando pra 020 - o tipo de
    detalhe que so aparece quando o banco de alguem fica sem a tabela nova.
    Aplicar a pasta inteira e idempotente do mesmo jeito (tudo IF NOT EXISTS)
    e nao depende de eu lembrar de mexer aqui na proxima.
    """
    alvo = Path(caminho)
    if alvo.is_dir():
        arquivos = sorted(alvo.glob("*_mentor_*.sql"))
    elif alvo.exists():
        arquivos = [alvo]
    else:
        arquivos = sorted(Path("migrations").glob("*_mentor_*.sql"))
    if not arquivos:
        print(f"  [ERRO] nao achei migration nenhuma em: {alvo.resolve()}")
        print("         rode de dentro do tipmike_api, ou passe o caminho:")
        print("         python mentor_carga.py --migrar caminho/para/migrations")
        return False
    print(f"  {len(arquivos)} migration(s): "
          f"{', '.join(a.name for a in arquivos)}")
    for a in arquivos:
        if not aplicar_migration(dsn, a):
            return False
    return True


def aplicar_migration(dsn, caminho):
    """
    Aplica um .sql sem precisar do psql.

    Na VPS (Windows) o psql nao esta no PATH, e instalar client so pra isso
    e trabalho a toa: o psycopg2 ja esta ai para a carga e da conta. O arquivo
    e idempotente (tudo IF NOT EXISTS), entao rodar de novo nao quebra.
    """
    arq = Path(caminho)
    if not arq.exists():
        print(f"  [ERRO] nao achei o arquivo: {arq.resolve()}")
        return False
    sql = arq.read_text(encoding="utf-8")
    print(f"  Aplicando {arq.name} ({len(sql)} chars)...")

    conn = conectar(dsn)
    conn.autocommit = False
    cur = conn.cursor()
    try:
        cur.execute(sql)
        conn.commit()
        # information_schema.tables mistura TABELA e VIEW - separo, senao o
        # contador diz "22 tabelas" quando sao 21 + a v_questao_usavel.
        cur.execute("""
            SELECT table_name, table_type FROM information_schema.tables
             WHERE table_schema = 'mentor' ORDER BY table_name
        """)
        linhas = cur.fetchall()
        tabelas = [n for n, t in linhas if t == "BASE TABLE"]
        views = [n for n, t in linhas if t == "VIEW"]
        print(f"  OK. {len(tabelas)} tabelas e {len(views)} view(s) no schema mentor:")
        for i in range(0, len(tabelas), 4):
            print("     " + "  ".join(f"{t:<18}" for t in tabelas[i:i + 4]))
        if views:
            print("     view(s): " + ", ".join(views))
        return True
    except Exception as e:
        conn.rollback()
        print(f"\n  [ERRO] rollback, nada foi criado:\n     {e}")
        return False
    finally:
        cur.close()
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="Carrega os JSONs do Gran no schema mentor.")
    ap.add_argument("--migrar", nargs="?", const="migrations",
                    metavar="ARQUIVO_OU_PASTA",
                    help="cria/atualiza as tabelas rodando os .sql (dispensa o "
                         "psql). Padrao: todas as migrations do mentor, em ordem")
    ap.add_argument("--simular", action="store_true",
                    help="so le e valida, sem tocar no banco")
    ap.add_argument("--tudo", action="store_true", help="aulas + questoes + vinculo")
    ap.add_argument("--aulas", action="store_true")
    ap.add_argument("--questoes", action="store_true")
    ap.add_argument("--vincular", action="store_true",
                    help="liga questao->topico pelos assunto_ids")
    ap.add_argument("--dsn", default=DSN_PADRAO)
    ap.add_argument("--saida", default=None, metavar="PASTA",
                    help="onde estao as pastas aulas/ e questoes/ "
                         "(padrao: ./saida ao lado deste script)")
    args = ap.parse_args()

    global DIR_SAIDA
    if args.saida:
        DIR_SAIDA = Path(args.saida).expanduser().resolve()
    else:
        DIR_SAIDA = achar_pasta_de_dados(DIR_SAIDA)

    print("=" * 70)
    print("  MIKE MENTOR - carga")
    print("=" * 70)

    if args.migrar:
        alvo = args.dsn.split("@")[-1]
        print(f"  Banco: {alvo}")
        if not aplicar_migrations(args.dsn, args.migrar):
            sys.exit(1)
        if not (args.tudo or args.aulas or args.questoes or args.vincular or args.simular):
            print()
            return
        print()

    print(f"  Dados em: {DIR_SAIDA}")
    if not DIR_SAIDA.exists():
        print(f"\n  [ERRO] pasta nao existe: {DIR_SAIDA}")
        print("         use --saida pra apontar onde estao aulas/ e questoes/\n")
        sys.exit(1)

    a = ler_aulas()
    q = ler_questoes()
    relatar(a, q)

    if args.simular:
        print("\n  (--simular: nada foi gravado)\n")
        return
    if not (args.tudo or args.aulas or args.questoes or args.vincular):
        print("\n  Escolha: --simular, --tudo, --aulas, --questoes ou --vincular\n")
        return

    alvo = args.dsn.split("@")[-1]
    print(f"\n  Banco: {alvo}")
    carregar(args.dsn, a, q,
             fazer_aulas=args.tudo or args.aulas,
             fazer_questoes=args.tudo or args.questoes,
             fazer_vinculo=args.tudo or args.vincular)
    print()


if __name__ == "__main__":
    main()
