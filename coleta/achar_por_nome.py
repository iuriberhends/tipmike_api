# -*- coding: utf-8 -*-
"""
achar_por_nome.py
=================
Resolve os `buscar_por_nome` do `filtros_edital.json`: acha o id da etiqueta
pelo nome e mostra id + nome + total, como a regra 5 exige.

COMO ACHA SEM PEDIR A ARVORE
----------------------------
A arvore de assuntos nao tem endereco conhecido, e levantar a arvore inteira
custaria ~5h de requisicao. Mas cada questao carrega os proprios assuntos com
id E nome. Entao eu consulto a etiqueta PAI (`dentro_de`), leio 1-2 paginas e
recolho os nomes das filhas - 1 a 2 consultas por busca.

O que ele NAO faz: coletar. So descobre e mostra. Quem decide usar e o dono.

USO:
    python achar_por_nome.py
    python achar_por_nome.py --paginas 3     (amostra maior)
"""

import sys
import re
import json
import argparse
import unicodedata
import importlib.util
from pathlib import Path

RAIZ = Path(__file__).parent
ARQ = RAIZ / "filtros_edital.json"


def nz(t):
    t = unicodedata.normalize("NFKD", str(t or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", t.lower())).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paginas", type=int, default=2)
    ap.add_argument("--porta", type=int, default=9380)
    args = ap.parse_args()

    sp = importlib.util.spec_from_file_location("g", RAIZ / "gran_extrator.py")
    g = importlib.util.module_from_spec(sp)
    argv, sys.argv = sys.argv, [sys.argv[0]]
    sp.loader.exec_module(g)
    sys.argv = argv

    progresso = g.ler_progresso()
    g.PARAM_BANCA = progresso.get("param_banca")
    if not g.PARAM_BANCA:
        print("  [ERRO] parametro de banca nao aprendido")
        return 1

    filtro = json.loads(ARQ.read_text(encoding="utf-8"))
    buscas = []
    for m in filtro["materias"]:
        if m.get("coletar") is False:
            continue
        for i in (m.get("itens") or []):
            for b in (i.get("buscar_por_nome") or []):
                buscas.append((m["materia"], i.get("item", ""), b))
        for b in (m.get("buscar_por_nome") or []):
            buscas.append((m["materia"], "(materia)", b))
    if not buscas:
        print("  nada pendente")
        return 0

    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        br = pw.chromium.connect_over_cdp(f"http://localhost:{args.porta}")
    except Exception as e:
        print(f"  [ERRO] nao conectei na porta {args.porta}: {str(e)[:60]}")
        return 1
    pgs = [x for c in br.contexts for x in c.pages]
    pagina = next((x for x in pgs if "grancursos" in x.url), None) or (
        pgs[0] if pgs else br.contexts[0].new_page())

    # o que ja sei de nome, de graca
    g._carregar_nomes_de_assunto()
    achados = []
    por_pai = {}
    for mat, item, b in buscas:
        pai = str(b.get("dentro_de") or "")
        termos = [nz(x) for x in (b.get("termos") or [])]
        print(f"\n  {'=' * 70}")
        print(f"  {mat} - {b.get('procurar')}")
        print(f"     onde: {b.get('onde')}")

        # 1) tenta nos nomes que ja tenho: pode nao custar nada
        pre = [(i, n) for i, n in g._NOMES_DE_ASSUNTO.items()
               if n and any(t in nz(n) for t in termos)]
        if pre:
            print(f"     ja conhecia {len(pre)}:")
            for i, n in pre[:8]:
                print(f"        {i:>8}  {n[:56]}")
            achados += [{"materia": mat, "item": item, "id": i, "nome": n,
                         "origem": "ja conhecido"} for i, n in pre]
            continue

        # 2) consulta o pai e le os nomes das filhas
        if not pai:
            print("     [!] sem 'dentro_de' - nao sei por onde procurar")
            continue
        if pai not in por_pai:
            nomes = {}
            fonte = g.ListagemPelaTela(pagina, [pai], [g.NIVEL_PADRAO], 100,
                                       sem_cliente=True)
            try:
                fonte.preparar(g.montar_url_tela(
                    [pai], [g.NIVEL_PADRAO],
                    bancas=sorted(g.BANCAS_PERMITIDAS)))
                for p in range(1, args.paginas + 1):
                    try:
                        j = fonte.buscar(p)
                    except Exception as e:
                        print(f"     (pagina {p}: {str(e).splitlines()[0][:44]})")
                        break
                    for q in (j.get("data") or {}).get("rows") or []:
                        for a in (q.get("assuntos") or []):
                            if isinstance(a, dict) and a.get("id") is not None:
                                n = a.get("titulo") or a.get("nome")
                                if n:
                                    nomes[str(a["id"])] = n
                    if p < args.paginas:
                        g.pausa(pagina, "procurando")
            except Exception as e:
                print(f"     [!] nao consultei o pai {pai}: "
                      f"{str(e).splitlines()[0][:50]}")
            finally:
                fonte.fechar()
            por_pai[pai] = nomes
            g._NOMES_DE_ASSUNTO.update(nomes)
            g.pausa(pagina, "procurando")

        nomes = por_pai.get(pai) or {}
        hits = [(i, n) for i, n in nomes.items()
                if any(t in nz(n) for t in termos)]
        print(f"     li {len(nomes)} nome(s) debaixo de {pai}; "
              f"{len(hits)} casam com {b.get('termos')}")
        for i, n in hits[:10]:
            print(f"        {i:>8}  {n[:56]}")
        if not hits:
            print("        (nenhuma - a amostra pode nao ter alcancado; "
                  "tente --paginas 4)")
        achados += [{"materia": mat, "item": item, "id": i, "nome": n,
                     "origem": f"filha de {pai}"} for i, n in hits]

    alvo = RAIZ / "saida" / "achados_por_nome.json"
    alvo.parent.mkdir(parents=True, exist_ok=True)
    alvo.write_text(json.dumps(achados, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print(f"\n  {len(achados)} etiqueta(s) achada(s) - gravado em {alvo.name}")
    print("  NADA foi coletado nem adicionado ao filtro. Decide o dono.")
    if progresso.get("nomes_de_assunto") is not None:
        progresso["nomes_de_assunto"].update(
            {k: v for k, v in g._NOMES_DE_ASSUNTO.items() if v})
        g.gravar_progresso(progresso)
    return 0


if __name__ == "__main__":
    sys.exit(main())
