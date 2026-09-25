# -*- coding: utf-8 -*-
"""
conferir_edital.py
==================
O que foi coletado cobre o EDITAL?

POR QUE EXISTE
--------------
O filtro da coleta nasce dos cadernos de questao das AULAS - nao do texto do
edital. Na pratica os dois deveriam bater, porque o curso foi montado pra esta
prova, mas isso era suposicao minha: eu nunca tinha conferido. O dono
perguntou "voce ta seguindo o edital?" e eu nao tinha como responder com
numero.

De onde vem o edital: do TITULO DO MODULO das aulas. O Gran escreve ali o
trecho do edital que o modulo cobre - "1. Descobrimento do Brasil (1500). 2.
Brasil Colonia (1530-1815)...". E o edital oficial, na fonte que ja esta no
disco.

NAO FAZ REQUISICAO. Le so `saida/`.

USO:
    python conferir_edital.py
    python conferir_edital.py --materia "História do Brasil"
    python conferir_edital.py --detalhe        (mostra os assuntos achados)
"""

import sys
import re
import json
import argparse
import unicodedata
from pathlib import Path
from collections import defaultdict

RAIZ = Path(__file__).parent
SAIDA = RAIZ / "saida"

# Palavras que sozinhas nao identificam item nenhum - se eu casasse por elas,
# qualquer coisa "cobriria" qualquer item.
VAZIAS = {"de", "da", "do", "das", "dos", "e", "a", "o", "as", "os", "no", "na",
          "em", "para", "por", "com", "the", "ate", "sua", "seu", "que"}


