# -*- coding: utf-8 -*-
"""
routers/mentor.py — MIKE MENTOR, motor de estudo (schema v3, spec v3 + v3.2)

Tudo que o painel precisa, em rotas:
  painel        GET  /mentor/painel                    início: agora, plano de hoje, KPIs, XP/sequência
  edital        GET  /mentor/edital?materia_id=        árvore: módulo → assunto → aulas (tier, estado, estoque)
  semanas       GET  /mentor/semanas                   lista; POST /mentor/semana/montar (planejador da segunda)
                GET  /mentor/semana/{id}
  sessão        POST /mentor/sessao/iniciar            POST /mentor/sessao/{id}/fechar
  aula          GET  /mentor/aula/{id}                 conteúdo + aquecimento + fixação + gate
                POST /mentor/aula/{id}/assistida
                POST /mentor/aula/{id}/gate            fecha o gate (4 de 5); agenda R1 quando o assunto fecha
  resposta      POST /mentor/resposta                  grava e devolve a correção
  revisões      GET  /mentor/revisoes/hoje             POST /mentor/revisao/{id}/concluir  POST /mentor/revisao/{id}/adiar
  questões      GET  /mentor/questoes                  "Fazer questões" (tudo estudado | escolher)
  simulado      GET  /mentor/simulados  POST /mentor/simulado/montar  GET /mentor/simulado/{id}
                POST /mentor/simulado/{id}/entregar
  mais cai      GET  /mentor/mais-cai
  bizus         GET  /mentor/bizus/aula/{id}
  cards         GET  /mentor/cards/hoje  POST /mentor/card/{id}/responder

Regras (spec v3.2): gate 4 de 5 por aula; aquecimento não conta; certo/errado entra em tudo menos no
simulado; revisão adaptativa R1→R7→R30→M com intervalos que esticam ou encurtam pelo acerto; teto de
revisão 35% do dia; XP só por resultado (gate, revisão no dia, card, meta) — nunca por velocidade.
"""
import json
import logging
import math
import random
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query

from database import db

log = logging.getLogger("mentor")
router = APIRouter(prefix="/mentor", tags=["Mentor"])

# ----------------------------------------------------------------- constantes
GATE_N, GATE_MIN = 5, 4
AQUEC_N, FIX_N = 5, 5
REV_N = {"R1": 4, "R2": 4, "R3": 4, "R7": 6, "R15": 5, "R30": 4, "M": 4}
INTERVALO = {"R1": 1, "R2": 1, "R3": 2, "R7": 6, "R15": 8, "R30": 23, "M": 30}
XP = {"gate": 50, "gate_reestudo": 25, "revisao": 30, "revisao_no_dia": 15, "card": 3, "meta_dia": 80,
      "meta_semana": 300, "simulado": 200, "simulado_acima_60": 200, "combo": 10, "aula_propria": 30}
PATENTES = [(0, "Recruta"), (500, "Soldado"), (1500, "Cabo"), (3000, "3º Sargento"), (5000, "2º Sargento"),
            (8000, "1º Sargento"), (12000, "Subtenente"), (17000, "Aspirante"), (23000, "Tenente"),
            (30000, "Capitão"), (40000, "Major"), (52000, "Tenente-Coronel"), (65000, "Coronel")]
MESES_HORAS = {10: 3.0, 11: 4.0, 12: 5.5, 1: 8.0, 2: 8.0, 9: 3.0}
PCT_CONTEUDO = 0.65
DIST_SIMULADO = [(1, 10), (2, 8), (3, 8), (4, 8), (5, 8), (6, 8), (7, 5), (8, 5), (9, 5), (10, 5), (11, 5), (12, 5)]
BANCAS_PREF = ("FCC", "IBFC")


def _hoje():
    # dia de estudo vai das 04:00 às 04:00 (spec) — fuso do servidor
    return (datetime.now() - timedelta(hours=4)).date()


def _r(row):
    return dict(row) if row is not None else None


def _rs(rows):
    return [dict(r) for r in rows]


def _patente(xp):
    p = PATENTES[0][1]
    for lim, nome in PATENTES:
        if xp >= lim:
            p = nome
    return p


async def _add_xp(conn, tipo, pontos, ref=None):
    await conn.execute("INSERT INTO mentor.xp_evento (tipo, pontos, ref) VALUES ($1,$2,$3)", tipo, pontos, json.dumps(ref or {}))
    xp = await conn.fetchval("UPDATE mentor.progresso SET xp_total = xp_total + $1 WHERE id=1 RETURNING xp_total", pontos)
    await conn.execute("UPDATE mentor.progresso SET patente=$1 WHERE id=1", _patente(xp))
    return xp


async def _config(conn, chave, padrao=None):
    v = await conn.fetchval("SELECT valor FROM mentor.config WHERE chave=$1", chave)
    if v is None:
        return padrao
    return json.loads(v) if isinstance(v, str) else v


def _horas_dia(d: date, cfg=None):
    cfg = cfg or {}
    return float(cfg.get(str(d.month), MESES_HORAS.get(d.month, 3.0)))


def _dia_conta(d: date):
    """Outubro: sábado só revisão, domingo livre. De novembro em diante, 7 dias (v3.2 §2)."""
    if d.month in (9, 10) and d.year == 2026:
        return "conteudo" if d.weekday() < 5 else ("revisao" if d.weekday() == 5 else "livre")
    return "conteudo"


# ----------------------------------------------------------------- seleção de questões
SQL_BASE_Q = """
    q.usavel AND q.fora_banca = FALSE AND COALESCE(q.fora_edital, FALSE) = FALSE
    AND (q.prova_real_id IS NULL OR NOT EXISTS (SELECT 1 FROM mentor.prova_real pr WHERE pr.id=q.prova_real_id AND pr.reservada))
"""


async def _questoes_da_aula(conn, aula_id, n, contexto, ineditas=True, so_me=False, excluir=()):
    """Questões ligadas à aula (etiqueta exata primeiro), inéditas, com preferência FCC/IBFC e recentes."""
    excl = list(excluir)
    rows = await conn.fetch(f"""
        SELECT q.id, q.enunciado, q.alternativas, q.gabarito, q.tipo, q.banca_sigla, q.ano, q.orgao_sigla, q.cargo,
               q.indice_acerto, q.comentario_professor, q.soldado_ba, qa.via, qa.forca
        FROM mentor.questao_aula qa JOIN mentor.questao q ON q.id = qa.questao_id
        WHERE qa.aula_id = $1 AND {SQL_BASE_Q}
          {"AND q.tipo = 'ME'" if so_me else ""}
          AND NOT (q.id = ANY($3::bigint[]))
          {"AND NOT EXISTS (SELECT 1 FROM mentor.resposta r WHERE r.questao_id = q.id)" if ineditas else ""}
        ORDER BY qa.forca DESC,
                 CASE WHEN q.banca_sigla = ANY($4::text[]) THEN 0 ELSE 1 END,
                 CASE WHEN q.soldado_ba THEN 0 ELSE 1 END,
                 q.ano DESC NULLS LAST, random()
        LIMIT $2
    """, aula_id, n, excl, list(BANCAS_PREF))
    out = _rs(rows)
    if len(out) < n and ineditas:
        # estoque curto: completa com questão já vista há mais de 30 dias (spec §3.10)
        mais = await conn.fetch(f"""
            SELECT q.id, q.enunciado, q.alternativas, q.gabarito, q.tipo, q.banca_sigla, q.ano, q.orgao_sigla, q.cargo,
                   q.indice_acerto, q.comentario_professor, q.soldado_ba, qa.via, qa.forca
            FROM mentor.questao_aula qa JOIN mentor.questao q ON q.id = qa.questao_id
            WHERE qa.aula_id = $1 AND {SQL_BASE_Q}
              AND NOT (q.id = ANY($3::bigint[]))
              AND NOT EXISTS (SELECT 1 FROM mentor.resposta r WHERE r.questao_id = q.id AND r.data > now() - interval '30 days')
            ORDER BY random() LIMIT $2
        """, aula_id, n - len(out), excl + [q["id"] for q in out])
        out += _rs(mais)
    for q in out:
        q["alternativas"] = json.loads(q["alternativas"]) if isinstance(q["alternativas"], str) else q["alternativas"]
        q["contexto"] = contexto
    return out


