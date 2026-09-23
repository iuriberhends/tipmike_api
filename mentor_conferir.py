# -*- coding: utf-8 -*-
"""
mentor_conferir.py
==================
A saude do schema `mentor`, lida direto do banco.

Por que existe: pra conferir se a carga ficou boa eu tinha mandado o dono
subir a API, fazer login e chamar /mentor/saude - pedindo e-mail e senha que
ele nao precisa digitar. O dado esta no Postgres e o DSN ja e conhecido. Uma
pergunta sobre dado se responde no banco, nao passando por uma API.

USO:
    python mentor_conferir.py
    python mentor_conferir.py --dsn "postgresql://..."
"""

import sys
import io
import os
import argparse

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)

DSN_PADRAO = os.environ.get(
    "MIKEDB_DSN", "postgresql://postgres:mikedb0702@localhost:5432/mikedb")


CONSULTAS = {
    "contagem": """
        SELECT
          (SELECT count(*) FROM mentor.questao)                      AS questoes,
          (SELECT count(*) FROM mentor.topico)                       AS topicos,
          (SELECT count(DISTINCT questao_id)
             FROM mentor.questao_topico)                             AS ligadas,
          (SELECT count(*) FROM mentor.questao_topico)               AS vinculos,
          (SELECT count(*) FROM mentor.questao
            WHERE assunto_ids IS NULL
               OR cardinality(assunto_ids) = 0)                      AS sem_assunto,
          (SELECT count(*) FROM mentor.questao WHERE topico_id IS NOT NULL)
                                                                     AS com_principal,
          (SELECT count(*) FROM mentor.topico WHERE tier IS NULL)    AS sem_tier
    """,
    # O CASE do ORDER BY repete a MESMA expressao do GROUP BY, de proposito.
    # Escrever `ORDER BY CASE tier ...` parece equivalente e nao e: ali dentro
    # o `tier` vale a COLUNA da tabela, nao o apelido do SELECT - e a coluna
    # nao esta agrupada. O Postgres recusa com "must appear in the GROUP BY".
    "tiers": """
        SELECT COALESCE(tier::text, '(sem tier)') AS rotulo, count(*) AS n
          FROM mentor.topico
         GROUP BY COALESCE(tier::text, '(sem tier)')
         ORDER BY CASE COALESCE(tier::text, '(sem tier)')
                    WHEN 'S' THEN 0 WHEN 'A' THEN 1 WHEN 'B' THEN 2
                    WHEN 'C' THEN 3 WHEN 'Z' THEN 4 ELSE 5 END
    """,
    "suficiencia": """
        SELECT status, count(*) AS n FROM mentor.suficiencia
         GROUP BY status ORDER BY n DESC
    """,
    # Os que ficaram sem tier: quero saber POR QUE, nao so quantos. Se nao tem
    # questao ligada, nao ha o que medir - e estado final legitimo, nao
    # pendencia.
    "sem_tier_quem": """
        SELECT t.titulo,
               (SELECT count(*) FROM mentor.questao_topico qt
                 WHERE qt.topico_id = t.id) AS ligadas,
               cardinality(COALESCE(t.assunto_ids, '{}')) AS assuntos
          FROM mentor.topico t
         WHERE t.tier IS NULL
         ORDER BY 2, 1
    """,
    # A PROVA por tras do tier S: as questoes de PM-BA Soldado que fizeram o
    # assunto virar prioridade. Existe pra o dono poder CONFERIR em vez de
    # acreditar - ele estuda o conteudo, eu so leio dado.
    # Os padroes sao os mesmos do mentor_tiers (ORGAOS_ALVO e CARGO_ALVO);
    # se mudarem la, mudar aqui - senao a evidencia mostrada deixa de ser a
    # que decidiu.
    "evidencia": """
        SELECT t.titulo, q.id, q.ano, q.banca, q.cargo, q.orgao,
               left(COALESCE(q.enunciado, ''), 110) AS trecho
          FROM mentor.topico t
          JOIN mentor.suficiencia s  ON s.topico_id = t.id
          JOIN mentor.questao_topico qt ON qt.topico_id = t.id
          JOIN mentor.questao q      ON q.id = qt.questao_id
         WHERE t.tier IN ('S','A') AND s.status = 'insuficiente'
           AND (q.orgao ~* 'PM ?BA|Pol[ií]cia Militar da Bahia'
                OR q.orgao_sigla ~* 'PM ?BA|CBM ?BA')
           AND q.cargo ~* 'soldado|pra[cç]a'
         ORDER BY t.titulo, q.ano DESC NULLS LAST
    """,
    "genericos": """
        SELECT g.assunto_id, g.cadernos, g.cadernos_total, d.nome AS disciplina
          FROM mentor.assunto_generico g
          LEFT JOIN mentor.disciplina d ON d.id = g.disciplina_id
         ORDER BY g.cadernos DESC
    """,
    # o alerta vermelho ja AGRUPADO: aulas que dividem o mesmo caderno sao o
    # mesmo ASSUNTO. Cru, "Contravencoes Penais" aparece 7 vezes e parece 7
    # problemas.
    "alerta": """
        SELECT t.assunto_ids,
               count(*)            AS aulas,
               min(t.titulo)       AS exemplo,
               min(s.n_total)      AS questoes_uteis,
               min(s.motivo)       AS motivo,
               min(t.tier::text)   AS tier
          FROM mentor.topico t
          JOIN mentor.suficiencia s ON s.topico_id = t.id
         WHERE t.tier IN ('S','A') AND s.status = 'insuficiente'
         GROUP BY t.assunto_ids
         ORDER BY min(s.n_total) NULLS FIRST
    """,
}


