# -*- coding: utf-8 -*-
"""
preparar_coleta_vps.py
======================
Gera o `pacote_coleta.json`: tudo que o coletor de questoes precisa saber,
num arquivo pequeno.

Por que existe: a coleta pela tela le `saida/aulas` (55 MB de JSON) e
`saida/questoes` (32 MB) so para extrair tres coisas - as etiquetas de cada
materia, as etiquetas de prova de Soldado e os ids ja coletados. Copiar 87 MB
pra VPS a cada rodada e desnecessario: isso cabe em algumas centenas de KB.

USO:
    python preparar_coleta_vps.py
    (depois copie pacote_coleta.json + gran_extrator.py pra VPS e rode la:
     python gran_extrator.py --navegador firefox --coletar-edital
            --adiar-leves --pacote pacote_coleta.json)
"""

import sys
import json
import importlib.util
from pathlib import Path

RAIZ = Path(__file__).parent
_sp = importlib.util.spec_from_file_location("g", RAIZ / "gran_extrator.py")
g = importlib.util.module_from_spec(_sp)
_argv, sys.argv = sys.argv, [sys.argv[0]]
_sp.loader.exec_module(g)
sys.argv = _argv

SAIDA = RAIZ / "pacote_coleta.json"


def main():
    por_mat = g._filtros_das_aulas()
    soldado = g._etiquetas_de_soldado()
    ja = g._ids_ja_coletados()

    ordem = [m for m in g.ORDEM_MATERIAS if m in por_mat] + \
            sorted(m for m in por_mat if m not in g.ORDEM_MATERIAS)

    pacote = {
        "gerado_em": None,          # preenchido abaixo, sem hora do sistema no json
        "materias": [{"rotulo": m, "assuntos": sorted(por_mat[m])}
                     for m in ordem],
        # {raiz: [etiquetas]} - as que CAIRAM em prova de PM-BA Soldado e
        # podem nao estar em caderno de aula nenhuma (§3.7)
        "soldado_por_raiz": {str(k): sorted(v) for k, v in soldado.items()},
        "ja_coletadas": sorted(ja),
    }
    from datetime import datetime
    pacote["gerado_em"] = datetime.now().isoformat(timespec="seconds")

    SAIDA.write_text(json.dumps(pacote, ensure_ascii=False), encoding="utf-8")
    mb = SAIDA.stat().st_size / 1e6

    print(f"  {len(pacote['materias'])} materias")
    for m in pacote["materias"]:
        print(f"     {m['rotulo']:32} {len(m['assuntos']):>4} etiquetas")
    print(f"  {sum(len(v) for v in pacote['soldado_por_raiz'].values())} "
          f"etiquetas de prova de Soldado")
    print(f"  {len(pacote['ja_coletadas'])} questoes ja coletadas "
          f"(nao serao rebaixadas)")
    print(f"\n  GRAVADO: {SAIDA}  ({mb:.2f} MB)")
    print("\n  Pra VPS, copie SO estes dois:")
    print("     gran_extrator.py")
    print("     pacote_coleta.json")
    print("\n  E rode la:")
    print("     python gran_extrator.py --navegador firefox --coletar-edital \\")
    print("            --adiar-leves --pacote pacote_coleta.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