async def _questoes_do_assunto(conn, assunto_id, n, ineditas=True):
    rows = await conn.fetch(f"""
        SELECT DISTINCT ON (q.id) q.id, q.enunciado, q.alternativas, q.gabarito, q.tipo, q.banca_sigla, q.ano,
               q.orgao_sigla, q.indice_acerto, q.comentario_professor, q.soldado_ba, qa.aula_id
        FROM mentor.questao_aula qa JOIN mentor.questao q ON q.id = qa.questao_id
        WHERE qa.assunto_id = $1 AND {SQL_BASE_Q}
          {"AND NOT EXISTS (SELECT 1 FROM mentor.resposta r WHERE r.questao_id = q.id)" if ineditas else ""}
    """, assunto_id)
    lst = _rs(rows)
    random.shuffle(lst)
    lst.sort(key=lambda q: (0 if q["banca_sigla"] in BANCAS_PREF else 1, 0 if q["soldado_ba"] else 1))
    out = lst[:n]
    if len(out) < n and ineditas:
        out += await _questoes_do_assunto(conn, assunto_id, n - len(out), ineditas=False)
    for q in out:
        q["alternativas"] = json.loads(q["alternativas"]) if isinstance(q["alternativas"], str) else q["alternativas"]
    return out[:n]


# ----------------------------------------------------------------- painel
@router.get("/painel")
async def painel():
    hoje = _hoje()
    async with db() as conn:
        prog = _r(await conn.fetchrow("SELECT * FROM mentor.progresso WHERE id=1"))
        semana = _r(await conn.fetchrow("SELECT * FROM mentor.semana WHERE status='aberta' ORDER BY inicio DESC LIMIT 1"))
        plano = []
        if semana:
            plano = _rs(await conn.fetch("""
                SELECT pi.id, pi.ordem, pi.previsto_h, pi.feito, s.id AS assunto_id, s.nome, s.tier, s.modo, s.estado, m.nome AS materia,
                       (SELECT count(*) FROM mentor.aula a WHERE a.assunto_id=s.id AND a.tipo IN ('conteudo','exercicios','propria')) AS n_aulas,
                       (SELECT count(*) FROM mentor.aula a WHERE a.assunto_id=s.id AND a.estado='concluida') AS n_concluidas
                FROM mentor.plano_item pi JOIN mentor.assunto s ON s.id=pi.assunto_id JOIN mentor.materia m ON m.id=s.materia_id
                WHERE pi.semana_id=$1 ORDER BY pi.ordem""", semana["id"]))
        revisoes = _rs(await conn.fetch("""
            SELECT r.id, r.tipo, r.prevista, s.nome, m.nome AS materia, (r.prevista < $1) AS atrasada
            FROM mentor.revisao r JOIN mentor.assunto s ON s.id=r.assunto_id JOIN mentor.materia m ON m.id=s.materia_id
            WHERE r.feita IS NULL AND COALESCE(r.adiada_para, r.prevista) <= $1 ORDER BY r.prevista""", hoje))
        cards = await conn.fetchval("SELECT count(*) FROM mentor.card WHERE ativo AND proxima <= $1", hoje)
        # próxima ação: revisão atrasada > R1 de ontem > aula pendente do plano
        proxima = None
        if revisoes:
            proxima = {"tipo": "revisao", "revisao_id": revisoes[0]["id"], "titulo": f"{revisoes[0]['tipo']} · {revisoes[0]['nome']}", "materia": revisoes[0]["materia"]}
        else:
            for it in plano:
                if not it["feito"]:
                    aula = _r(await conn.fetchrow("""SELECT id, titulo, duracao_s, estado FROM mentor.aula WHERE assunto_id=$1 AND estado <> 'concluida'
                                                     AND tipo IN ('conteudo','exercicios','propria') ORDER BY ordem LIMIT 1""", it["assunto_id"]))
                    if aula:
                        proxima = {"tipo": "aula", "aula_id": aula["id"], "assunto_id": it["assunto_id"], "titulo": aula["titulo"], "materia": it["materia"], "duracao_s": aula["duracao_s"], "modo": it["modo"]}
                        break
        # KPIs
        horas_hoje = await conn.fetchval("SELECT COALESCE(sum(minutos),0)/60.0 FROM mentor.sessao WHERE fim IS NOT NULL AND (inicio - interval '4 hours')::date = $1", hoje)
        horas_semana = semana["horas_feitas"] if semana else 0
        dias30 = await conn.fetchval("SELECT count(DISTINCT (inicio - interval '4 hours')::date) FROM mentor.sessao WHERE inicio > now() - interval '30 days' AND minutos > 0")
        respondidas = await conn.fetchval("SELECT count(*) FROM mentor.resposta WHERE contexto <> 'aquecimento'")
        acerto = await conn.fetchval("SELECT round(100.0*avg(CASE WHEN correta THEN 1 ELSE 0 END),1) FROM mentor.resposta WHERE contexto IN ('gate','R1','R7','R30','M','questoes','simulado')")
        por_dia = _rs(await conn.fetch("""SELECT (inicio - interval '4 hours')::date AS dia, round(sum(minutos)/60.0,2) AS horas
                                          FROM mentor.sessao WHERE fim IS NOT NULL AND inicio > now() - interval '35 days' GROUP BY 1 ORDER BY 1"""))
        por_materia = _rs(await conn.fetch("""SELECT m.nome AS materia, count(*) AS n, round(100.0*avg(CASE WHEN r.correta THEN 1 ELSE 0 END),0) AS acerto
                                              FROM mentor.resposta r JOIN mentor.assunto s ON s.id=r.assunto_id JOIN mentor.materia m ON m.id=s.materia_id
                                              WHERE r.contexto <> 'aquecimento' GROUP BY 1 ORDER BY 1"""))
        meta_dia = _horas_dia(hoje, await _config(conn, "horas_dia", {}))
    return {"hoje": hoje.isoformat(), "dia": _dia_conta(hoje), "meta_dia_h": meta_dia, "horas_hoje": float(horas_hoje or 0),
            "semana": semana, "plano": plano, "revisoes_pendentes": revisoes, "cards_pendentes": cards, "proxima": proxima,
            "kpi": {"horas_semana": float(horas_semana or 0), "meta_semana": float(semana["meta_horas"]) if semana else None,
                    "dias_30": dias30, "questoes_respondidas": respondidas, "acerto_geral": float(acerto) if acerto is not None else None},
            "horas_por_dia": por_dia, "acerto_por_materia": por_materia, "progresso": prog}


# ----------------------------------------------------------------- edital
@router.get("/materias")
async def materias():
    async with db() as conn:
        rows = await conn.fetch("""
            SELECT m.id, m.nome, m.peso_prova,
                   count(DISTINCT s.id) AS assuntos,
                   count(DISTINCT s.id) FILTER (WHERE s.estado IN ('estudado','dominado')) AS estudados,
                   count(DISTINCT a.id) AS aulas,
                   count(DISTINCT a.id) FILTER (WHERE a.estado='concluida') AS aulas_concluidas,
                   COALESCE(sum(a.duracao_s) FILTER (WHERE s.tier IN ('S','A')),0)/3600.0 AS horas_sa
            FROM mentor.materia m LEFT JOIN mentor.assunto s ON s.materia_id=m.id LEFT JOIN mentor.aula a ON a.assunto_id=s.id
            GROUP BY m.id ORDER BY m.id""")
    return _rs(rows)


