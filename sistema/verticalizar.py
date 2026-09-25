# -*- coding: utf-8 -*-
"""
verticalizar.py
===============
Gera o `edital_verticalizado.yaml` da spec §3.0.1 a partir dos JSONs do coletor.

A ESTRUTURA NAO E INFERIDA EM TEMPO DE EXECUCAO. Ela vive num arquivo, gerado
uma vez, revisado e APROVADO pelo usuario, versionado. A carga do banco le so
esse arquivo. Mudou a estrutura? Edita o arquivo - nunca o codigo.

OS QUATRO NIVEIS (§3.0), cada palavra com UM significado:
    MATERIA   Direito Penal                 <- disciplina da prova (13)
     MODULO     Teoria do Crime             <- item do edital (o "topico" do Gran)
      ASSUNTO     Crimes Culposos           <- o conceito
       AULA         Crimes Culposos II      <- 1 video = 1 PARTE do assunto

REGRA DE OURO: "Crimes Culposos I" e "Crimes Culposos II" NAO sao dois
assuntos. Sao duas AULAS do mesmo assunto. O numeral no fim do titulo
(I, II, 1, 2, "Parte 1") e a PARTE, nunca o assunto.

ETIQUETA != ASSUNTO: os rotulos que o Gran Questoes chama de "assunto"
(`assunto[]` do caderno) aqui se chamam ETIQUETA. Sao so a ponte tecnica
entre aula e questao.

O script NAO decide caso ambiguo - manda pra lista de revisao.

USO:
    python verticalizar.py                          gera e mostra o resumo
    python verticalizar.py --mostrar "Direito Penal"   imprime a arvore
    python verticalizar.py --revisao                so a lista de revisao
    python verticalizar.py --validar                so as validacoes
"""

import sys
import io
import re
import json
import glob
import argparse
import unicodedata
from pathlib import Path
from collections import defaultdict

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)

RAIZ = Path(__file__).parent
DIR_SAIDA = RAIZ / "saida"
ARQUIVO = RAIZ / "edital_verticalizado.yaml"

# ids fixos das materias (spec §3.2, tabela do §1)
MATERIA_ID = {
    "Língua Portuguesa": 1, "Matemática": 2, "História do Brasil": 3,
    "Geografia do Brasil": 4, "Atualidades": 5, "Informática": 6,
    "Direito Constitucional": 7, "Direitos Humanos": 8,
    "Direito Administrativo": 9, "Direito Penal": 10,
    "Igualdade Racial e de Gênero": 11, "Direito Penal Militar": 12,
    "Conhecimentos da Bahia": 13,
}

# ids do Gran que a SPEC ja informa (§3.2: "Direito Penal: 574 / 3392").
# O coletor nao guardou o id da MATERIA - so o da disciplina no curso. Em vez
# de perguntar ao dono o que ja esta escrito na spec, leio daqui.
GRAN_MATERIA_ID = {"Direito Penal": 574}

ROMANOS = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5,
           "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}

# numeracao no COMECO do titulo: "3 - ", "12. "
RE_PREFIXO_NUM = re.compile(r"^\s*\d{1,2}\s*[-.)]\s*")
# numeral no FIM: romano, arabico 1-20 ou "Parte N", com ou sem traco antes.
#
# Os dois olhares pra tras NAO sao enfeite. Sem `(?<!\d)`, "Art. 168" virava
# base "Art. 1" + parte 68 - o regex mordia so os dois ultimos digitos de um
# numero de artigo. Sem `(?<![A-Za-z])`, "Da Deserção" podia perder letras
# para o grupo de romanos. O validador pegou o primeiro caso; o segundo ficou
# de licao.
RE_PARTE_FIM = re.compile(
    r"\s*[-–—]?\s*(?:parte\s*)?(?<!\d)(\d{1,2}|(?<![A-Za-z])[IVX]{1,5})\s*$",
    re.I)

