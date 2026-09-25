# -*- coding: utf-8 -*-
"""
preflight_edital.py
===================
A tabela que a regra 10 do `filtros_edital.json` exige ANTES de coletar:
por materia, cada etiqueta com id, nome, total antes e depois do filtro de
banca, quantas ja estao no PC e quantas faltam.

DUAS PARTES, E SO UMA CUSTA REQUISICAO
--------------------------------------
  --offline (padrao): id, nome e "quantas ja estao no PC". Sai na hora, lendo
      `saida/`. Serve pra ver o plano e conferir a lista antes de gastar nada.
  --medir: abre o filtro de cada etiqueta pra ler os totais no Gran. Uma
      carga de tela por etiqueta (~135), no ritmo normal.

O total "antes" (sem banca) custa uma segunda carga por etiqueta. Por isso e
opcional: `--com-antes`. Sem ele, so o total que de fato vai ser coletado.

USO:
    python preflight_edital.py
    python preflight_edital.py --medir
    python preflight_edital.py --medir --com-antes
    python preflight_edital.py --medir --materia "Direito Penal"
"""

import sys
import json
import argparse
import importlib.util
from pathlib import Path
from collections import defaultdict

RAIZ = Path(__file__).parent
ARQ_FILTRO = RAIZ / "filtros_edital.json"
SAIDA = RAIZ / "saida"


def carregar_extrator():
    sp = importlib.util.spec_from_file_location("g", RAIZ / "gran_extrator.py")
    g = importlib.util.module_from_spec(sp)
    argv, sys.argv = sys.argv, [sys.argv[0]]
    sp.loader.exec_module(g)
    sys.argv = argv
    return g


def etiquetas_do_plano(filtro, so_materia=None):
    """
    Tudo que a regra 2 manda coletar: `itens` + `evidencia_soldado`.

    Devolve [(materia, id, nome, origem)] sem repetir id dentro da materia -
    a mesma etiqueta aparece em mais de um item quando o edital junta dois
    assuntos, e coletar duas vezes seria pagar duas vezes pelo mesmo.
    """
    saida, pendentes, nao = [], [], defaultdict(list)
    for m in filtro["materias"]:
        mat = m["materia"]
        if so_materia and so_materia.lower() not in mat.lower():
            continue
        if m.get("coletar") is False:
            continue
        vistos = set()
        for it in (m.get("itens") or []):
            for e in (it.get("etiquetas") or []):
                i = str(e.get("id"))
                if i and i not in vistos:
                    vistos.add(i)
                    saida.append((mat, i, e.get("nome"), it.get("item", "")[:40]))
            for b in (it.get("buscar_por_nome") or []):
                pendentes.append((mat, it.get("item", ""), b))
        for e in (m.get("evidencia_soldado") or []):
            i = str(e.get("id"))
            if i and i not in vistos:
                vistos.add(i)
                saida.append((mat, i, e.get("nome"), "evidencia_soldado"))
        for b in (m.get("buscar_por_nome") or []):
            pendentes.append((mat, "(materia)", b))
        for e in (m.get("nao_coletar") or []):
            nao[mat].append((str(e.get("id")), e.get("nome"), e.get("motivo")))
    return saida, pendentes, nao