@router.get("/edital")
async def edital(materia_id: int = Query(...)):
    async with db() as conn:
        mat = _r(await conn.fetchrow("SELECT * FROM mentor.materia WHERE id=$1", materia_id))
        if not mat:
            raise HTTPException(404, "matéria não encontrada")
        mods = _rs(await conn.fetch("SELECT * FROM mentor.modulo WHERE materia_id=$1 ORDER BY ordem", materia_id))
        ass = _rs(await conn.fetch("""
            SELECT s.*, (SELECT count(DISTINCT qa.questao_id) FROM mentor.questao_aula qa JOIN mentor.questao q ON q.id=qa.questao_id
                         WHERE qa.assunto_id=s.id AND {base}) AS estoque_usavel,
                   (SELECT count(*) FROM mentor.resposta r WHERE r.assunto_id=s.id AND r.contexto <> 'aquecimento') AS respondidas,
                   (SELECT round(100.0*avg(CASE WHEN r.correta THEN 1 ELSE 0 END),0) FROM mentor.resposta r WHERE r.assunto_id=s.id AND r.contexto IN ('gate','R1','R7','R30','M')) AS acerto
            FROM mentor.assunto s WHERE s.materia_id=$1 ORDER BY s.modulo_id, s.ordem""".format(base=SQL_BASE_Q.replace("q.", "q.")), materia_id))
        aulas = _rs(await conn.fetch("""SELECT id, assunto_id, ordem, tipo, titulo, duracao_s, estado, codigo_aula, video_id
                                        FROM mentor.aula WHERE assunto_id = ANY($1::bigint[]) ORDER BY assunto_id, ordem""", [a["id"] for a in ass]))
    por_ass = {}
    for a in aulas:
        por_ass.setdefault(a["assunto_id"], []).append(a)
    for a in ass:
        a["aulas"] = por_ass.get(a["id"], [])
        a.pop("etiquetas", None)
    for m in mods:
        m["assuntos"] = [a for a in ass if a["modulo_id"] == m["id"]]
    mat.pop("etiquetas_raiz", None)
    mat["modulos"] = mods
    return mat


# ----------------------------------------------------------------- semanas e planejador
def _segunda(d: date):
    return d - timedelta(days=d.weekday())


async def _capacidade_semana(conn, seg: date):
    cfg = await _config(conn, "horas_dia", {})
    total = 0.0
    for i in range(7):
        d = seg + timedelta(days=i)
        tipo = _dia_conta(d)
        if tipo == "conteudo":
            total += _horas_dia(d, cfg)
        elif tipo == "revisao":
            total += 3.0
    return round(total, 1)


def _custo(assunto):
    v = (assunto.get("duracao_total") or 0) / 3600.0
    n = assunto.get("n_aulas") or 0
    t = assunto.get("tier")
    if t == "C":
        return round(n * 0.15 + 0.1, 2)
    if t == "treino":
        return round(n * 0.3, 2)
    if assunto.get("origem") == "propria":
        return 0.75
    return round(v / 1.5 + n * 0.25, 2)


@router.get("/semanas")
async def semanas():
    async with db() as conn:
        rows = await conn.fetch("""SELECT s.*, (SELECT count(*) FROM mentor.plano_item p WHERE p.semana_id=s.id) AS itens,
                                          (SELECT count(*) FROM mentor.plano_item p WHERE p.semana_id=s.id AND p.feito) AS feitos
                                   FROM mentor.semana s ORDER BY inicio""")
    return _rs(rows)


@router.get("/semana/{semana_id}")
async def semana(semana_id: int):
    async with db() as conn:
        s = _r(await conn.fetchrow("SELECT * FROM mentor.semana WHERE id=$1", semana_id))
        if not s:
            raise HTTPException(404, "semana não encontrada")
        s["itens"] = _rs(await conn.fetch("""
            SELECT pi.*, a.nome, a.tier, a.modo, a.estado, m.nome AS materia,
                   (SELECT count(*) FROM mentor.aula x WHERE x.assunto_id=a.id AND x.tipo IN ('conteudo','exercicios','propria')) AS n_aulas,
                   (SELECT count(*) FROM mentor.aula x WHERE x.assunto_id=a.id AND x.estado='concluida') AS n_concluidas
            FROM mentor.plano_item pi JOIN mentor.assunto a ON a.id=pi.assunto_id JOIN mentor.materia m ON m.id=a.materia_id
            WHERE pi.semana_id=$1 ORDER BY pi.ordem""", semana_id))
        s["revisoes"] = _rs(await conn.fetch("""SELECT r.id, r.tipo, r.prevista, r.feita, r.acerto, a.nome, m.nome AS materia
                                                FROM mentor.revisao r JOIN mentor.assunto a ON a.id=r.assunto_id JOIN mentor.materia m ON m.id=a.materia_id
                                                WHERE COALESCE(r.adiada_para, r.prevista) BETWEEN $1 AND $2 ORDER BY r.prevista""", s["inicio"], s["fim"]))
    return s