def nz(t):
    t = unicodedata.normalize("NFKD", str(t or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", t.lower())).strip()


def itens_do_edital():
    """materia -> lista de itens, lidos do titulo dos modulos das aulas."""
    por_mat = defaultdict(set)
    base = SAIDA / "aulas"
    if not base.exists():
        return {}
    for arq in base.glob("*/*.json"):
        try:
            d = json.loads(arq.read_text(encoding="utf-8"))
        except Exception:
            continue
        mat = d.get("materia")
        titulo = ((d.get("modulo") or {}).get("titulo") or "").strip()
        if not mat or len(titulo) < 40:
            continue
        for bruto in re.split(r"\n|\d+\.\s", titulo):
            item = bruto.strip(" .;")
            if len(item) > 4:
                por_mat[mat].add(item)
    return {m: sorted(v) for m, v in por_mat.items()}


# Como o CURSO fatia a materia -> a pasta onde as questoes foram gravadas.
#
# O edital vive nos titulos de modulo, e o curso os espalha: "Gramatica FCC" e
# "Interpretacao de Texto" sao Lingua Portuguesa; "Legislacao Federal" e
# Igualdade Racial. Sem este mapa o conferidor procurava uma pasta
# "Gramatica FCC", nao achava e dizia "0 questoes coletadas" - com 47.959 de
# Portugues no disco. Terceira vez hoje que um conferidor meu reprova o que
# esta certo.
ONDE_ESTAO = {
    "Gramática FCC": "Língua Portuguesa",
    "Interpretação de Texto": "Língua Portuguesa",
    "Matemática Básica": "Matemática",
    "Raciocínio Lógico": "Matemática",
    "História": "História do Brasil",
    "História da Bahia": "História do Brasil",
    "Geografia": "Geografia do Brasil",
    "Atualidades e Conhecimentos Gerais": "Atualidades",
    "Constituição do Estado da Bahia": "Direito Constitucional",
    "Direito Internacional": "Direitos Humanos",
    "Legislação": "Igualdade Racial e de Gênero",
    "Legislação Federal": "Igualdade Racial e de Gênero",
    "Lei 7.716/89": "Igualdade Racial e de Gênero",
    "Lei 9.455/97": "Igualdade Racial e de Gênero",
    "Lei nº 11.340/2006 - Lei Maria da Penha": "Igualdade Racial e de Gênero",
    "Promoção da Igualdade Racial e de Gênero": "Igualdade Racial e de Gênero",
    "Direito Penal - Parte Geral": "Direito Penal",
    "Direito Penal - Parte Especial": "Direito Penal",
}


def assuntos_coletados(mat):
    """Os nomes de assunto que aparecem nas questoes ja coletadas da materia."""
    nomes, n_q = set(), 0
    alvo = nz(ONDE_ESTAO.get(mat, mat))
    for arq in (SAIDA / "questoes").glob("*/*.json"):
        try:
            d = json.loads(arq.read_text(encoding="utf-8"))
        except Exception:
            continue
        # CASAR PELO CAMPO **E** PELO NOME DA PASTA.
        #
        # Os arquivos antigos (Direito Penal, orgao PM BA) nao tem o campo
        # `materia` - foram gravados antes dele existir. Comparando so o
        # campo, o conferidor dizia "0 questoes coletadas" pra Direito Penal,
        # que tem 6.821. Um conferidor que reprova o que esta certo e pior
        # que nenhum.
        cand = {nz(d.get("materia")), nz(arq.parent.name),
                nz(arq.parent.name.replace("edital ", ""))}
        cand.discard("")
        if not any(c == alvo or c.startswith(alvo) or alvo.startswith(c)
                   for c in cand):
            continue
        for q in d.get("questoes") or []:
            n_q += 1
            for a in (q.get("assuntos") or []):
                if isinstance(a, dict):
                    nome = a.get("titulo") or a.get("nome")
                    if nome:
                        nomes.add(nome)
    return nomes, n_q


def cobre(item, nomes_nz):
    """
    O item do edital aparece em algum assunto coletado?

    Casa por PALAVRA DE CONTEUDO, nao por texto inteiro: o edital escreve
    "Revolta de Canudos" e o Gran etiqueta "Canudos". Exijo que TODAS as
    palavras de conteudo do item apareçam no mesmo assunto - senao "Primeira
    Republica" casaria com qualquer coisa que tivesse "republica".
    """
    inz = nz(item)
    palavras = [p for p in inz.split() if p not in VAZIAS and len(p) > 2]
    if not palavras:
        return None

    # DIRECAO 1: o nome da etiqueta cabe DENTRO do item do edital.
    #
    # E a direcao que funciona na maioria: o edital escreve "Conjuntos
    # numericos: Numeros Naturais, Inteiros, Racionais..." e o Gran etiqueta
    # so "Conjuntos". A 1a versao so olhava o contrario (palavras do item
    # dentro do nome da etiqueta) e por isso dizia 0/7 em Matematica, que tem
    # 25.798 questoes coletadas. Conferidor que reprova o que esta certo ja me
    # custou duas vezes hoje.
    #
    # PALAVRA INTEIRA, sempre: sem isso "etica" casa dentro de "aritmetica".
    import re as _re
    for nome in nomes_nz:
        if len(nome) > 4 and _re.search(
                r"(?:^| )" + _re.escape(nome) + r"(?:$| )", inz):
            return nome

    # DIRECAO 2: o nucleo do item dentro do nome da etiqueta.
    # Pega os itens curtos ("Revolta de Canudos" -> etiqueta "Canudos").
    nucleo = palavras[:3]
    for nome in nomes_nz:
        if all(_re.search(r"(?:^| )" + _re.escape(p) + r"(?:$| )", nome)
               for p in nucleo):
            return nome
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--materia")
    ap.add_argument("--detalhe", action="store_true")
    args = ap.parse_args()

    edital = itens_do_edital()
    if not edital:
        print("  [ERRO] nao achei aulas em saida/aulas")
        return 1

    mats = [args.materia] if args.materia else sorted(edital)
    geral_ok = geral_tot = 0
    for mat in mats:
        itens = edital.get(mat)
        if not itens:
            continue
        nomes, n_q = assuntos_coletados(mat)
        nomes_nz = {nz(n) for n in nomes}
        print(f"\n  === {mat} ===")
        print(f"  {n_q} questoes coletadas, {len(nomes)} assuntos distintos")
        if not n_q:
            print("  (nada coletado ainda - a conferencia so vale no fim)")
        achou = faltam = 0
        for item in itens:
            onde = cobre(item, nomes_nz)
            if onde:
                achou += 1
                if args.detalhe:
                    print(f"     ok    {item[:52]:52} <- {onde[:34]}")
            else:
                faltam += 1
                print(f"     FALTA {item[:70]}")
        if not args.detalhe and achou:
            print(f"     ({achou} item(ns) cobertos, nao listados)")
        print(f"  COBERTURA: {achou}/{achou + faltam}")
        geral_ok += achou
        geral_tot += achou + faltam

    if geral_tot:
        print(f"\n  {'=' * 56}")
        print(f"  TOTAL: {geral_ok}/{geral_tot} itens do edital com questao "
              f"({100 * geral_ok / geral_tot:.0f}%)")
        print("  Itens sem questao podem ser: materia ainda em coleta,")
        print("  ou etiqueta que o Gran nomeia diferente do edital.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