# O tipo da aula e um ROTULO, e rotulo vem no comeco ou depois de um travessao.
# Nao basta procurar a palavra solta: existe a aula "... e Do Exercício de
# Comércio", que e CONTEUDO (o crime de exercer comercio), e um `exerc\w*`
# cru a classificaria como lista de exercicios. Exigir a posicao separa as
# duas sem precisar de lista de excecao.
#
# `exerc[íi]c` (e nao `exerc[íi]cio`) e de proposito: o Gran tem "Exercícos II"
# com typo, e por causa dele duas aulas do mesmo assunto foram parar em
# lugares diferentes. O prefixo curto cobre exercício/exercicios/exercícos.
_SEG = r"(?:^|[-–—]\s*)"
TIPOS = [
    ("apresentacao", re.compile(_SEG + r"apresenta[çc][ãa]o", re.I)),
    ("exercicios", re.compile(
        _SEG + r"(?:exerc[íi]c\w*|quest[õo]es comentadas|"
               r"resolu[çc][ãa]o de quest\w*|quest[õo]es de consolida\w*)", re.I)),
    ("revisao", re.compile(_SEG + r"(?:revis[ãa]o|revisa[çc]o)", re.I)),
]


def sem_acento(t):
    return "".join(c for c in unicodedata.normalize("NFD", t or "")
                   if unicodedata.category(c) != "Mn")


def tipo_da_aula(titulo):
    for nome, regex in TIPOS:
        if regex.search(titulo or ""):
            return nome
    return "conteudo"


def separar_parte(titulo):
    """
    Devolve (nome_base, parte, ambiguo_por_que).

    Ambiguo quando o numeral no fim pode nao ser parte - e ai o script NAO
    decide: "Titulo II da CF" e "Art. 5o, XI" tem numeral que e conteudo.
    Heuristica: se o que vem antes do numeral termina em palavra que costuma
    reger numero (Art., Titulo, Capitulo, Lei, Inciso, Livro, Secao), e
    ambiguo. Melhor mandar pra revisao do que inventar um assunto.
    """
    t = RE_PREFIXO_NUM.sub("", (titulo or "").strip())
    m = RE_PARTE_FIM.search(t)
    if not m:
        return t.strip(" -–—"), 1, None

    base = t[:m.start()].strip(" -–—")
    bruto = m.group(1)
    parte = ROMANOS.get(bruto.upper()) if not bruto.isdigit() else int(bruto)
    if not parte:
        return t.strip(" -–—"), 1, None

    if not base:
        return t.strip(" -–—"), 1, "titulo e so um numeral"
    if re.search(r"\b(art|artigo|t[íi]tulo|cap[íi]tulo|lei|inciso|livro|"
                 r"se[çc][ãa]o|anexo|d[ée]cada|s[ée]culo)\.?\s*$",
                 sem_acento(base), re.I):
        return base, parte, f"o numeral '{bruto}' pode ser conteudo, nao parte"
    return base, parte, None


def duracao_min(txt):
    try:
        p = [int(x) for x in str(txt).split(":")]
    except Exception:
        return None
    if len(p) == 3:
        return round((p[0] * 3600 + p[1] * 60 + p[2]) / 60)
    if len(p) == 2:
        return round((p[0] * 60 + p[1]) / 60)
    return None


def etiquetas_do_caderno(url):
    """Os `assunto[]` do caderno = ETIQUETAS (§3.0), nunca 'assuntos'."""
    if not url:
        return []
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(url).query)
    ids = []
    for chave in ("assunto", "a"):
        for v in q.get(chave, []):
            ids.extend(int(x) for x in str(v).split(",") if x.strip().isdigit())
    return sorted(set(ids))


def ler_aulas():
    aulas = []
    for arq in sorted(glob.glob(str(DIR_SAIDA / "aulas" / "*" / "*.json"))):
        try:
            aulas.append(json.load(open(arq, encoding="utf-8")))
        except Exception as e:
            print(f"  [!] {Path(arq).name}: {e}")
    return aulas