@router.post("/semana/montar")
async def montar_semana(inicio: Optional[str] = Body(None, embed=True), forcar: bool = Body(False, embed=True)):
    """
    O planejador da segunda (spec v3.2 §2): fecha a semana anterior, abre a nova e escolhe os assuntos.
    Regra: S de todas as matérias primeiro, depois A, depois B (com evidência antes), depois C por resumo;
    4-5 matérias por semana, na proporção das horas restantes × peso; ordem do curso dentro da matéria;
    o que sobrou da semana anterior vai na frente.
    """
    hoje = _hoje()
    seg = _segunda(date.fromisoformat(inicio)) if inicio else _segunda(hoje)
    async with db() as conn:
        async with conn.transaction():
            existente = _r(await conn.fetchrow("SELECT * FROM mentor.semana WHERE inicio=$1", seg))
            if existente and existente["status"] != "bloqueada" and not forcar:
                raise HTTPException(409, f"semana de {seg} já está {existente['status']}; use forcar=true pra refazer")
            # fecha a anterior aberta
            await conn.execute("""UPDATE mentor.semana SET status='fechada', fechada_em=now(),
                                  horas_feitas = COALESCE((SELECT sum(minutos)/60.0 FROM mentor.sessao x WHERE x.fim IS NOT NULL
                                                           AND (x.inicio - interval '4 hours')::date BETWEEN semana.inicio AND semana.fim),0)
                                  WHERE status='aberta' AND inicio < $1""", seg)
            cap_total = await _capacidade_semana(conn, seg)
            cap_conteudo = round(cap_total * PCT_CONTEUDO, 1)
            # pendências: assuntos em andamento ou do plano anterior não feitos
            pend = _rs(await conn.fetch("""
                SELECT DISTINCT a.id, a.nome, a.tier, a.modo, a.origem, a.materia_id, m.peso_prova, a.ordem, a.modulo_id,
                       (SELECT count(*) FROM mentor.aula x WHERE x.assunto_id=a.id AND x.tipo IN ('conteudo','exercicios','propria') AND x.estado <> 'concluida') AS n_aulas,
                       (SELECT COALESCE(sum(duracao_s),0) FROM mentor.aula x WHERE x.assunto_id=a.id AND x.tipo IN ('conteudo','exercicios','propria') AND x.estado <> 'concluida') AS duracao_total
                FROM mentor.assunto a JOIN mentor.materia m ON m.id=a.materia_id
                WHERE a.estado = 'em_andamento' OR a.id IN (SELECT p.assunto_id FROM mentor.plano_item p JOIN mentor.semana s ON s.id=p.semana_id
                                                            WHERE s.inicio < $1 AND NOT p.feito AND s.status='fechada')""", seg))
            restantes = _rs(await conn.fetch("""
                SELECT a.id, a.nome, a.tier, a.modo, a.origem, a.materia_id, m.peso_prova, a.ordem, a.modulo_id, mo.ordem AS modulo_ordem,
                       a.soldado,
                       (SELECT count(*) FROM mentor.aula x WHERE x.assunto_id=a.id AND x.tipo IN ('conteudo','exercicios','propria')) AS n_aulas,
                       (SELECT COALESCE(sum(duracao_s),0) FROM mentor.aula x WHERE x.assunto_id=a.id AND x.tipo IN ('conteudo','exercicios','propria')) AS duracao_total
                FROM mentor.assunto a JOIN mentor.materia m ON m.id=a.materia_id JOIN mentor.modulo mo ON mo.id=a.modulo_id
                WHERE a.estado = 'nao_estudado' AND a.tier IN ('S','A','B','C','treino') AND a.id <> ALL($1::bigint[])
                ORDER BY a.materia_id, (mo.tipo = 'propria'), mo.ordem, a.ordem""", [p["id"] for p in pend]))
            escolhidos, gasto = [], 0.0
            for p in pend:
                escolhidos.append(p)
                gasto += _custo(p)
            PESO_MAT = {1: 1.5, 8: 1.5, 7: 1.3, 4: 1.1, 6: 1.1, 10: 1.1, 12: 1.1, 5: 0.8}
            for tier in ("S", "A", "B", "C", "treino"):
                if gasto >= cap_conteudo * 0.95:
                    break
                fila = {}
                for a in restantes:
                    if a["tier"] == tier:
                        fila.setdefault(a["materia_id"], []).append(a)
                if tier == "B":
                    for lst in fila.values():
                        lst.sort(key=lambda a: (-(a["soldado"] or 0), a["modulo_ordem"], a["ordem"]))
                if not fila:
                    continue
                resto = cap_conteudo - gasto
                peso = {mid: sum(_custo(a) for a in lst) * PESO_MAT.get(mid, 1.0) for mid, lst in fila.items()}
                top = sorted(peso, key=lambda k: -peso[k])[:5]
                tot = sum(peso[m] for m in top) or 1
                for mid in top:
                    if gasto >= cap_conteudo:
                        break
                    aloc, g = resto * peso[mid] / tot, 0.0
                    for a in fila[mid]:
                        c = _custo(a)
                        if gasto + g + c > cap_conteudo * 1.1 or (g > 0 and g + c > aloc * 1.2):
                            break
                        escolhidos.append(a)
                        g += c
                    gasto += g
            fim = seg + timedelta(days=6)
            fase = "A" if seg < date(2026, 12, 14) else ("B" if seg < date(2027, 1, 4) else ("C" if seg < date(2027, 1, 18) else "D"))
            if existente:
                sid = existente["id"]
                await conn.execute("UPDATE mentor.semana SET meta_horas=$2, status='aberta', fase=$3, montada_em=now() WHERE id=$1", sid, cap_total, fase)
                await conn.execute("DELETE FROM mentor.plano_item WHERE semana_id=$1", sid)
            else:
                sid = await conn.fetchval("INSERT INTO mentor.semana (inicio, fim, meta_horas, status, fase, montada_em) VALUES ($1,$2,$3,'aberta',$4,now()) RETURNING id",
                                          seg, fim, cap_total, fase)
            for i, a in enumerate(escolhidos, 1):
                await conn.execute("INSERT INTO mentor.plano_item (semana_id, assunto_id, ordem, previsto_h) VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING", sid, a["id"], i, _custo(a))
            # bloqueia a seguinte (cria placeholder)
            prox = seg + timedelta(days=7)
            await conn.execute("INSERT INTO mentor.semana (inicio, fim, meta_horas, status) VALUES ($1,$2,$3,'bloqueada') ON CONFLICT (inicio) DO NOTHING",
                               prox, prox + timedelta(days=6), await _capacidade_semana(conn, prox))
    return {"semana_id": sid, "inicio": seg.isoformat(), "meta_horas": cap_total, "conteudo_h": cap_conteudo, "fase": fase,
            "itens": len(escolhidos), "horas_previstas": round(gasto, 1), "pendencias_da_anterior": len(pend)}


# ----------------------------------------------------------------- sessão
@router.post("/sessao/iniciar")
async def iniciar_sessao(tipo: str = Body("estudo", embed=True)):
    if tipo not in ("estudo", "revisao", "questoes", "simulado", "cards", "semana"):
        raise HTTPException(400, "tipo inválido")
    async with db() as conn:
        aberta = await conn.fetchval("SELECT id FROM mentor.sessao WHERE fim IS NULL ORDER BY inicio DESC LIMIT 1")
        if aberta:
            await conn.execute("UPDATE mentor.sessao SET fim=now(), minutos=LEAST(EXTRACT(EPOCH FROM now()-inicio)/60, 240)::int WHERE id=$1", aberta)
        sid = await conn.fetchval("INSERT INTO mentor.sessao (tipo) VALUES ($1) RETURNING id", tipo)
    return {"sessao_id": sid}


@router.post("/sessao/{sessao_id}/fechar")
async def fechar_sessao(sessao_id: int):
    hoje = _hoje()
    async with db() as conn:
        s = _r(await conn.fetchrow("SELECT * FROM mentor.sessao WHERE id=$1", sessao_id))
        if not s:
            raise HTTPException(404, "sessão não encontrada")
        if s["fim"] is None:
            await conn.execute("UPDATE mentor.sessao SET fim=now(), minutos=LEAST(EXTRACT(EPOCH FROM now()-inicio)/60, 240)::int WHERE id=$1", sessao_id)
        resumo = _r(await conn.fetchrow("""
            SELECT count(*) FILTER (WHERE contexto NOT IN ('aquecimento','fixacao')) AS questoes,
                   count(*) FILTER (WHERE correta AND contexto NOT IN ('aquecimento','fixacao')) AS certas,
                   count(DISTINCT aula_id) FILTER (WHERE contexto='gate') AS aulas
            FROM mentor.resposta WHERE sessao_id=$1""", sessao_id))
        xp_sessao = await conn.fetchval("SELECT COALESCE(sum(pontos),0) FROM mentor.xp_evento WHERE (ref->>'sessao_id')::bigint = $1", sessao_id)
        # meta do dia e sequência
        horas_hoje = await conn.fetchval("SELECT COALESCE(sum(minutos),0)/60.0 FROM mentor.sessao WHERE fim IS NOT NULL AND (inicio - interval '4 hours')::date = $1", hoje)
        meta = _horas_dia(hoje, await _config(conn, "horas_dia", {}))
        if _dia_conta(hoje) == "revisao":
            meta = 3.0
        prog = _r(await conn.fetchrow("SELECT * FROM mentor.progresso WHERE id=1"))
        meta_batida = False
        if float(horas_hoje) >= meta * 0.9 and prog["ultimo_dia_meta"] != hoje:
            meta_batida = True
            ontem = hoje - timedelta(days=1)
            if prog["ultimo_dia_meta"] == ontem or (prog["ultimo_dia_meta"] and (hoje - prog["ultimo_dia_meta"]).days == 2 and prog["congelamentos"] > 0):
                streak = prog["streak_dias"] + 1
                congel = prog["congelamentos"] - (1 if (hoje - prog["ultimo_dia_meta"]).days == 2 else 0)
            else:
                streak, congel = 1, prog["congelamentos"]
            await conn.execute("UPDATE mentor.progresso SET streak_dias=$1, ultimo_dia_meta=$2, congelamentos=$3 WHERE id=1", streak, hoje, congel)
            await _add_xp(conn, "meta_dia", XP["meta_dia"], {"dia": hoje.isoformat(), "sessao_id": sessao_id})
            if hoje.weekday() == 0:
                await conn.execute("UPDATE mentor.progresso SET congelamentos = LEAST(congelamentos + 1, 2) WHERE id=1")
        await conn.execute("UPDATE mentor.sessao SET resumo=$2, xp=$3 WHERE id=$1", sessao_id, json.dumps({**resumo, "meta_batida": meta_batida}), int(xp_sessao))
        prog = _r(await conn.fetchrow("SELECT * FROM mentor.progresso WHERE id=1"))
        s = _r(await conn.fetchrow("SELECT * FROM mentor.sessao WHERE id=$1", sessao_id))
    return {"sessao": s, "resumo": resumo, "xp_sessao": int(xp_sessao), "horas_hoje": float(horas_hoje), "meta_dia": meta, "meta_batida": meta_batida, "progresso": prog}


