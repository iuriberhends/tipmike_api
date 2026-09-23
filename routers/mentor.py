"""
routers/mentor.py — Mike Mentor, leitura do schema `mentor`.

SO LEITURA, de proposito. O diagnostico (tier §3.7 + suficiencia §3.8) e a
hipotese em que a agenda (§3.4), o SRS (§3.6) e o bot (§3.5) vao se apoiar:
"estude primeiro o tier S que esta insuficiente". Se essa priorizacao estiver
errada, as tres fases seguintes nascem erradas. Validar custa um router de
leitura; descobrir depois custa refazer tres fases.

DUAS ARMADILHAS DO DADO, as duas ja custaram caro uma vez:

1. NAO use mentor.questao.topico_id pra listar as questoes de uma aula.
   Ele e so o topico PRINCIPAL - uma escolha entre varios. 17% das questoes
   pertencem legitimamente a mais de um topico (a mesma questao de furto serve
   as 4 aulas de Furto). O vinculo completo esta em mentor.questao_topico.

2. Aulas diferentes dividem o mesmo caderno de questoes. "Furto", "Furto II",
   "Furto VIII" e "Furto - Exercicios" tem as MESMAS 173 questoes e a mesma
   evidencia de prova. Listadas cruas, viram 4 prioridades onde ha 1. Por isso
   /topicos agrupa por assunto e diz quantas aulas ha em cada grupo.

ENDPOINTS:
    GET /mentor/disciplinas          panorama por disciplina
    GET /mentor/topicos              tier e suficiencia, agrupados
    GET /mentor/topico/{id}          a aula: resumo, cards, questoes
    GET /mentor/diagnostico          o que estudar primeiro (§3.7 + §3.8)
    GET /mentor/saude                o que ha de torto no dado
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from database import db

logger = logging.getLogger("tipmike.mentor")

router = APIRouter(prefix="/mentor", tags=["Mentor"])

# A ordem em que o tier importa pra quem vai estudar. 'Z' = medido e nao cai;
# NULL = nao deu pra medir. Sao coisas diferentes e nao podem colidir.
ORDEM_TIER = "CASE t.tier WHEN 'S' THEN 0 WHEN 'A' THEN 1 WHEN 'B' THEN 2 " \
             "WHEN 'C' THEN 3 WHEN 'Z' THEN 4 ELSE 5 END"


@router.get("/disciplinas")
async def disciplinas():
    """Panorama: quantas aulas, quantas questoes ligadas, como estao os tiers."""
    async with db() as conn:
        rows = await conn.fetch("""
            SELECT d.id, d.nome, d.peso_prova,
                   count(DISTINCT t.id)                              AS topicos,
                   count(DISTINCT qt.questao_id)                     AS questoes,
                   count(DISTINCT t.id) FILTER (WHERE t.tier = 'S')  AS tier_s,
                   count(DISTINCT t.id) FILTER (WHERE t.tier = 'A')  AS tier_a,
                   count(DISTINCT t.id) FILTER (WHERE t.tier IS NULL) AS sem_tier,
                   count(DISTINCT t.id) FILTER (
                       WHERE s.status = 'insuficiente')              AS insuficientes,
                   count(DISTINCT t.id) FILTER (
                       WHERE t.tier IN ('S','A')
                         AND s.status = 'insuficiente')              AS alerta_vermelho
              FROM mentor.disciplina d
              LEFT JOIN mentor.topico t       ON t.disciplina_id = d.id
              LEFT JOIN mentor.suficiencia s  ON s.topico_id = t.id
              LEFT JOIN mentor.questao_topico qt ON qt.topico_id = t.id
             GROUP BY d.id, d.nome, d.peso_prova
             ORDER BY d.peso_prova DESC NULLS LAST, d.nome
        """)
    return {"disciplinas": [dict(r) for r in rows]}


@router.get("/topicos")
async def topicos(
    disciplina: Optional[int] = Query(None, description="id da disciplina"),
    tier: Optional[str] = Query(None, description="S, A, B, C ou Z"),
    apenas_insuficientes: bool = False,
    agrupar: bool = Query(True, description="junta aulas que dividem o caderno"),
):
    """
    Os topicos com tier e suficiencia.

    Com agrupar=True (padrao), aulas que compartilham exatamente o mesmo
    conjunto de assuntos viram UMA linha com a lista de aulas dentro. Sem isso
    "Furto" aparece 4 vezes com numeros identicos e parece 4 prioridades.
    """
    filtros, args = [], []
    if disciplina:
        args.append(disciplina)
        filtros.append(f"t.disciplina_id = ${len(args)}")
    if tier:
        args.append(tier.upper())
        filtros.append(f"t.tier = ${len(args)}")
    if apenas_insuficientes:
        filtros.append("s.status = 'insuficiente'")
    onde = ("WHERE " + " AND ".join(filtros)) if filtros else ""

    async with db() as conn:
        rows = await conn.fetch(f"""
            SELECT t.id, t.titulo, t.ordem, t.assunto_ids,
                   d.id AS disciplina_id, d.nome AS disciplina,
                   m.titulo AS modulo,
                   t.tier, t.tier_motivo AS evidencia, s.status, s.motivo,
                   s.n_total AS questoes_uteis,
                   (SELECT count(*) FROM mentor.questao_topico qt
                     WHERE qt.topico_id = t.id) AS questoes_ligadas
              FROM mentor.topico t
              JOIN mentor.disciplina d ON d.id = t.disciplina_id
              LEFT JOIN mentor.modulo m      ON m.id = t.modulo_id
              LEFT JOIN mentor.suficiencia s ON s.topico_id = t.id
              {onde}
             ORDER BY {ORDEM_TIER}, s.n_total DESC NULLS LAST, t.ordem
        """, *args)

    itens = [dict(r) for r in rows]
    if not agrupar:
        return {"total": len(itens), "agrupado": False, "topicos": itens}

    grupos: dict = {}
    for it in itens:
        # a chave e o conjunto de assuntos: quem divide o caderno divide as
        # questoes, o tier e a evidencia - e uma coisa so pra quem estuda
        chave = (it["disciplina_id"], tuple(sorted(it["assunto_ids"] or [])))
        g = grupos.get(chave)
        if g is None:
            g = {k: it[k] for k in ("disciplina_id", "disciplina", "modulo",
                                    "tier", "evidencia", "status", "motivo",
                                    "questoes_uteis", "questoes_ligadas",
                                    "assunto_ids")}
            g["aulas"] = []
            grupos[chave] = g
        g["aulas"].append({"id": it["id"], "titulo": it["titulo"],
                           "ordem": it["ordem"]})

    saida = list(grupos.values())
    for g in saida:
        g["aulas"].sort(key=lambda a: (a["ordem"] is None, a["ordem"]))
        g["titulo"] = g["aulas"][0]["titulo"]
        g["n_aulas"] = len(g["aulas"])
    ordem = {"S": 0, "A": 1, "B": 2, "C": 3, "Z": 4}
    saida.sort(key=lambda g: (ordem.get(g["tier"], 5),
                              -(g["questoes_uteis"] or 0)))
    return {"total": len(saida), "aulas_no_total": len(itens),
            "agrupado": True, "topicos": saida}


@router.get("/topico/{topico_id}")
async def topico(topico_id: int,
                 questoes: int = Query(20, ge=0, le=200),
                 cards: int = Query(50, ge=0, le=500)):
    """Uma aula: conteudo, cards, questoes de fixacao e questoes do caderno."""
    async with db() as conn:
        t = await conn.fetchrow("""
            -- t.* ja traz tier e tier_motivo: repetir aqui daria coluna
            -- duplicada, o mesmo erro que derrubou a view da migration 021
            SELECT t.*, d.nome AS disciplina, m.titulo AS modulo,
                   s.status, s.motivo, s.n_total
              FROM mentor.topico t
              JOIN mentor.disciplina d ON d.id = t.disciplina_id
              LEFT JOIN mentor.modulo m      ON m.id = t.modulo_id
              LEFT JOIN mentor.suficiencia s ON s.topico_id = t.id
             WHERE t.id = $1
        """, topico_id)
        if not t:
            raise HTTPException(404, f"topico {topico_id} nao existe")

        aula = await conn.fetchrow(
            "SELECT * FROM mentor.aula WHERE topico_id = $1", topico_id)
        irmas = await conn.fetch("""
            SELECT id, titulo, ordem FROM mentor.topico
             WHERE disciplina_id = $1 AND assunto_ids = $2 AND id <> $3
             ORDER BY ordem
        """, t["disciplina_id"], t["assunto_ids"], topico_id)
        lista_cards = await conn.fetch("""
            SELECT id, frente, verso, deck FROM mentor.card
             WHERE topico_id = $1 ORDER BY id LIMIT $2
        """, topico_id, cards)
        fixacao = await conn.fetch("""
            SELECT id, enunciado, alternativas, gabarito, explicacao_por_alternativa
              FROM mentor.questao_fixacao
             WHERE topico_id = $1 ORDER BY id
        """, topico_id)
        # pelo VINCULO, nao por questao.topico_id: senao a aula mostra so a
        # fatia que ganhou o sorteio de "principal"
        do_caderno = await conn.fetch(f"""
            SELECT q.id, q.enunciado, q.gabarito, q.banca, q.ano, q.orgao,
                   q.indice_acerto_gran, qt.forca, qt.principal
              FROM mentor.questao_topico qt
              JOIN mentor.questao q ON q.id = qt.questao_id
             WHERE qt.topico_id = $1
               AND q.so_texto AND NOT q.anulada AND NOT q.desatualizada
             ORDER BY qt.forca DESC, q.indice_acerto_gran NULLS LAST
             LIMIT $2
        """, topico_id, questoes)
        total_q = await conn.fetchval(
            "SELECT count(*) FROM mentor.questao_topico WHERE topico_id = $1",
            topico_id)

    return {
        "topico": dict(t),
        "aula": dict(aula) if aula else None,
        # quem divide o caderno divide as questoes: dizer isso evita a leitura
        # de que sao materias diferentes com numeros iguais por coincidencia
        "aulas_irmas": [dict(r) for r in irmas],
        "cards": [dict(r) for r in lista_cards],
        "questoes_fixacao": [dict(r) for r in fixacao],
        "questoes": {"total": total_q, "mostrando": len(do_caderno),
                     "itens": [dict(r) for r in do_caderno]},
    }


@router.get("/diagnostico")
async def diagnostico(disciplina: Optional[int] = None, limite: int = 30):
    """
    O que estudar primeiro: cai na PMBA e nao tem questao que baste.

    Agrupa as aulas irmas - 9 das 14 linhas de alerta de hoje sao a MESMA
    materia (Contravencoes Penais) repetida em 7 aulas. Listar cru faz parecer
    que ha 9 problemas onde ha 1.
    """
    args = []
    onde = "WHERE t.tier IN ('S','A') AND s.status = 'insuficiente'"
    if disciplina:
        args.append(disciplina)
        onde += f" AND t.disciplina_id = ${len(args)}"

    async with db() as conn:
        rows = await conn.fetch(f"""
            SELECT t.id, t.titulo, t.assunto_ids, t.disciplina_id,
                   d.nome AS disciplina,
                   t.tier, t.tier_motivo AS evidencia, s.status, s.motivo, s.n_total
              FROM mentor.topico t
              JOIN mentor.disciplina d ON d.id = t.disciplina_id
              JOIN mentor.suficiencia s ON s.topico_id = t.id
              {onde}
             ORDER BY {ORDEM_TIER}, s.n_total NULLS FIRST
        """, *args)

    grupos: dict = {}
    for r in rows:
        chave = (r["disciplina_id"], tuple(sorted(r["assunto_ids"] or [])))
        g = grupos.setdefault(chave, {
            "disciplina": r["disciplina"], "tier": r["tier"],
            "evidencia": r["evidencia"], "motivo": r["motivo"],
            "questoes_uteis": r["n_total"], "aulas": []})
        g["aulas"].append({"id": r["id"], "titulo": r["titulo"]})
    saida = list(grupos.values())
    for g in saida:
        g["titulo"] = g["aulas"][0]["titulo"]
        g["n_aulas"] = len(g["aulas"])
    saida.sort(key=lambda g: (g["tier"] != "S", g["questoes_uteis"] or 0))

    return {"alerta_vermelho": len(saida),
            "aulas_envolvidas": len(rows),
            "itens": saida[:limite]}


@router.get("/saude")
async def saude():
    """
    O que ha de torto no dado. Existe pra o painel nunca mostrar numero
    bonito sem dizer do que ele e feito.
    """
    async with db() as conn:
        r = await conn.fetchrow("""
            SELECT
              (SELECT count(*) FROM mentor.questao)                    AS questoes,
              (SELECT count(*) FROM mentor.questao
                WHERE assunto_ids IS NULL
                   OR cardinality(assunto_ids) = 0)                    AS sem_assunto,
              (SELECT count(DISTINCT questao_id)
                 FROM mentor.questao_topico)                           AS ligadas,
              (SELECT count(*) FROM mentor.questao_topico)             AS vinculos,
              (SELECT count(*) FROM mentor.questao
                WHERE NOT so_texto)                                    AS nao_so_texto,
              (SELECT count(*) FROM mentor.questao
                WHERE gabarito IS NULL)                                AS sem_gabarito,
              (SELECT count(*) FROM mentor.topico
                WHERE assunto_ids IS NULL
                   OR cardinality(assunto_ids) = 0)                    AS topicos_sem_caderno,
              (SELECT count(*) FROM mentor.topico
                WHERE tier IS NULL)                                    AS topicos_sem_tier
        """)
        genericos = await conn.fetch("""
            SELECT g.assunto_id, g.cadernos, g.cadernos_total, d.nome AS disciplina
              FROM mentor.assunto_generico g
              LEFT JOIN mentor.disciplina d ON d.id = g.disciplina_id
             ORDER BY g.cadernos DESC
        """)

    # o agregado sempre devolve linha em producao, mas 500 por causa de um
    # dict(None) seria o jeito mais bobo de a tela de saude ficar doente
    d = dict(r) if r else {}
    d["assuntos_genericos"] = [dict(x) for x in genericos]
    d["avisos"] = []
    if d.get("questoes"):
        pct = 100.0 * d["ligadas"] / d["questoes"]
        d["pct_ligadas"] = round(pct, 1)
        # A trava que faltava em 23/09: sem os genericos fora, o vinculo pulou
        # pra 78% e ninguem estranhou. 34% e o esperado hoje.
        if pct > 60:
            d["avisos"].append(
                f"{pct:.0f}% das questoes ligadas a algum topico - alto demais. "
                "Suspeite de assunto generico (a raiz da disciplina) tendo "
                "voltado pro vinculo.")
    if not genericos:
        d["avisos"].append(
            "nenhum assunto generico marcado - rode mentor_carga.py --vincular")
    if d.get("sem_assunto"):
        d["avisos"].append(
            f"{d['sem_assunto']} questao(oes) sem assunto: nao entram em topico "
            "nenhum. Sao questoes que sairam do filtro do Gran.")
    return d