def montar(aulas):
    """Devolve (materias, revisao). Nada ambiguo e decidido aqui."""
    revisao = []
    por_materia = defaultdict(lambda: defaultdict(list))
    modulos_info = {}

    for a in aulas:
        materia = (a.get("disciplina") or "").strip()
        mod = a.get("modulo") or {}
        mod_id = mod.get("id")
        modulos_info[(materia, mod_id)] = {
            "nome": (mod.get("titulo") or "").strip() or f"Modulo {mod_id}",
            "gran_topico_id": mod_id,
        }
        por_materia[materia][mod_id].append(a)

    materias = []
    for materia, mods in sorted(por_materia.items()):
        # ATENCAO aos dois ids, que tem nomes parecidos e significados
        # diferentes (ja me morderam uma vez):
        #   gran_disciplina_curso_id -> 3392 (Penal) / 363 (Penal Militar).
        #       Vem de `disciplina_id` no JSON da aula. E o id DENTRO do curso.
        #   gran_materia_id          -> 574 (Penal), segundo a spec §3.2.
        #       NAO foi coletado: o campo `materia` do JSON e outra coisa -
        #       e o sub-agrupamento do Gran ("Direito Penal - Parte Geral",
        #       "Parte Especial", "Legislação Especial"), que NAO e um nivel
        #       da nossa hierarquia. Fica nulo e vai pra revisao em vez de eu
        #       gravar o texto errado num campo de id.
        das_minhas = [x for x in aulas if x.get("disciplina") == materia]
        sub = sorted({x.get("materia") for x in das_minhas if x.get("materia")})
        m_out = {
            "id": MATERIA_ID.get(materia),
            "nome": materia,
            "fonte": "gran",
            "gran_materia_id": GRAN_MATERIA_ID.get(materia),
            "gran_disciplina_curso_id": next(
                (x.get("disciplina_id") for x in das_minhas), None),
            "gran_subgrupos": sub,          # so referencia, nao e nivel
            "modulos": [],
        }
        if m_out["gran_materia_id"] is None:
            revisao.append({
                "tipo": "gran_materia_id desconhecido", "materia": materia,
                "por_que": "o coletor nao guardou o id da MATERIA do Gran e a "
                           "spec so informa o de Direito Penal (574). O campo "
                           f"`materia` do JSON e o sub-agrupamento: {sub}",
                "proposta": "pegar da arvore do Gran na proxima coleta"})
        if m_out["id"] is None:
            revisao.append({"tipo": "materia sem id fixo", "materia": materia,
                            "proposta": "acrescentar em MATERIA_ID"})

        for ordem_mod, mod_id in enumerate(sorted(mods, key=lambda x: (x is None, x)), 1):
            lista = sorted(mods[mod_id],
                           key=lambda x: (x.get("ordem") is None, x.get("ordem")))
            info = modulos_info[(materia, mod_id)]

            # --- agrupar as aulas em ASSUNTOS pelo nome-base ---------------
            grupos, ordem_de = defaultdict(list), {}
            for a in lista:
                titulo = (a.get("titulo") or "").strip()
                tipo = tipo_da_aula(titulo)
                base, parte, ambiguo = separar_parte(titulo)

                if ambiguo:
                    revisao.append({
                        "tipo": "titulo ambiguo", "materia": materia,
                        "modulo": info["nome"][:60], "aula": titulo,
                        "por_que": ambiguo,
                        "proposta": f"assunto '{base}' parte {parte} - confirmar"})
                if re.search(r"\s[+/]\s", titulo):
                    revisao.append({
                        "tipo": "dois conceitos num titulo", "materia": materia,
                        "modulo": info["nome"][:60], "aula": titulo,
                        "por_que": "titulo junta dois conceitos com + ou /",
                        "proposta": "decidir se vira um assunto ou dois"})

                # §3.0: exercicios/revisao ficam num assunto proprio;
                # apresentacao idem. Nao sao parte do assunto de conteudo.
                if tipo == "apresentacao":
                    chave = "Apresentação"
                elif tipo in ("exercicios", "revisao"):
                    chave = f"Exercícios — {info['nome'][:40]}"
                else:
                    chave = base

                grupos[chave].append({"aula": a, "parte": parte, "tipo": tipo,
                                      "titulo": titulo})
                ordem_de.setdefault(chave, a.get("ordem") or 0)
                ordem_de[chave] = min(ordem_de[chave], a.get("ordem") or 0)

            assuntos = []
            for ordem_ass, chave in enumerate(
                    sorted(grupos, key=lambda c: ordem_de[c]), 1):
                itens = sorted(grupos[chave], key=lambda x: (x["parte"],
                                                             x["aula"].get("ordem") or 0))
                # partes continuas? (validacao do §3.0.1)
                partes = [i["parte"] for i in itens]
                if chave.startswith("Exercícios") or chave == "Apresentação":
                    partes = list(range(1, len(itens) + 1))
                    for n, i in enumerate(itens, 1):
                        i["parte"] = n
                elif sorted(partes) != list(range(1, len(partes) + 1)):
                    if len(set(partes)) == 1 and len(partes) > 1:
                        # o Gran repetiu o mesmo titulo, sem numerar as partes
                        por_que = (f"{len(partes)} aulas com titulo identico e "
                                   f"sem numeral - o Gran nao numerou as partes")
                        proposta = ("numerar parte 1..N pela ordem do curso "
                                    "(" + ", ".join(
                                        f"ord {i['aula'].get('ordem')}"
                                        for i in itens) + ")")
                    else:
                        por_que = (f"partes {sorted(partes)} - falta alguma ou "
                                   f"o numeral nao era parte")
                        proposta = "conferir os titulos deste assunto"
                    revisao.append({
                        "tipo": "partes descontinuas", "materia": materia,
                        "modulo": info["nome"][:60], "assunto": chave,
                        "por_que": por_que, "proposta": proposta})

                assuntos.append({
                    "ordem": ordem_ass,
                    "nome": chave,
                    "aulas": [{
                        "ordem": i["aula"].get("ordem"),
                        "parte": i["parte"],
                        "tipo": i["tipo"],
                        "titulo": i["titulo"],
                        "gran_aula_id": str(i["aula"].get("codigo_aula")),
                        "duracao_min": duracao_min(i["aula"].get("duracao")),
                        "professor": i["aula"].get("professor"),
                        "etiquetas": etiquetas_do_caderno(
                            i["aula"].get("caderno_questoes")),
                    } for i in itens],
                })

            m_out["modulos"].append({
                "ordem": ordem_mod,
                "nome": info["nome"],
                "gran_topico_id": info["gran_topico_id"],
                "item_edital": None,
                "prioridade": None,
                "assuntos": assuntos,
            })
        materias.append(m_out)

    # --- espelhos: mesmo titulo em materias diferentes --------------------
    por_titulo = defaultdict(list)
    for a in aulas:
        chave = sem_acento((a.get("titulo") or "").lower()).strip()
        por_titulo[chave].append(a)
    for chave, ls in por_titulo.items():
        materias_ = {x.get("disciplina") for x in ls}
        if len(materias_) > 1:
            revisao.append({
                "tipo": "possivel aula espelhada",
                "aula": ls[0].get("titulo"),
                "por_que": f"mesmo titulo em {sorted(materias_)}",
                "proposta": "confirmar espelho_de"})
    return materias, revisao