# ----------------------------------------------------------------- aula
@router.get("/aula/{aula_id}")
async def aula(aula_id: int, so_conteudo: bool = False):
    async with db() as conn:
        a = _r(await conn.fetchrow("""SELECT a.*, s.nome AS assunto, s.tier, s.modo, s.materia_id, m.nome AS materia
                                      FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id JOIN mentor.materia m ON m.id=s.materia_id WHERE a.id=$1""", aula_id))
        if not a:
            raise HTTPException(404, "aula não encontrada")
        a.pop("transcricao", None)
        for k in ("flashcards", "questoes_fixacao"):
            if isinstance(a.get(k), str):
                try:
                    a[k] = json.loads(a[k])
                except Exception:
                    pass
        a["bizus"] = _rs(await conn.fetch("SELECT id, secao, texto, fonte FROM mentor.bizu WHERE aula_id=$1 AND aprovado ORDER BY secao, id LIMIT 12", aula_id))
        a["estoque"] = await conn.fetchval(f"SELECT count(*) FROM mentor.questao_aula qa JOIN mentor.questao q ON q.id=qa.questao_id WHERE qa.aula_id=$1 AND {SQL_BASE_Q}", aula_id)
        a["ineditas"] = await conn.fetchval(f"""SELECT count(*) FROM mentor.questao_aula qa JOIN mentor.questao q ON q.id=qa.questao_id WHERE qa.aula_id=$1 AND {SQL_BASE_Q}
                                             AND NOT EXISTS (SELECT 1 FROM mentor.resposta r WHERE r.questao_id=q.id)""", aula_id)
        if not so_conteudo:
            a["aquecimento"] = await _questoes_da_aula(conn, aula_id, AQUEC_N, "aquecimento")
            fix = a.get("questoes_fixacao") or []
            a["fixacao"] = fix[:FIX_N] if isinstance(fix, list) else []
            a["gate"] = await _questoes_da_aula(conn, aula_id, GATE_N, "gate", excluir=[q["id"] for q in a["aquecimento"]])
        # a aula em andamento marca o assunto
        await conn.execute("UPDATE mentor.assunto SET estado='em_andamento' WHERE id=$1 AND estado='nao_estudado'", a["assunto_id"])
    return a


@router.post("/aula/{aula_id}/assistida")
async def aula_assistida(aula_id: int):
    async with db() as conn:
        ok = await conn.fetchval("UPDATE mentor.aula SET estado='assistida', assistida_em=now() WHERE id=$1 AND estado IN ('pendente','ressalva','pulada') RETURNING id", aula_id)
    return {"aula_id": aula_id, "assistida": bool(ok)}


@router.post("/resposta")
async def responder(questao_id: int = Body(...), contexto: str = Body(...), marcada: Optional[str] = Body(None),
                    aula_id: Optional[int] = Body(None), assunto_id: Optional[int] = Body(None), sessao_id: Optional[int] = Body(None),
                    confianca: Optional[str] = Body(None), tipo_erro: Optional[str] = Body(None), tempo_s: Optional[int] = Body(None),
                    revisao_id: Optional[int] = Body(None)):
    if contexto not in ("aquecimento", "fixacao", "gate", "reestudo", "R1", "R3", "R7", "R15", "R30", "M", "semana", "questoes", "simulado", "bolso", "R2"):
        raise HTTPException(400, "contexto inválido")
    async with db() as conn:
        q = _r(await conn.fetchrow("SELECT id, gabarito, comentario_professor, alternativas, tipo FROM mentor.questao WHERE id=$1", questao_id))
        if not q:
            raise HTTPException(404, "questão não encontrada")
        if assunto_id is None and aula_id is not None:
            assunto_id = await conn.fetchval("SELECT assunto_id FROM mentor.aula WHERE id=$1", aula_id)
        correta = (marcada or "").strip().upper() == (q["gabarito"] or "").strip().upper() if marcada else None
        rid = await conn.fetchval("""INSERT INTO mentor.resposta (questao_id, sessao_id, aula_id, assunto_id, contexto, marcada, correta, confianca, tipo_erro, tempo_s)
                                     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id""",
                                  questao_id, sessao_id, aula_id, assunto_id, contexto, marcada, correta, confianca, tipo_erro, tempo_s)
        # combo de certeza: 3 certezas certas seguidas = XP (v3.2 dopaminérgico)
        combo = None
        if correta and confianca == "certeza" and contexto in ("gate", "R1", "R7", "R30", "M", "questoes"):
            ult = await conn.fetch("SELECT correta, confianca FROM mentor.resposta WHERE sessao_id=$1 AND contexto NOT IN ('aquecimento','fixacao') ORDER BY id DESC LIMIT 3", sessao_id)
            if len(ult) == 3 and all(r["correta"] and r["confianca"] == "certeza" for r in ult):
                await _add_xp(conn, "combo", XP["combo"], {"sessao_id": sessao_id, "resposta_id": rid})
                combo = XP["combo"]
    return {"resposta_id": rid, "correta": correta, "gabarito": q["gabarito"], "comentario_professor": q["comentario_professor"], "combo_xp": combo}


async def _agendar_revisao(conn, assunto_id, tipo, dias, hoje=None):
    hoje = hoje or _hoje()
    prevista = hoje + timedelta(days=dias)
    await conn.execute("DELETE FROM mentor.revisao WHERE assunto_id=$1 AND feita IS NULL", assunto_id)
    await conn.execute("INSERT INTO mentor.revisao (assunto_id, tipo, prevista) VALUES ($1,$2,$3)", assunto_id, tipo, prevista)
    return prevista