def main():
    ap = argparse.ArgumentParser(description="Confere o schema mentor no banco.")
    ap.add_argument("--dsn", default=DSN_PADRAO)
    ap.add_argument("--evidencia", action="store_true",
                    help="mostra as questoes de PM-BA Soldado que fizeram cada "
                         "assunto do alerta virar prioridade")
    args = ap.parse_args()

    try:
        import psycopg2
    except ImportError:
        print("  [ERRO] falta o psycopg2:  pip install psycopg2-binary")
        return 1

    try:
        conn = psycopg2.connect(args.dsn)
    except Exception as e:
        print(f"  [ERRO] nao conectei em {args.dsn.split('@')[-1]}: {e}")
        return 1

    cur = conn.cursor()
    print("=" * 66)
    print(f"  MENTOR - conferencia   ({args.dsn.split('@')[-1]})")
    print("=" * 66)

    quebradas = []

    def consultar(nome):
        """
        Roda uma consulta e devolve as linhas; se der errado, anota e segue.

        Existe porque um GROUP BY errado meu derrubou o script inteiro na
        secao de TIER e o dono perdeu todo o resto do relatorio - que ja
        estava calculado. Uma consulta quebrada custa a propria secao, nao
        a conferencia toda. E o banco precisa de rollback: depois de um erro
        a transacao fica abortada e TUDO que vier depois falha tambem.
        """
        try:
            cur.execute(CONSULTAS[nome])
            return cur.fetchall()
        except Exception as e:
            conn.rollback()
            quebradas.append(f"{nome}: {str(e).strip().splitlines()[0]}")
            print(f"     [!] a secao '{nome}' falhou - segue o resto")
            return []

    linhas = consultar("contagem")
    if not linhas:
        print("\n  [ERRO] nem a contagem basica rodou - o schema existe?")
        return 1
    (questoes, topicos, ligadas, vinculos, sem_assunto,
     com_principal, sem_tier) = linhas[0]
    pct = 100.0 * ligadas / questoes if questoes else 0

    print(f"\n  questoes           {questoes}")
    print(f"  topicos (aulas)    {topicos}")
    print(f"  ligadas a topico   {ligadas}  ({pct:.1f}%)")
    media = f"  ({vinculos / ligadas:.1f} topicos por questao)" if ligadas else ""
    print(f"  vinculos           {vinculos}{media}")
    print(f"  com topico princ.  {com_principal}")
    print(f"  sem assunto        {sem_assunto}")
    print(f"  topicos sem tier   {sem_tier}")

    gen = consultar("genericos")
    print(f"\n  assuntos genericos fora do vinculo: {len(gen)}")
    for aid, cad, tot, disc in gen:
        print(f"     {aid}  em {cad}/{tot} cadernos  [{disc}]")

    print("\n  --- TIER ---")
    for tier, n in consultar("tiers"):
        print(f"     {tier:12} {n:>4}")

    if sem_tier:
        # nao e alarme: e o "por que" dos que nao deram pra medir
        print(f"\n  os {sem_tier} sem tier (nao deu pra medir, nao e pendencia):")
        for titulo, ligadas_t, n_ass in consultar("sem_tier_quem"):
            razao = ("sem caderno de questoes" if not n_ass
                     else "nenhuma questao ligada" if not ligadas_t
                     else f"{ligadas_t} questoes, mas sem evidencia de prova")
            print(f"     {(titulo or '')[:46]:46} {razao}")

    print("\n  --- SUFICIENCIA ---")
    for st, n in consultar("suficiencia"):
        print(f"     {st:12} {n:>4}")

    alertas = consultar("alerta")
    print(f"\n  --- ALERTA VERMELHO (agrupado): {len(alertas)} materia(s) ---")
    for _, aulas, exemplo, n_uteis, motivo, tier in alertas:
        selo = f"{aulas} aulas" if aulas > 1 else "1 aula"
        print(f"     [{tier}] {(exemplo or '')[:44]:44} {selo:8} "
              f"{n_uteis or 0:>3}q")
        print(f"           {(motivo or '')[:90]}")

    if args.evidencia:
        print("\n" + "=" * 66)
        print("  A PROVA POR TRAS DO ALERTA")
        print("  (as questoes de PM-BA SOLDADO que puseram cada assunto")
        print("   na lista de prioridade - confira se batem com o assunto)")
        print("=" * 66)
        atual = None
        for titulo, qid, ano, banca, cargo, orgao, trecho in consultar("evidencia"):
            if titulo != atual:
                atual = titulo
                print(f"\n  >> {titulo}")
            limpo = " ".join((trecho or "").split())
            print(f"     Q{qid}  {ano or '?'}  {(banca or '?')[:26]}  "
                  f"[{(cargo or '?')[:30]}]")
            print(f"        {limpo}...")

    # --- os avisos: o que faria eu desconfiar do proprio numero -------------
    avisos = []
    if pct > 60:
        avisos.append(f"{pct:.0f}% ligadas e ALTO DEMAIS. Em 23/09 isso foi o "
                      "assunto raiz voltando pro vinculo e inflando tudo. "
                      "Esperado hoje: ~33%.")
    if not gen:
        avisos.append("nenhum assunto generico marcado - rode "
                      "mentor_carga.py --vincular")
    if com_principal != ligadas:
        avisos.append(f"{ligadas} ligadas mas {com_principal} com principal - "
                      "deviam bater.")
    # 'tier' nulo NAO e pendencia: o mentor_tiers distingue de proposito
    # Z ("medido, nao cai") de NULL ("nao deu pra medir"). Mandar rodar de
    # novo nao mudaria nada - so ensina a ignorar os avisos. So reclamo se
    # NENHUM topico tiver tier, que ai sim o calculo nunca rodou.
    if sem_tier and sem_tier == topicos:
        avisos.append("nenhum topico tem tier - rode mentor_tiers.py --aplicar")
    for q in quebradas:
        avisos.append(f"consulta com defeito ({q}) - o numero dela nao saiu")

    print()
    if avisos:
        print("  " + "!" * 62)
        for a in avisos:
            print(f"  [!] {a}")
        print("  " + "!" * 62)
    else:
        print("  Nada a apontar: o vinculo esta sao.")
    print()
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