def validar(materias, revisao):
    """As validacoes obrigatorias do §3.0.1. Falhou uma, a carga nao roda."""
    erros = []
    for m in materias:
        n_aulas = 0
        for mod in m["modulos"]:
            for ass in mod["assuntos"]:
                # "nenhum nome de assunto termina em numeral" (§3.0.1) e uma
                # heuristica pra pegar parte nao separada. Testar com o MESMO
                # separador em vez de um regex solto: senao "Art. 168" e
                # "Art. 318 a 322" - numero de artigo, conteudo legitimo -
                # barram a carga a toa. Se o separador ainda enxerga parte
                # aqui, ai sim sobrou numeral pra separar.
                _, parte_residual, _ = separar_parte(ass["nome"])
                if parte_residual != 1:
                    erros.append(f"{m['nome']} / assunto '{ass['nome']}' termina "
                                 f"em numeral de parte - nao foi separado")
                partes = [a["parte"] for a in ass["aulas"]]
                if sorted(partes) != list(range(1, len(partes) + 1)):
                    erros.append(f"{m['nome']} / '{ass['nome']}': partes "
                                 f"{sorted(partes)} nao sao continuas")
                for a in ass["aulas"]:
                    n_aulas += 1
                    if not a.get("tipo"):
                        erros.append(f"aula sem tipo: {a['titulo']}")
        if m["nome"] == "Direito Penal" and n_aulas != 97:
            erros.append(f"Direito Penal tem {n_aulas} aulas, a arvore do Gran "
                         f"tem 97")
        if m["nome"] == "Direito Penal" and len(m["modulos"]) != 8:
            erros.append(f"Direito Penal tem {len(m['modulos'])} modulos, "
                         f"deviam ser 8")
    if revisao:
        erros.append(f"a lista de revisao tem {len(revisao)} item(ns) - "
                     f"tudo ambiguo precisa ser decidido e escrito no arquivo")
    return erros