@router.post("/aula/{aula_id}/gate")
async def fechar_gate(aula_id: int, sessao_id: Optional[int] = Body(None, embed=True)):
    """Lê as últimas 5 respostas de gate desta aula. 4+ = concluída. Se todas as aulas do assunto fecharam, R1 amanhã."""
    async with db() as conn:
        async with conn.transaction():
            a = _r(await conn.fetchrow("SELECT a.*, s.tier FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id WHERE a.id=$1", aula_id))
            if not a:
                raise HTTPException(404, "aula não encontrada")
            if a["estado"] == "concluida":
                return {"aula_id": aula_id, "fechou": True, "ja_concluida": True}
            ult = await conn.fetch("SELECT correta FROM mentor.resposta WHERE aula_id=$1 AND contexto='gate' ORDER BY id DESC LIMIT $2", aula_id, GATE_N)
            certas = sum(1 for r in ult if r["correta"])
            n = len(ult)
            if n < GATE_N:
                return {"aula_id": aula_id, "fechou": False, "certas": certas, "n": n, "faltam": GATE_N - n}
            total_gate = await conn.fetchval("SELECT count(*) FROM mentor.resposta WHERE aula_id=$1 AND contexto='gate'", aula_id)
            reestudo_antes = max(0, (total_gate - 1) // GATE_N)   # 0 na 1ª rodada, 1 na 2ª...
            if certas >= GATE_MIN:
                await conn.execute("UPDATE mentor.aula SET estado='concluida', concluida_em=now() WHERE id=$1", aula_id)
                xp = await _add_xp(conn, "gate", XP["gate"] if not reestudo_antes else XP["gate_reestudo"], {"aula_id": aula_id, "sessao_id": sessao_id})
                await conn.execute("UPDATE mentor.plano_item pi SET feito=TRUE FROM mentor.semana s WHERE pi.semana_id=s.id AND s.status='aberta' AND pi.assunto_id=$1 AND NOT EXISTS (SELECT 1 FROM mentor.aula x WHERE x.assunto_id=$1 AND x.tipo IN ('conteudo','exercicios','propria') AND x.estado <> 'concluida')", a["assunto_id"])
                faltam = await conn.fetchval("SELECT count(*) FROM mentor.aula WHERE assunto_id=$1 AND tipo IN ('conteudo','exercicios','propria') AND estado <> 'concluida'", a["assunto_id"])
                r1 = None
                if faltam == 0:
                    await conn.execute("UPDATE mentor.assunto SET estado='estudado', estudado_em=now() WHERE id=$1", a["assunto_id"])
                    r1 = await _agendar_revisao(conn, a["assunto_id"], "R1", INTERVALO["R1"])
                return {"aula_id": aula_id, "fechou": True, "certas": certas, "n": n, "xp": xp, "assunto_fechou": faltam == 0, "r1_em": r1.isoformat() if r1 else None}
            # reprovou: reestudo + 5 novas (não repete as do gate)
            await conn.execute("UPDATE mentor.aula SET estado='ressalva' WHERE id=$1", aula_id)
            vistas = [r["questao_id"] for r in await conn.fetch("SELECT questao_id FROM mentor.resposta WHERE aula_id=$1", aula_id)]
            novas = await _questoes_da_aula(conn, aula_id, GATE_N, "gate", excluir=vistas)
            if reestudo_antes >= 1:
                # reincidente: fica estudado com ressalva, R1 com gate próprio (spec §3.3.2)
                await conn.execute("UPDATE mentor.aula SET estado='concluida', concluida_em=now() WHERE id=$1", aula_id)
                faltam = await conn.fetchval("SELECT count(*) FROM mentor.aula WHERE assunto_id=$1 AND tipo IN ('conteudo','exercicios','propria') AND estado <> 'concluida'", a["assunto_id"])
                if faltam == 0:
                    await conn.execute("UPDATE mentor.assunto SET estado='ressalva', estudado_em=now() WHERE id=$1", a["assunto_id"])
                    await _agendar_revisao(conn, a["assunto_id"], "R1", INTERVALO["R1"])
                return {"aula_id": aula_id, "fechou": True, "com_ressalva": True, "certas": certas, "n": n, "assunto_fechou": faltam == 0}
            # marca o reestudo (conta quantas vezes a aula reprovou) sem inventar resposta
            await conn.execute("UPDATE mentor.aula SET estado='ressalva' WHERE id=$1", aula_id)
            return {"aula_id": aula_id, "fechou": False, "certas": certas, "n": n, "reestudo": True, "resumo_bolso": a["resumo_bolso"], "novas": novas}


# ----------------------------------------------------------------- revisões (curva do esquecimento)
def _proxima_revisao(tipo, acerto):
    """Intervalo adaptativo (v3.2 §3). Devolve (tipo_seguinte, dias) ou ('reestudo', 0)."""
    if tipo == "R1":
        if acerto >= 75: return "R7", INTERVALO["R7"]
        if acerto >= 50: return "R3", INTERVALO["R3"]
        return "R2", INTERVALO["R2"]
    if tipo in ("R2", "R3"):
        return ("R7", INTERVALO["R7"]) if acerto >= 60 else ("R3", INTERVALO["R3"])
    if tipo == "R7":
        if acerto >= 80: return "R30", INTERVALO["R30"]
        if acerto >= 60: return "R15", INTERVALO["R15"]
        return "R3", INTERVALO["R3"]
    if tipo == "R15":
        return ("R30", INTERVALO["R30"]) if acerto >= 60 else ("R7", INTERVALO["R7"])
    # R30 e M
    if acerto >= 60: return "M", INTERVALO["M"]
    return "R7", INTERVALO["R7"]


@router.get("/revisoes/hoje")
async def revisoes_hoje(com_questoes: bool = True):
    hoje = _hoje()
    async with db() as conn:
        revs = _rs(await conn.fetch("""
            SELECT r.id, r.tipo, r.prevista, r.adiada_para, r.n_adiamentos, s.id AS assunto_id, s.nome, s.tier, m.nome AS materia, m.id AS materia_id,
                   (r.prevista < $1) AS atrasada
            FROM mentor.revisao r JOIN mentor.assunto s ON s.id=r.assunto_id JOIN mentor.materia m ON m.id=s.materia_id
            WHERE r.feita IS NULL AND COALESCE(r.adiada_para, r.prevista) <= $1
            ORDER BY r.prevista, m.id""", hoje))
        # intercalar: alterna matérias
        por_mat = {}
        for r in revs:
            por_mat.setdefault(r["materia_id"], []).append(r)
        ordenadas = []
        while any(por_mat.values()):
            for mid in list(por_mat):
                if por_mat[mid]:
                    ordenadas.append(por_mat[mid].pop(0))
        if com_questoes:
            for r in ordenadas:
                r["questoes"] = await _questoes_do_assunto(conn, r["assunto_id"], REV_N.get(r["tipo"], 4))
                # erro pendente volta dentro da revisão (v3.2 §3)
                erradas = await conn.fetch(f"""
                    SELECT q.id, q.enunciado, q.alternativas, q.gabarito, q.tipo, q.banca_sigla, q.ano, q.orgao_sigla, q.indice_acerto, q.comentario_professor, q.soldado_ba
                    FROM mentor.questao q WHERE q.id IN (
                        SELECT r1.questao_id FROM mentor.resposta r1 WHERE r1.assunto_id=$1 AND r1.correta = FALSE AND r1.contexto <> 'aquecimento'
                          AND NOT EXISTS (SELECT 1 FROM mentor.resposta r2 WHERE r2.questao_id=r1.questao_id AND r2.correta AND r2.id > r1.id))
                    AND {SQL_BASE_Q} ORDER BY random() LIMIT 2""", r["assunto_id"])
                for e in erradas:
                    e = dict(e)
                    e["alternativas"] = json.loads(e["alternativas"]) if isinstance(e["alternativas"], str) else e["alternativas"]
                    e["repeticao"] = True
                    if e["id"] not in [x["id"] for x in r["questoes"]]:
                        r["questoes"].append(e)
        teto_min = int(_horas_dia(hoje, await _config(conn, "horas_dia", {})) * 60 * 0.35)
    return {"hoje": hoje.isoformat(), "revisoes": ordenadas, "teto_minutos": teto_min}


@router.post("/revisao/{revisao_id}/concluir")
async def concluir_revisao(revisao_id: int, sessao_id: Optional[int] = Body(None, embed=True)):
    hoje = _hoje()
    async with db() as conn:
        async with conn.transaction():
            r = _r(await conn.fetchrow("SELECT * FROM mentor.revisao WHERE id=$1", revisao_id))
            if not r:
                raise HTTPException(404, "revisão não encontrada")
            if r["feita"]:
                raise HTTPException(409, "revisão já feita")
            resp = await conn.fetch("SELECT correta FROM mentor.resposta WHERE assunto_id=$1 AND contexto=$2 AND data > $3 ORDER BY id DESC LIMIT $4",
                                    r["assunto_id"], r["tipo"], datetime.combine(hoje, datetime.min.time()) - timedelta(days=1), REV_N.get(r["tipo"], 4) + 2)
            n = len(resp)
            if n == 0:
                raise HTTPException(409, "nenhuma resposta desta revisão gravada ainda")
            acerto = round(100.0 * sum(1 for x in resp if x["correta"]) / n, 1)
            await conn.execute("UPDATE mentor.revisao SET feita=now(), acerto=$2, n_questoes=$3, sessao_id=$4 WHERE id=$1", revisao_id, acerto, n, sessao_id)
            prox, dias = _proxima_revisao(r["tipo"], acerto)
            prevista = await _agendar_revisao(conn, r["assunto_id"], prox, dias, hoje)
            no_dia = (r["adiada_para"] or r["prevista"]) >= hoje
            xp = await _add_xp(conn, "revisao", XP["revisao"] + (XP["revisao_no_dia"] if no_dia else 0), {"revisao_id": revisao_id, "sessao_id": sessao_id, "acerto": acerto})
            reestudo = acerto < 50 and r["tipo"] in ("R1", "R7")
            if reestudo:
                await conn.execute("UPDATE mentor.assunto SET estado='ressalva' WHERE id=$1", r["assunto_id"])
    return {"revisao_id": revisao_id, "acerto": acerto, "n": n, "proxima": prox, "prevista": prevista.isoformat(), "xp": xp, "reestudo": reestudo}


@router.post("/revisao/{revisao_id}/adiar")
async def adiar_revisao(revisao_id: int, dias: int = Body(1, embed=True)):
    if dias < 1 or dias > 3:
        raise HTTPException(400, "adiar de 1 a 3 dias")
    async with db() as conn:
        ok = await conn.fetchval("UPDATE mentor.revisao SET adiada_para = COALESCE(adiada_para, prevista) + $2, n_adiamentos = n_adiamentos + 1 WHERE id=$1 AND feita IS NULL RETURNING adiada_para", revisao_id, dias)
        if not ok:
            raise HTTPException(404, "revisão não encontrada ou já feita")
    return {"revisao_id": revisao_id, "adiada_para": ok.isoformat()}


# ----------------------------------------------------------------- questões (aba)
@router.get("/questoes")
async def questoes(modo: str = "estudado", materias: Optional[str] = None, assuntos: Optional[str] = None, n: int = 10, so_me: bool = False):
    n = max(5, min(n, 30))
    async with db() as conn:
        if modo == "estudado":
            ids = [r["id"] for r in await conn.fetch("SELECT id FROM mentor.assunto WHERE estado IN ('estudado','ressalva','dominado','em_andamento')")]
        else:
            ids = [int(x) for x in (assuntos or "").split(",") if x.strip().isdigit()]
            if materias and not ids:
                ids = [r["id"] for r in await conn.fetch("SELECT id FROM mentor.assunto WHERE materia_id = ANY($1::int[])", [int(x) for x in materias.split(",") if x.strip().isdigit()])]
        if not ids:
            return {"questoes": [], "aviso": "nenhum assunto selecionado ou estudado ainda"}
        rows = await conn.fetch(f"""
            SELECT DISTINCT ON (q.id) q.id, q.enunciado, q.alternativas, q.gabarito, q.tipo, q.banca_sigla, q.ano, q.orgao_sigla,
                   q.indice_acerto, q.comentario_professor, q.soldado_ba, qa.assunto_id, qa.aula_id
            FROM mentor.questao_aula qa JOIN mentor.questao q ON q.id=qa.questao_id
            WHERE qa.assunto_id = ANY($1::bigint[]) AND {SQL_BASE_Q} {"AND q.tipo='ME'" if so_me else ""}
              AND NOT EXISTS (SELECT 1 FROM mentor.resposta r WHERE r.questao_id=q.id)
        """, ids)
        lst = _rs(rows)
        random.shuffle(lst)
        # mistura matérias/assuntos: no máximo 3 por assunto
        out, por = [], {}
        teto = max(3, math.ceil(n / max(1, len(ids))))
        for q in lst:
            if por.get(q["assunto_id"], 0) >= teto:
                continue
            por[q["assunto_id"]] = por.get(q["assunto_id"], 0) + 1
            out.append(q)
            if len(out) >= n:
                break
        for q in out:
            q["alternativas"] = json.loads(q["alternativas"]) if isinstance(q["alternativas"], str) else q["alternativas"]
    return {"questoes": out, "disponiveis": len(lst)}


# ----------------------------------------------------------------- simulados
@router.get("/simulados")
async def simulados():
    async with db() as conn:
        rows = await conn.fetch("SELECT s.id, s.numero, s.data, s.tipo, s.status, s.certas, s.nota, s.por_materia, s.inicio, s.fim, array_length(s.questoes,1) AS n, p.nome AS prova FROM mentor.simulado s LEFT JOIN mentor.prova_real p ON p.id=s.prova_real_id ORDER BY s.data")
        reais = await conn.fetch("SELECT id, nome, banca, ano, n_questoes, reservada FROM mentor.prova_real WHERE soldado_ba ORDER BY ano")
    return {"simulados": _rs(rows), "provas_reais": _rs(reais)}


@router.post("/simulado/montar")
async def montar_simulado(data: Optional[str] = Body(None, embed=True), prova_real_id: Optional[int] = Body(None, embed=True), n_total: int = Body(80, embed=True)):
    """Montado: 80 ME inéditas, distribuição do edital 2022, FCC de preferência, sem C/E. Real: a prova reservada inteira."""
    d = date.fromisoformat(data) if data else _hoje()
    async with db() as conn:
        async with conn.transaction():
            if prova_real_id:
                ids = [r["id"] for r in await conn.fetch("SELECT id FROM mentor.questao WHERE prova_real_id=$1 AND usavel ORDER BY id", prova_real_id)]
                if not ids:
                    raise HTTPException(404, "prova real sem questões usáveis")
                tipo = "real"
            else:
                ids = []
                fator = n_total / 80.0
                for mid, n in DIST_SIMULADO:
                    k = max(1, round(n * fator))
                    rows = await conn.fetch(f"""
                        SELECT DISTINCT ON (q.id) q.id, (q.banca_sigla = ANY($3::text[])) AS pref, q.ano
                        FROM mentor.questao_aula qa JOIN mentor.questao q ON q.id=qa.questao_id JOIN mentor.assunto s ON s.id=qa.assunto_id
                        WHERE s.materia_id=$1 AND q.tipo='ME' AND {SQL_BASE_Q}
                          AND NOT EXISTS (SELECT 1 FROM mentor.resposta r WHERE r.questao_id=q.id)
                          AND NOT EXISTS (SELECT 1 FROM mentor.simulado sm WHERE q.id = ANY(sm.questoes))
                        ORDER BY q.id, random() LIMIT $2""", mid, k * 6, list(BANCAS_PREF))
                    lst = _rs(rows)
                    random.shuffle(lst)
                    lst.sort(key=lambda r: (not r["pref"], -(r["ano"] or 0)))
                    ids += [r["id"] for r in lst[:k]]
                tipo = "montado"
            numero = (await conn.fetchval("SELECT COALESCE(max(numero),0) FROM mentor.simulado")) + 1
            sid = await conn.fetchval("INSERT INTO mentor.simulado (numero, data, tipo, prova_real_id, questoes) VALUES ($1,$2,$3,$4,$5) RETURNING id", numero, d, tipo, prova_real_id, ids)
    return {"simulado_id": sid, "numero": numero, "tipo": tipo, "n": len(ids), "data": d.isoformat()}


@router.get("/simulado/{simulado_id}")
async def simulado(simulado_id: int):
    async with db() as conn:
        s = _r(await conn.fetchrow("SELECT * FROM mentor.simulado WHERE id=$1", simulado_id))
        if not s:
            raise HTTPException(404, "simulado não encontrado")
        qs = _rs(await conn.fetch("""SELECT q.id, q.enunciado, q.alternativas, q.tipo, q.banca_sigla, q.ano, q.orgao_sigla, m.nome AS materia
                                     FROM mentor.questao q LEFT JOIN LATERAL (SELECT s.materia_id FROM mentor.questao_aula qa JOIN mentor.assunto s ON s.id=qa.assunto_id WHERE qa.questao_id=q.id LIMIT 1) x ON TRUE
                                     LEFT JOIN mentor.materia m ON m.id=x.materia_id WHERE q.id = ANY($1::bigint[])""", s["questoes"]))
        ordem = {qid: i for i, qid in enumerate(s["questoes"])}
        qs.sort(key=lambda q: ordem.get(q["id"], 0))
        for q in qs:
            q["alternativas"] = json.loads(q["alternativas"]) if isinstance(q["alternativas"], str) else q["alternativas"]
        if s["status"] == "agendado":
            await conn.execute("UPDATE mentor.simulado SET status='em_andamento', inicio=now() WHERE id=$1", simulado_id)
        s["questoes"] = qs
        s["por_materia"] = json.loads(s["por_materia"]) if isinstance(s["por_materia"], str) else s["por_materia"]
    return s


@router.post("/simulado/{simulado_id}/entregar")
async def entregar_simulado(simulado_id: int, respostas: dict = Body(..., embed=True), sessao_id: Optional[int] = Body(None, embed=True)):
    cfg_pontos = 1.25
    async with db() as conn:
        async with conn.transaction():
            s = _r(await conn.fetchrow("SELECT * FROM mentor.simulado WHERE id=$1", simulado_id))
            if not s or s["status"] == "entregue":
                raise HTTPException(409, "simulado inexistente ou já entregue")
            gab = {r["id"]: (r["gabarito"], r["materia"]) for r in await conn.fetch("""
                SELECT q.id, q.gabarito, m.nome AS materia FROM mentor.questao q
                LEFT JOIN LATERAL (SELECT s.materia_id FROM mentor.questao_aula qa JOIN mentor.assunto s ON s.id=qa.assunto_id WHERE qa.questao_id=q.id LIMIT 1) x ON TRUE
                LEFT JOIN mentor.materia m ON m.id=x.materia_id WHERE q.id = ANY($1::bigint[])""", s["questoes"])}
            certas, por = 0, {}
            for qid in s["questoes"]:
                marcada = (respostas.get(str(qid)) or respostas.get(qid) or "").strip().upper() or None
                g, mat = gab.get(qid, (None, None))
                ok = bool(marcada and g and marcada == g.strip().upper())
                certas += ok
                por.setdefault(mat or "?", {"n": 0, "certas": 0})
                por[mat or "?"]["n"] += 1
                por[mat or "?"]["certas"] += ok
                await conn.execute("INSERT INTO mentor.resposta (questao_id, sessao_id, contexto, marcada, correta) VALUES ($1,$2,'simulado',$3,$4)", qid, sessao_id, marcada, ok if marcada else None)
            n = len(s["questoes"]) or 1
            nota = round(certas * (100.0 / n), 2) if n != 80 else round(certas * cfg_pontos, 2)
            await conn.execute("UPDATE mentor.simulado SET status='entregue', fim=now(), certas=$2, nota=$3, por_materia=$4, respostas=$5 WHERE id=$1",
                               simulado_id, certas, nota, json.dumps(por), json.dumps({str(k): v for k, v in respostas.items()}))
            xp = await _add_xp(conn, "simulado", XP["simulado"] + (XP["simulado_acima_60"] if nota >= 60 else 0), {"simulado_id": simulado_id, "nota": nota, "sessao_id": sessao_id})
    return {"simulado_id": simulado_id, "certas": certas, "n": n, "nota": nota, "por_materia": por, "xp": xp}


# ----------------------------------------------------------------- o que mais cai · bizus · cards
@router.get("/mais-cai")
async def mais_cai(materia_id: Optional[int] = None, limite: int = 30):
    async with db() as conn:
        rows = await conn.fetch("""
            SELECT e.id, e.nome, m.nome AS materia, count(DISTINCT q.id) AS questoes, count(DISTINCT q.prova_real_id) AS provas
            FROM mentor.questao q CROSS JOIN LATERAL unnest(q.etiquetas) AS et(id)
            JOIN mentor.etiqueta e ON e.id = et.id
            LEFT JOIN mentor.materia m ON e.raiz = ANY(m.etiquetas_raiz)
            WHERE q.soldado_ba AND e.id <> e.raiz AND ($1::int IS NULL OR m.id = $1)
            GROUP BY e.id, e.nome, m.nome ORDER BY questoes DESC, provas DESC LIMIT $2""", materia_id, limite)
        total = await conn.fetchval("SELECT count(*) FROM mentor.questao WHERE soldado_ba")
        provas = await conn.fetchval("SELECT count(*) FROM mentor.prova_real WHERE soldado_ba")
    return {"total_questoes": total, "provas": provas, "etiquetas": _rs(rows)}


@router.get("/bizus/aula/{aula_id}")
async def bizus_aula(aula_id: int):
    async with db() as conn:
        rows = await conn.fetch("SELECT id, secao, texto, fonte, aprovado FROM mentor.bizu WHERE aula_id=$1 ORDER BY secao, id", aula_id)
    return _rs(rows)


@router.post("/bizu")
async def novo_bizu(aula_id: Optional[int] = Body(None), assunto_id: Optional[int] = Body(None), texto: str = Body(...)):
    texto = (texto or "").strip()
    if len(texto) < 5 or len(texto) > 500:
        raise HTTPException(400, "bizu de 5 a 500 caracteres")
    async with db() as conn:
        if assunto_id is None and aula_id:
            assunto_id = await conn.fetchval("SELECT assunto_id FROM mentor.aula WHERE id=$1", aula_id)
        bid = await conn.fetchval("INSERT INTO mentor.bizu (aula_id, assunto_id, fonte, texto) VALUES ($1,$2,'usuario',$3) RETURNING id", aula_id, assunto_id, texto)
        await conn.execute("INSERT INTO mentor.card (assunto_id, aula_id, tipo, frente, verso, fonte, proxima) VALUES ($1,$2,'usuario',$3,$3,'meu bizu',$4)", assunto_id, aula_id, texto, _hoje() + timedelta(days=1))
    return {"bizu_id": bid}


CAIXAS = [1, 3, 7, 15, 30]


@router.get("/cards/hoje")
async def cards_hoje(limite: int = 20):
    hoje = _hoje()
    async with db() as conn:
        rows = await conn.fetch("""SELECT c.id, c.tipo, c.frente, c.verso, c.caixa, c.proxima, s.nome AS assunto, m.nome AS materia
                                   FROM mentor.card c LEFT JOIN mentor.assunto s ON s.id=c.assunto_id LEFT JOIN mentor.materia m ON m.id=s.materia_id
                                   WHERE c.ativo AND (c.proxima IS NULL OR c.proxima <= $1) ORDER BY c.proxima NULLS FIRST, random() LIMIT $2""", hoje, limite)
    return _rs(rows)


@router.post("/card/{card_id}/responder")
async def responder_card(card_id: int, lembrou: bool = Body(..., embed=True)):
    hoje = _hoje()
    async with db() as conn:
        c = _r(await conn.fetchrow("SELECT * FROM mentor.card WHERE id=$1 AND ativo", card_id))
        if not c:
            raise HTTPException(404, "card não encontrado")
        caixa = min(c["caixa"] + 1, len(CAIXAS) - 1) if lembrou else 0
        prox = hoje + timedelta(days=CAIXAS[caixa])
        await conn.execute("UPDATE mentor.card SET caixa=$2, proxima=$3, acertos=acertos+$4, erros=erros+$5 WHERE id=$1", card_id, caixa, prox, int(lembrou), int(not lembrou))
        xp = await _add_xp(conn, "card", XP["card"], {"card_id": card_id}) if lembrou else None
    return {"card_id": card_id, "caixa": caixa, "proxima": prox.isoformat(), "xp": xp}
