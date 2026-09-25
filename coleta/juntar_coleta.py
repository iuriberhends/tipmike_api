# -*- coding: utf-8 -*-
"""
juntar_coleta.py
================
Traz pra `saida/` o que foi coletado noutra maquina (tipicamente a VPS).

Junta duas coisas:
  - os JSON de questoes: casados por ID, entao rodar duas vezes nao duplica e
    o que ja existe aqui nunca e perdido (o de la so acrescenta);
  - o `_progresso.json`: MESCLADO, nunca sobrescrito. O arquivo daqui tem o
    historico do PC (Direito Penal, passada por orgao); o de la tem o das
    materias do edital. Copiar por cima perderia metade.

USO:
    python juntar_coleta.py <pasta com saida/>
    python juntar_coleta.py _para_vps_coleta/_para_vps_coleta
    python juntar_coleta.py ... --simular
"""

import sys
import io
import json
import shutil
import argparse
from pathlib import Path
from datetime import datetime

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)

RAIZ = Path(__file__).parent
DESTINO = RAIZ / "saida"


def juntar_questoes(origem, simular):
    n_arq = n_novas = n_ja = 0
    for arq in sorted((origem / "questoes").glob("*/*.json")):
        try:
            de_la = json.loads(arq.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [!] {arq.name}: nao consegui ler ({e})")
            continue
        alvo = DESTINO / "questoes" / arq.parent.name / arq.name
        aqui = {}
        if alvo.exists():
            try:
                d = json.loads(alvo.read_text(encoding="utf-8"))
                aqui = {q["id"]: q for q in (d.get("questoes") or [])
                        if q.get("id")}
            except Exception:
                pass
        antes = len(aqui)
        for q in de_la.get("questoes") or []:
            if not q.get("id"):
                continue
            velha = aqui.get(q["id"])
            # nao deixar a versao de la apagar o que ja foi conquistado aqui:
            # indice de acerto e comentario custam cota, a listagem nao.
            if velha:
                for campo in ("indice_acerto", "comentario_professor",
                              "gabarito_confere_com_comentario"):
                    if velha.get(campo) is not None and q.get(campo) is None:
                        q[campo] = velha[campo]
                if q.get("indice_acerto") is not None:
                    q.pop("leves_pendentes", None)
            aqui[q["id"]] = q
        de_la["questoes"] = list(aqui.values())
        novas = len(aqui) - antes
        n_arq += 1
        n_novas += novas
        n_ja += antes
        print(f"  {arq.parent.name}: {antes} aqui + {novas} novas = {len(aqui)}")
        if not simular:
            alvo.parent.mkdir(parents=True, exist_ok=True)
            alvo.write_text(json.dumps(de_la, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    return n_arq, n_novas


def juntar_progresso(origem, simular):
    de_la_p = origem / "_progresso.json"
    aqui_p = DESTINO / "_progresso.json"
    if not de_la_p.exists():
        print("  (sem _progresso.json na origem - nada a mesclar)")
        return
    de_la = json.loads(de_la_p.read_text(encoding="utf-8"))
    aqui = json.loads(aqui_p.read_text(encoding="utf-8")) if aqui_p.exists() else {}

    # backup antes de mexer: progresso perdido = coleta refeita
    if aqui_p.exists() and not simular:
        bk = aqui_p.with_suffix(f".json.bak-{datetime.now():%Y%m%d%H%M%S}")
        shutil.copy2(aqui_p, bk)
        print(f"  backup do progresso daqui: {bk.name}")

    novos_blocos = 0
    dest_q = aqui.setdefault("questoes", {})
    for k, v in (de_la.get("questoes") or {}).items():
        if k not in dest_q:
            novos_blocos += 1
        dest_q[k] = v

    novas_mat = 0
    dest_fe = aqui.setdefault("filtros_edital", {})
    for mat, v in (de_la.get("filtros_edital") or {}).items():
        if mat not in dest_fe:
            novas_mat += 1
            dest_fe[mat] = v
        else:
            # mescla o que e acumulativo, mantendo o resto de la
            for chave in ("puladas", "falhadas"):
                juntos = dict(dest_fe[mat].get(chave) or {})
                juntos.update(v.get(chave) or {})
                v[chave] = juntos
            dest_fe[mat] = v
    print(f"  progresso: +{novos_blocos} bloco(s), +{novas_mat} materia(s)")
    if not simular:
        aqui_p.write_text(json.dumps(aqui, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("origem", help="pasta que CONTEM a saida/ da outra maquina")
    ap.add_argument("--simular", action="store_true")
    args = ap.parse_args()

    origem = Path(args.origem)
    if not origem.is_absolute():
        origem = RAIZ / origem
    if origem.name == "saida":
        origem = origem.parent
    if not (origem / "saida" / "questoes").exists():
        print(f"  [ERRO] nao achei saida/questoes em {origem}")
        return 1

    print(f"  de : {origem / 'saida'}")
    print(f"  pra: {DESTINO}\n")
    n_arq, n_novas = juntar_questoes(origem / "saida", args.simular)
    print()
    juntar_progresso(origem / "saida", args.simular)
    print(f"\n  {n_arq} arquivo(s), {n_novas} questao(oes) novas")
    if args.simular:
        print("  (--simular: nada foi gravado)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