def mostrar_arvore(materias, filtro):
    for m in materias:
        if filtro and filtro.lower() not in m["nome"].lower():
            continue
        n_ass = sum(len(mod["assuntos"]) for mod in m["modulos"])
        n_aulas = sum(len(a["aulas"]) for mod in m["modulos"]
                      for a in mod["assuntos"])
        print(f"\nMATERIA  {m['nome']}   "
              f"({len(m['modulos'])} modulos, {n_ass} assuntos, {n_aulas} aulas)")
        for mod in m["modulos"]:
            print(f"  MODULO   {mod['ordem']}. {mod['nome'][:70]}")
            for ass in mod["assuntos"]:
                minutos = sum(a["duracao_min"] or 0 for a in ass["aulas"])
                marca = "" if len(ass["aulas"]) == 1 else f"  [{len(ass['aulas'])} partes]"
                print(f"     ASSUNTO  {ass['nome'][:58]}{marca}  ({minutos} min)")
                for a in ass["aulas"]:
                    t = "" if a["tipo"] == "conteudo" else f" <{a['tipo']}>"
                    print(f"        AULA p{a['parte']}  {a['titulo'][:60]}"
                          f"  {a['duracao_min']}min{t}")


def main():
    ap = argparse.ArgumentParser(description="Gera o edital verticalizado (§3.0.1).")
    ap.add_argument("--mostrar", nargs="?", const="", metavar="MATERIA")
    ap.add_argument("--revisao", action="store_true")
    ap.add_argument("--validar", action="store_true")
    ap.add_argument("--gravar", action="store_true",
                    help="escreve o edital_verticalizado.yaml (so se validar)")
    ap.add_argument("--rascunho", action="store_true",
                    help="escreve mesmo com pendencia, como RASCUNHO nao "
                         "aprovado, com a lista de revisao dentro do arquivo")
    args = ap.parse_args()

    aulas = ler_aulas()
    print(f"  {len(aulas)} aulas lidas de {DIR_SAIDA}")
    materias, revisao = montar(aulas)

    if args.mostrar is not None:
        mostrar_arvore(materias, args.mostrar)
        return 0

    if args.revisao or not (args.validar or args.gravar):
        print(f"\n  LISTA DE REVISAO: {len(revisao)} item(ns)")
        print("  (o script NAO decide isto - §3.0.1 regra 4)\n")
        por_tipo = defaultdict(list)
        for r in revisao:
            por_tipo[r["tipo"]].append(r)
        for tipo, itens in sorted(por_tipo.items()):
            print(f"  --- {tipo}: {len(itens)}")
            for r in itens[:12]:
                alvo = r.get("aula") or r.get("assunto") or r.get("materia")
                print(f"     {str(alvo)[:60]}")
                print(f"        por que: {r['por_que']}")
                print(f"        proposta: {r['proposta']}")
            if len(itens) > 12:
                print(f"     ... e mais {len(itens) - 12}")

    erros = validar(materias, revisao)
    print(f"\n  VALIDACOES (§3.0.1): "
          f"{'TODAS OK' if not erros else str(len(erros)) + ' falharam'}")
    for e in erros[:15]:
        print(f"     [!] {e}")

    for m in materias:
        n_ass = sum(len(mod["assuntos"]) for mod in m["modulos"])
        n_aulas = sum(len(a["aulas"]) for mod in m["modulos"]
                      for a in mod["assuntos"])
        multi = sum(1 for mod in m["modulos"] for a in mod["assuntos"]
                    if len(a["aulas"]) > 1)
        print(f"\n  {m['nome']}: {len(m['modulos'])} modulos · {n_ass} assuntos "
              f"({multi} com mais de uma aula) · {n_aulas} aulas")

    if args.gravar or args.rascunho:
        if erros and not args.rascunho:
            print("\n  [!] NAO gravei: as validacoes precisam passar antes "
                  "(§3.0.1). Use --rascunho pra gerar assim mesmo, marcado "
                  "como nao aprovado.")
            return 1
        import yaml
        doc = {
            "versao": 1,
            # aprovado_em fica NULO de proposito ate o dono revisar. A carga
            # so pode ler arquivo aprovado (§3.0.1).
            "aprovado_em": None,
            "gerado_por": "verticalizar.py",
            "pendencias_de_revisao": revisao if erros else [],
            "materias": materias,
        }
        ARQUIVO.write_text(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False,
                           default_flow_style=False),
            encoding="utf-8")
        estado = "RASCUNHO (nao aprovado)" if erros else "gerado, falta aprovar"
        print(f"\n  {estado}: {ARQUIVO}")
        if erros:
            print(f"  {len(revisao)} pendencia(s) escritas dentro do arquivo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