def ja_no_pc():
    """id da etiqueta -> quantas questoes com ela ja estao em saida/questoes."""
    por_etq = defaultdict(set)
    base = SAIDA / "questoes"
    if not base.exists():
        return por_etq
    for arq in base.glob("*/*.json"):
        try:
            d = json.loads(arq.read_text(encoding="utf-8"))
        except Exception:
            continue
        for q in d.get("questoes") or []:
            qid = q.get("id")
            for a in (q.get("assunto_ids") or []):
                por_etq[str(a)].add(qid)
    return por_etq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--medir", action="store_true",
                    help="consulta o Gran pra ler os totais")
    ap.add_argument("--com-antes", action="store_true",
                    help="mede tambem o total SEM filtro de banca (dobra o custo)")
    ap.add_argument("--materia")
    ap.add_argument("--porta", type=int, default=9380)
    args = ap.parse_args()

    if not ARQ_FILTRO.exists():
        print(f"  [ERRO] nao achei {ARQ_FILTRO.name}")
        return 1
    filtro = json.loads(ARQ_FILTRO.read_text(encoding="utf-8"))
    alvo, pendentes, nao = etiquetas_do_plano(filtro, args.materia)
    tenho = ja_no_pc()

    print(f"  {filtro.get('edital', '(sem titulo)')}")
    print(f"  versao {filtro.get('versao')} - {filtro.get('gerado_em')}")
    print(f"\n  {len(alvo)} etiqueta(s) a coletar, "
          f"{len(pendentes)} a achar pelo nome, "
          f"{sum(len(v) for v in nao.values())} proibida(s)\n")

    pagina = g = None
    if args.medir:
        g = carregar_extrator()
        progresso = g.ler_progresso()
        g.PARAM_BANCA = progresso.get("param_banca")
        if not g.PARAM_BANCA:
            print("  [ERRO] ainda nao aprendi o parametro de banca.")
            return 1
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        try:
            b = pw.chromium.connect_over_cdp(f"http://localhost:{args.porta}")
        except Exception as e:
            print(f"  [ERRO] nao conectei na porta {args.porta}: {str(e)[:60]}")
            return 1
        pgs = [x for c in b.contexts for x in c.pages]
        pagina = next((x for x in pgs if "grancursos" in x.url), None) or (
            pgs[0] if pgs else b.contexts[0].new_page())

    def medir(etq, com_banca=True):
        import re
        url = g.montar_url_tela(
            [etq], [g.NIVEL_PADRAO],
            bancas=sorted(g.BANCAS_PERMITIDAS) if com_banca else None)
        est = g.preparar_tela(pagina, url_filtro=url, por_pagina=100)
        bruto = re.sub(r"\D", "", str(est.get("contagem") or "").split("quest")[0])
        return int(bruto) if bruto else None

    linhas = []
    mat_atual = None
    for mat, etq, nome, origem in alvo:
        if mat != mat_atual:
            mat_atual = mat
            print(f"\n  {'=' * 74}")
            print(f"  {mat}")
            print(f"  {'=' * 74}")
            print(f"  {'etiqueta':>9} {'nome':38} {'antes':>7} {'depois':>7} "
                  f"{'no PC':>6} {'faltam':>7}")
        n_pc = len(tenho.get(etq, ()))
        antes = depois = None
        if args.medir:
            try:
                if args.com_antes:
                    antes = medir(etq, com_banca=False)
                    g.pausa(pagina, "medindo")
                depois = medir(etq, com_banca=True)
                g.pausa(pagina, "medindo")
            except Exception as e:
                print(f"  {etq:>9} {str(nome)[:38]:38}  ERRO "
                      f"{str(e).splitlines()[0][:34]}")
                linhas.append({"materia": mat, "etiqueta": etq, "nome": nome,
                               "erro": str(e).splitlines()[0][:120]})
                continue
        faltam = (depois - n_pc) if isinstance(depois, int) else None
        print(f"  {etq:>9} {str(nome)[:38]:38} "
              f"{(antes if antes is not None else '-'):>7} "
              f"{(depois if depois is not None else '-'):>7} "
              f"{n_pc:>6} {(faltam if faltam is not None else '-'):>7}")
        linhas.append({"materia": mat, "etiqueta": etq, "nome": nome,
                       "item": origem, "antes": antes, "depois": depois,
                       "ja_no_pc": n_pc, "faltam": faltam})

    if pendentes:
        print(f"\n  {'=' * 74}")
        print("  A ACHAR PELO NOME antes de coletar (regra 5)")
        print(f"  {'=' * 74}")
        for mat, item, b in pendentes:
            print(f"  {mat[:24]:24} {json.dumps(b, ensure_ascii=False)[:66]}")
            if item:
                print(f"     item: {item[:66]}")

    if nao:
        print(f"\n  {'=' * 74}")
        print("  NAO COLETAR (regra 4)")
        print(f"  {'=' * 74}")
        for mat, lista in nao.items():
            for i, nome, motivo in lista:
                print(f"  {mat[:22]:22} {i:>9} {str(nome)[:28]:28} "
                      f"{str(motivo or '')[:30]}")

    alvo_arq = SAIDA / "preflight_edital.json"
    alvo_arq.parent.mkdir(parents=True, exist_ok=True)
    alvo_arq.write_text(json.dumps(linhas, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"\n  gravado em {alvo_arq}")
    if not args.medir:
        print("  (sem --medir: 'antes' e 'depois' ficam vazios - nada foi "
              "pedido ao Gran)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
